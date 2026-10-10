"""Refresh-token rotation, reuse detection, lifetimes and revocation (RFC 9700/7009).

Synthetic principals on disposable PostgreSQL schemas; no provider or broker.
"""
import secrets
import time
import unittest

from test_cloud_postgres import PostgresFixture, SERVICES
from agenthub.enterprise import Denied, _digest


def pair():
    return secrets.token_urlsafe(48), secrets.token_urlsafe(48)


class LifetimeConfiguration(unittest.TestCase):
    def test_defaults_bounds_and_runtime_settings(self):
        from agenthub.cloud_identity import validate_credential_lifetimes, DEFAULT_CREDENTIAL_LIFETIMES
        self.assertEqual(validate_credential_lifetimes(), {'access_ttl_seconds': 3600,
            'refresh_idle_seconds': 2592000, 'refresh_absolute_seconds': 7776000})
        self.assertEqual(validate_credential_lifetimes({'access_ttl_seconds': 900})['access_ttl_seconds'], 900)
        for bad in ({'access_ttl_seconds': 59}, {'access_ttl_seconds': 86401}, {'refresh_idle_seconds': 3600},
                    {'refresh_idle_seconds': 100 * 86400}, {'refresh_absolute_seconds': 400 * 86400},
                    {'access_ttl_seconds': 600.0}, {'unknown': 1}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_credential_lifetimes(bad)
        self.assertEqual(DEFAULT_CREDENTIAL_LIFETIMES['access_ttl_seconds'], 3600)
        from agenthub.cloud_runtime import registry_from_settings
        with self.assertRaisesRegex(ValueError, 'credential'):
            registry_from_settings({'control_dsn': 'unused', 'credentials': {'access_ttl_seconds': 10}}, '/unused')


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL required')
class RefreshTokenLifecycle(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup(); self.addCleanup(self.postgres_teardown)
        self.issued = self.store.enroll_with_refresh('acme', 'alice', 'alice-laptop', ['read', 'ingest'])

    def refresh(self, token, replacement=None):
        access, refresh = replacement or pair()
        result = self.store.refresh_credential(token, replacement_access_token=access,
            replacement_refresh_token=refresh, request_id='synthetic-request')
        return access, refresh, result

    def audit(self):
        with self.store.open() as state:
            return [dict(row) for row in state.db.execute(
                "SELECT * FROM enterprise_audit WHERE operation LIKE 'credential_%' ORDER BY id")]

    def test_rotation_issues_new_pair_and_spends_old_refresh(self):
        old_access, old_refresh = self.issued['access_token'], self.issued['refresh_token']
        self.assertEqual(self.store.authenticate(old_access)['actor'], 'alice')
        access, refresh, result = self.refresh(old_refresh)
        self.assertEqual({k: result[k] for k in ('tenant', 'principal', 'actor', 'enrollment', 'actions')},
            {'tenant': 'acme', 'principal': 'alice', 'actor': 'alice', 'enrollment': 'alice-laptop',
             'actions': ['ingest', 'read']})
        self.assertEqual(self.store.authenticate(access)['actor'], 'alice')
        with self.assertRaises(Denied): self.store.authenticate(old_access)
        # The new refresh token works exactly once more; the spent one never does.
        self.refresh(refresh)
        with self.assertRaises(Denied): self.refresh(old_refresh)

    def test_reuse_of_spent_refresh_revokes_whole_family_and_is_audited(self):
        access, refresh, _ = self.refresh(self.issued['refresh_token'])
        with self.assertRaises(Denied): self.refresh(self.issued['refresh_token'])
        with self.assertRaises(Denied): self.store.authenticate(access)
        with self.assertRaises(Denied): self.refresh(refresh)
        with self.store.open() as state:
            family = state.db.execute('SELECT * FROM enterprise_credential_families').fetchall()
            self.assertEqual([(f['enrollment'], f['revoked_reason']) for f in family if f['revoked_at']],
                [('alice-laptop', 'refresh_token_reuse')])
            active = state.db.execute('''SELECT count(*) FROM enterprise_credentials c
                JOIN enterprise_credential_families f ON f.id=c.family
                WHERE f.enrollment='alice-laptop' AND c.active=1''').fetchone()[0]
        self.assertEqual(active, 0)
        reuse = [row for row in self.audit() if row['operation'] == 'credential_refresh_reuse']
        self.assertEqual([(row['actor'], row['disposition']) for row in reuse], [('alice', 'revoked')])
        # Another family of the same principal is untouched.
        self.assertEqual(self.store.authenticate(self.tokens['alice'])['actor'], 'alice')

    def test_identical_lost_reply_retry_is_idempotent_until_successor_used(self):
        replacement = pair()
        first = self.refresh(self.issued['refresh_token'], replacement)[2]
        again = self.refresh(self.issued['refresh_token'], replacement)[2]
        self.assertEqual(first, again)
        self.assertEqual(self.store.authenticate(replacement[0])['actor'], 'alice')
        self.refresh(replacement[1])
        with self.assertRaises(Denied): self.refresh(self.issued['refresh_token'], replacement)
        with self.store.open() as state:
            self.assertIsNotNone(state.db.execute(
                "SELECT revoked_at FROM enterprise_credential_families WHERE enrollment='alice-laptop'").fetchone()[0])

    def test_concurrent_identical_retries_rotate_once(self):
        from concurrent.futures import ThreadPoolExecutor
        replacement = pair()
        def attempt(_):
            try: return self.refresh(self.issued['refresh_token'], replacement)[2]['expires_at']
            except Denied: return None
        with ThreadPoolExecutor(4) as pool: values = list(pool.map(attempt, range(4)))
        self.assertEqual(len(set(values)), 1); self.assertIsNotNone(values[0])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('''SELECT count(*) FROM enterprise_refresh_tokens r
                JOIN enterprise_credential_families f ON f.id=r.family WHERE f.enrollment='alice-laptop' ''').fetchone()[0], 2)

    def test_idle_and_absolute_expiry_are_enforced(self):
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_refresh_tokens SET idle_expires_at=%s WHERE digest=%s',
                (time.time() - 1, _digest(self.issued['refresh_token'])))
        with self.assertRaises(Denied): self.refresh(self.issued['refresh_token'])
        issued = self.store.enroll_with_refresh('acme', 'alice', 'alice-desktop', ['read'])
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_credential_families SET absolute_expires_at=%s WHERE enrollment='alice-desktop'",
                (time.time() + 90,))
        access, refresh, result = self.refresh(issued['refresh_token'])
        # Neither token of the rotated pair may outlive the original login's limit.
        self.assertLessEqual(result['expires_at'], result['session_expires_at'])
        self.assertLessEqual(result['refresh_expires_at'], result['session_expires_at'])
        self.assertLessEqual(self.store.authenticate(access)['credential_expires'], time.time() + 91)
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_credential_families SET absolute_expires_at=%s WHERE enrollment='alice-desktop'",
                (time.time() - 1,))
        with self.assertRaises(Denied): self.refresh(refresh)

    def test_operator_lifetimes_apply_to_issue_and_rotation(self):
        self.store.credential_lifetimes = {'access_ttl_seconds': 600, 'refresh_idle_seconds': 1200,
            'refresh_absolute_seconds': 1800}
        now = time.time(); issued = self.store.enroll_with_refresh('acme', 'bob', 'bob-laptop', ['read'])
        self.assertAlmostEqual(issued['expires_at'], now + 600, delta=30)
        self.assertAlmostEqual(issued['refresh_expires_at'], now + 1200, delta=30)
        self.assertAlmostEqual(issued['session_expires_at'], now + 1800, delta=30)
        result = self.refresh(issued['refresh_token'])[2]
        self.assertAlmostEqual(result['expires_at'], time.time() + 600, delta=30)
        self.assertEqual(result['session_expires_at'], issued['session_expires_at'])

    def test_deprovisioned_principal_and_revoked_delegation_cannot_refresh(self):
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_principals SET active=0 WHERE tenant='acme' AND id='alice'")
        with self.assertRaises(Denied): self.refresh(self.issued['refresh_token'])
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_principals SET active=1 WHERE tenant='acme' AND id='alice'")
        # Denial does not spend the token: it was not a rotation.
        access, _, _ = self.refresh(self.issued['refresh_token'])
        self.assertEqual(self.store.authenticate(access)['actor'], 'alice')
        self.store.set_delegation('acme', 'bob', 'alice', ['read'], ['maple'], True)
        delegated = self.store.enroll_with_refresh('acme', 'bob', 'bob-for-alice', ['read'], acting_for='alice')
        self.store.set_delegation('acme', 'bob', 'alice', ['read'], ['maple'], False)
        with self.assertRaises(Denied): self.refresh(delegated['refresh_token'])

    def test_revocation_is_family_wide_idempotent_and_audited(self):
        access, refresh, _ = self.refresh(self.issued['refresh_token'])
        self.assertEqual(self.store.revoke_credential_family(access, request_id='r1'), {'revoked': True})
        with self.assertRaises(Denied): self.store.authenticate(access)
        with self.assertRaises(Denied): self.refresh(refresh)
        # Expired/revoked tokens of the family and unknown tokens are acknowledged.
        for token in (access, refresh, self.issued['refresh_token'], secrets.token_urlsafe(48)):
            self.assertEqual(self.store.revoke_credential_family(token), {'revoked': True})
        dispositions = [row['disposition'] for row in self.audit() if row['operation'] == 'credential_revocation']
        self.assertEqual(dispositions, ['applied', 'already_revoked', 'already_revoked', 'already_revoked'])
        self.assertEqual(self.store.authenticate(self.tokens['alice'])['actor'], 'alice')

    def test_pre_migration_credential_keeps_working_but_cannot_refresh(self):
        legacy = self.tokens['bob']
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_credentials SET family=NULL WHERE digest=%s', (_digest(legacy),))
        self.assertEqual(self.store.authenticate(legacy)['actor'], 'bob')
        with self.assertRaises(Denied): self.refresh(legacy)
        self.store.revoke_credential_family(legacy)
        with self.assertRaises(Denied): self.store.authenticate(legacy)
        self.assertEqual(self.store.authenticate(self.tokens['alice'])['actor'], 'alice')

    def test_http_routes_separate_access_and_refresh_tokens(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        store = self.store
        class Registry:
            def store_for_token(self, token): return store
        client = TestClient(create_app(Registry(), allowed_hosts=['testserver']))
        access, refresh = self.issued['access_token'], self.issued['refresh_token']
        def renew(token, **extra):
            body = {'refresh_token': token, 'replacement_access_token': secrets.token_urlsafe(48),
                    'replacement_refresh_token': secrets.token_urlsafe(48), **extra}
            return client.post('/enterprise/v3/auth/renew', json=body)
        # An access token is not a refresh token, whether in the body or as Bearer.
        self.assertEqual(renew(access).status_code, 404)
        legacy = client.post('/enterprise/v3/auth/renew', json={'replacement_token': secrets.token_urlsafe(48)},
            headers={'Authorization': 'Bearer ' + access})
        self.assertEqual(legacy.status_code, 400)
        # A refresh token is not an API credential.
        for method, path in (('GET', '/enterprise/v1/status'), ('GET', '/enterprise/v3/auth/credential'),
                             ('POST', '/enterprise/v3/auth/rotate')):
            response = client.request(method, path, headers={'Authorization': 'Bearer ' + refresh},
                json={} if method == 'POST' else None)
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(client.get('/enterprise/v1/status', headers={'Authorization': 'Bearer ' + access}).status_code, 200)
        self.assertEqual(renew(refresh).status_code, 200)
        self.assertEqual(renew(refresh, extra_field=1).status_code, 400)
        # Revocation: Bearer access or RFC 7009 body token, idempotent, never an oracle.
        for headers, body in (({'Authorization': 'Bearer ' + access}, {}), ({}, {'token': refresh}),
                              ({}, {'token': secrets.token_urlsafe(48)})):
            response = client.post('/enterprise/v3/auth/revoke', json=body, headers=headers)
            self.assertEqual((response.status_code, response.json()), (200, {'revoked': True}))
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(client.post('/enterprise/v3/auth/revoke', json={}).status_code, 400)

    def test_raw_tokens_never_reach_audit_or_credential_rows(self):
        tokens = [self.issued['access_token'], self.issued['refresh_token']]
        access, refresh, _ = self.refresh(self.issued['refresh_token']); tokens += [access, refresh]
        replay = pair(); self.refresh(refresh, replay); self.refresh(refresh, replay); tokens += list(replay)
        with self.assertRaises(Denied): self.refresh(self.issued['refresh_token'])
        self.store.revoke_credential_family(replay[0])
        with self.store.open() as state:
            rows = []
            for table in ('enterprise_audit', 'enterprise_credentials', 'enterprise_refresh_tokens',
                          'enterprise_credential_families'):
                rows += [str(dict(row)) for row in state.db.execute('SELECT * FROM ' + table)]
        operations = {row for row in rows if 'credential_' in row}
        self.assertTrue(operations)
        for token in tokens:
            self.assertFalse(any(token in row for row in rows))


if __name__ == '__main__':
    unittest.main()
