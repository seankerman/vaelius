"""Periodic access review, attestation, idle-access hygiene and offboarding.

Synthetic principals and identities on disposable PostgreSQL: a control database
per class and a tenant schema per test. No provider, broker or real directory.
"""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
import uuid
from unittest import mock

from test_cloud_postgres import PostgresFixture, SERVICES
from agentclient.enterprise_contract import VERSION

ISSUER = 'https://idp.example.test'
DAY = 86400


class ConfigurationTests(unittest.TestCase):
    def test_hygiene_is_off_by_default_and_configuration_is_validated(self):
        from agenthub.access_review import settings_from_runtime
        value = settings_from_runtime({})
        self.assertEqual(value['stale_days'], 90)
        self.assertFalse(value['hygiene']['enabled'])
        self.assertFalse(value['hygiene']['deactivate_bindings'])
        enabled = settings_from_runtime({'access_review': {'stale_days': 30, 'hygiene': {
            'enabled': True, 'credential_idle_days': 45}}})
        self.assertEqual((enabled['stale_days'], enabled['hygiene']['credential_idle_days']), (30, 45))
        for bad in ({'unknown': 1}, {'stale_days': 0}, {'stale_days': '90'}, {'hygiene': {'enabled': 'yes'}},
                    {'hygiene': {'credential_idle_days': 9999}}, {'hygiene': {'surprise': True}}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings_from_runtime({'access_review': bad})

    def test_csv_cells_cannot_start_spreadsheet_formulas(self):
        from agenthub.access_review import to_csv
        text = to_csv([{'line_id': 'ar_' + '0' * 24, 'principal': '=HYPERLINK("x")', 'principal_active': True,
                        'kind': 'binding', 'target': {'subject': '@sum(1)'}, 'actions': [],
                        'last_used_at': None, 'flags': ['never_used']}])
        row = list(csv.DictReader(io.StringIO(text)))[0]
        self.assertEqual(row['principal'], '\'=HYPERLINK("x")')
        self.assertTrue(row['target'].startswith('subject=@'))


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL required')
class AccessReviewPostgresTests(PostgresFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from agenthub.postgres import connect, migrate
        cls.manifest = json.loads(Path(SERVICES).read_text())
        cls.control_name = 'access_review_control_' + uuid.uuid4().hex[:10]
        with connect(cls.manifest['admin_dsn'], autocommit=True) as db:
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.control_name)))
        cls.control_dsn = make_conninfo(cls.manifest['admin_dsn'], dbname=cls.control_name)
        migrate(cls.control_dsn, control=True)

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        # Plain admin session: dropping waits for terminated backends to exit.
        with psycopg.connect(cls.manifest['admin_dsn'], autocommit=True, connect_timeout=5) as db:
            db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(cls.control_name)))

    def setUp(self):
        from agenthub.cloud_identity import IdentityBindings
        from agenthub.postgres import TenantRegistry
        self.tenant = 't' + uuid.uuid4().hex[:12]
        self.postgres_setup(tenant_id=self.tenant, bootstrap=False)
        self.addCleanup(self.postgres_teardown)
        self.registry = TenantRegistry(self.control_dsn, Path(self.temp.name) / 'stores')
        self.registry.register(self.tenant, self.dsn)
        self.store = self.registry.resolve(self.tenant)
        self.control_admin = self.registry.open_control
        self.bindings = IdentityBindings(self.registry.open_control)
        store, tenant = self.store, self.tenant
        store.create_organization(tenant)
        for name in ('admin', 'auditor', 'alice', 'bob', 'carol', 'dave', 'svc'):
            store.create_principal(tenant, name, settings_admin=name in ('admin', 'auditor'))
        for project in ('maple', 'oak'):
            store.create_project(tenant, project)
        for name in ('alice', 'bob', 'dave'):
            store.set_membership(tenant, 'maple', name, True)
        owner_actions = ['ingest', 'read', 'source_read', 'correct', 'withdraw', 'policy', 'feedback']
        self.tokens = {
            'admin': store.enroll(tenant, 'admin', 'admin-console', ['read', 'settings', 'audit', 'policy']),
            'auditor': store.enroll(tenant, 'auditor', 'auditor-console', ['read', 'settings', 'audit']),
            'bob': store.enroll(tenant, 'bob', 'bob-cli', owner_actions),
            'carol': store.enroll(tenant, 'carol', 'carol-cli', ['read']),
            'dave': store.enroll(tenant, 'dave', 'dave-cli', owner_actions)}
        self.bindings.bind(ISSUER, 'sub-alice', tenant, 'alice')
        self.alice = store.enroll_with_refresh(tenant, 'alice', 'alice-laptop', owner_actions,
            external_identity={'issuer': ISSUER, 'subject': 'sub-alice', 'provider': 'native'})
        self.tokens['alice'] = self.alice['access_token']
        self.bindings.bind(ISSUER, 'sub-bob', tenant, 'bob')
        self.bindings.bind(ISSUER, '=cmd|calc', tenant, 'ghost')
        store.set_delegation(tenant, 'svc', 'alice', ['read'], ['maple'], True)
        self.tokens['svc'] = store.enroll(tenant, 'svc', 'svc-agent', ['read'], acting_for='alice')
        self.ctx = {name: store.authenticate(token) for name, token in self.tokens.items()}
        # Directory: bob reaches oak only through a SCIM group.
        store.bind_directory_user(self.ctx['admin'], 'ext-bob', 'bob')
        store.bind_directory_user(self.ctx['admin'], 'ext-carol', 'carol')
        store.bind_directory_group(self.ctx['admin'], 'grp-eng', 'oak')
        for key, resource, value in (
                ('u1', 'Users', {'id': 'ext-bob', 'userName': 'ext-bob', 'active': True}),
                ('u2', 'Users', {'id': 'ext-carol', 'userName': 'ext-carol', 'active': True}),
                ('g1', 'Groups', {'id': 'grp-eng', 'displayName': 'Engineering', 'members': [{'value': 'ext-bob'}]})):
            store.apply_directory_event(self.ctx['admin'], resource=resource, value=value, sequence=1, key=key)
        store.enroll_connection(self.ctx['alice'], 'notes-' + tenant, 'docs', 'maple', ['document'],
                                visibility='team', reader_ids=['bob', 'dave'])
        self.body = 'Synthetic body text ' + uuid.uuid4().hex
        self.source = store.ingest(self.ctx['alice'], {'version': VERSION, 'external_id': 'note-1',
            'session': 's', 'turn': '1', 'project': 'maple', 'kind': 'Stop', 'body': self.body,
            'occurred_at': 12345, 'visibility': 'private'})['source_id']
        with store.open() as state, state.db:
            state.db.execute('''INSERT INTO backend_releases VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                ('release-1', 'doc-released', 'doc-original', 'rev-1', tenant, 'alice',
                 json.dumps(['bob', 'dave']), '{}', time.time()))

    # --- helpers -----------------------------------------------------------------
    def sql(self, statement, params=()):
        with self.store.open() as state, state.db:
            cursor = state.db.execute(statement, params)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def backdate(self, principal, days):
        then = time.time() - days * DAY
        self.sql('UPDATE enterprise_credentials SET created=%s WHERE principal=%s', (then, principal))
        self.sql('UPDATE enterprise_credential_families SET created=%s WHERE principal=%s', (then, principal))
        self.sql('UPDATE enterprise_audit SET created=%s WHERE actor=%s', (then, principal))

    def denied(self, token):
        from agenthub.enterprise import Denied
        with self.assertRaises(Denied):
            self.store.authenticate(token)

    def report(self, **kwargs):
        from agenthub.access_review import collect
        return collect(self.store, **kwargs)

    def lines(self, report, principal, kind):
        return [item for item in report['lines'] if item['principal'] == principal and item['kind'] == kind]

    def person(self, report, principal):
        return next(item for item in report['principals'] if item['principal'] == principal)

    def audits(self, operation):
        return self.sql('SELECT actor,disposition,target_id FROM enterprise_audit WHERE operation=%s ORDER BY id',
                        (operation,))

    def client(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        return TestClient(create_app(self.registry, allowed_hosts=['testserver']))

    def post(self, client, user, action, body=None):
        return client.post('/enterprise/v3/access-review/' + action, json=body or {},
                           headers={'Authorization': 'Bearer ' + self.tokens[user]})

    # --- report -----------------------------------------------------------------
    def test_report_inventories_access_and_flags_without_secrets_or_content(self):
        from agenthub.access_review import to_csv
        from agenthub.enterprise import _digest
        self.backdate('bob', 120)
        report = self.report(stale_days=90)
        self.assertEqual(report['control_plane'], 'read')
        alice = self.person(report, 'alice')
        self.assertEqual([(b['issuer'], b['subject']) for b in alice['identity_bindings']], [(ISSUER, 'sub-alice')])
        self.assertNotIn('stale', alice['identity_bindings'][0]['flags'])
        self.assertEqual((alice['active_access_tokens'], alice['active_refresh_sessions']), (1, 1))
        self.assertIn('source_read', alice['granted_actions'])
        self.assertTrue(alice['raw_source_read'])
        self.assertEqual(alice['privileged_actions'], ['policy', 'source_read'])
        self.assertIsNotNone(alice['last_activity_at'])
        self.assertEqual([g['project'] for g in alice['project_grants']], ['maple'])
        self.assertEqual(alice['delegations']['represented_by'][0]['principal'], 'svc')
        bob = self.person(report, 'bob')
        self.assertIn('stale', bob['flags'])
        [credential] = self.lines(report, 'bob', 'credential')
        self.assertIn('stale', credential['flags'])
        self.assertIn('raw_source_read', credential['flags'])
        [binding] = self.lines(report, 'bob', 'binding')
        self.assertEqual(set(binding['flags']), {'never_used', 'stale'})
        self.assertEqual([(g['external_id'], g['projects']) for g in bob['groups']], [('grp-eng', ['oak'])])
        self.assertEqual({g['project']: g['via'] for g in bob['project_grants']},
                         {'maple': ['explicit'], 'oak': ['directory']})
        self.assertEqual([g['document_id'] for g in bob['document_grants']], ['doc-released'])
        self.assertEqual([g['connection'] for g in bob['connection_reader_grants']], ['notes-' + self.tenant])
        svc = self.lines(report, 'svc', 'delegation')
        self.assertEqual([(d['target']['acting_for'], d['actions']) for d in svc], [('alice', ['read'])])
        self.assertIn('delegated', self.lines(report, 'svc', 'credential')[0]['flags'])
        self.assertEqual(self.lines(report, 'admin', 'settings_admin')[0]['flags'], ['privileged'])
        ghost = self.person(report, 'ghost')
        self.assertIn('unknown_principal', ghost['flags'])
        self.assertIn('orphaned:unknown_principal', self.lines(report, 'ghost', 'binding')[0]['flags'])
        self.assertGreaterEqual(report['summary']['privileged_principals'], 4)
        self.assertEqual(len({item['line_id'] for item in report['lines']}), len(report['lines']))
        text = to_csv(report['lines'])
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), len(report['lines']))
        self.assertTrue(any('subject==cmd|calc' in r['target'] for r in rows if r['principal'] == 'ghost'))
        self.assertFalse([cell for r in rows for cell in r.values() if cell[:1] in ('=', '+', '-', '@')])
        serialized = json.dumps(report) + text
        secrets_seen = list(self.tokens.values()) + [self.alice['refresh_token']]
        with self.store.open() as state:
            secrets_seen += [r[0] for r in state.db.execute('SELECT digest FROM enterprise_credentials')]
            secrets_seen += [r[0] for r in state.db.execute('SELECT digest FROM enterprise_refresh_tokens')]
            secrets_seen += [r[0] for r in state.db.execute('SELECT id FROM enterprise_credential_families')]
        secrets_seen += [_digest(token) for token in self.tokens.values()]
        for value in secrets_seen + [self.body]:
            self.assertNotIn(value, serialized)

    def test_directory_drift_and_orphaned_grants_are_flagged(self):
        # SCIM deactivates carol; a local reactivation drifts from the directory.
        self.store.apply_directory_event(self.ctx['admin'], resource='Users',
            value={'id': 'ext-carol', 'userName': 'ext-carol', 'active': False}, sequence=2, key='u3')
        self.sql("UPDATE enterprise_principals SET active=1 WHERE id='carol'")
        # dave is deactivated outside offboarding: his grants are left behind.
        self.sql("UPDATE enterprise_principals SET active=0 WHERE id='dave'")
        self.store.set_delegation(self.tenant, 'svc', 'dave', ['read'], ['maple'], True)
        report = self.report()
        self.assertIn('directory_deactivated', self.person(report, 'carol')['flags'])
        self.assertIn('directory_deactivated', self.lines(report, 'carol', 'principal')[0]['flags'])
        self.assertEqual(report['summary']['directory_deactivated'], 1)
        orphaned = {(item['principal'], item['kind']): item['reasons'] for item in report['orphaned_grants']}
        self.assertEqual(orphaned[('dave', 'project_grant')], ['inactive_principal'])
        self.assertEqual(orphaned[('dave', 'credential')], ['inactive_principal'])
        self.assertEqual(orphaned[('dave', 'document_grant')], ['inactive_principal'])
        self.assertEqual(orphaned[('dave', 'connection_reader')], ['inactive_principal'])
        self.assertEqual(orphaned[('svc', 'delegation')], ['inactive_represented_principal'])
        self.assertIn('inactive_with_access', self.person(report, 'dave')['flags'])
        self.assertFalse(self.lines(report, 'dave', 'principal'))
        self.assertEqual(orphaned[('ghost', 'binding')], ['unknown_principal'])

    def test_report_statement_count_does_not_grow_with_principals(self):
        from agenthub.postgres import PostgresConnection
        def count():
            calls = []
            original = PostgresConnection.execute
            def counted(connection, statement, params=None):
                calls.append(statement)
                return original(connection, statement, params)
            with mock.patch.object(PostgresConnection, 'execute', counted):
                self.report()
            return len(calls)
        before = count()
        for index in range(25):
            name = 'extra%02d' % index
            self.store.create_principal(self.tenant, name)
            self.store.set_membership(self.tenant, 'maple', name, True)
            self.store.enroll(self.tenant, name, name + '-cli', ['read'])
        self.assertEqual(count(), before)

    # --- authenticated admin route ---------------------------------------------------
    def test_route_requires_tenant_administrator_with_audit_or_policy(self):
        client = self.client()
        response = self.post(client, 'admin', 'report', {'stale_days': 30})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['stale_days'], 30)
        self.assertEqual(self.post(client, 'auditor', 'report').status_code, 200)
        csv_response = self.post(client, 'auditor', 'report', {'format': 'csv'})
        self.assertTrue(csv_response.json()['content'].startswith('line_id,principal,'))
        # `policy` alone (held by ordinary owners), delegated credentials and
        # anonymous callers never reach the tenant-wide report.
        for user in ('alice', 'bob', 'svc'):
            with self.subTest(user=user):
                self.assertEqual(self.post(client, user, 'report').status_code, 404)
        self.assertEqual(client.post('/enterprise/v3/access-review/report', json={}).status_code, 404)
        self.assertEqual(client.get('/enterprise/v3/access-review/report',
            headers={'Authorization': 'Bearer ' + self.tokens['admin']}).status_code, 404)
        self.assertEqual(self.post(client, 'admin', 'report', {'surprise': 1}).status_code, 400)
        # A demoted administrator loses access at the next request.
        self.sql("UPDATE enterprise_principals SET settings_admin=0 WHERE id='auditor'")
        self.assertEqual(self.post(client, 'auditor', 'report').status_code, 404)
        self.assertIn({'actor': 'admin', 'disposition': 'generated', 'target_id': None},
                      self.audits('access_review_report'))

    def test_attestation_records_decisions_and_revokes_through_existing_paths(self):
        from agenthub.access_review import decide
        client = self.client()
        opened = self.post(client, 'admin', 'reviews').json()
        review = opened['review_id']
        report = self.report()
        line = lambda principal, kind, **match: next(item['line_id'] for item in self.lines(report, principal, kind)
            if all(item['target'].get(k) == v for k, v in match.items()))
        decide_as = lambda user, line_id, decision, comment='': self.post(client, user, 'decisions',
            {'review_id': review, 'line_id': line_id, 'decision': decision, 'comment': comment})

        approved = decide_as('admin', line('alice', 'principal'), 'approve', 'Synthetic quarterly review: still on team')
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual((approved.json()['reviewer'], approved.json()['decision']), ('admin', 'approve'))
        self.assertTrue(decide_as('admin', line('alice', 'principal'), 'approve').json()['duplicate'])
        self.assertEqual(decide_as('admin', line('alice', 'principal'), 'revoke').status_code, 409)
        self.assertEqual(decide_as('admin', line('admin', 'settings_admin'), 'approve').status_code, 404)
        self.assertEqual(decide_as('auditor', line('bob', 'credential'), 'revoke').status_code, 404)
        self.assertEqual(decide_as('admin', line('bob', 'credential'), 'approve',
                                   'password=' + 'hunter2-synthetic-' + 'x' * 12).status_code, 400)
        self.assertEqual(decide_as('alice', line('bob', 'credential'), 'revoke').status_code, 404)

        revoked = decide_as('admin', line('bob', 'credential'), 'revoke', 'left the project').json()
        self.assertEqual(revoked['outcome']['status'], 'applied')
        self.denied(self.tokens['bob'])
        refresh = self.lines(report, 'alice', 'credential')[0]['line_id']
        self.assertEqual(decide_as('admin', refresh, 'revoke').json()['outcome']['refresh_families_revoked'], 1)
        self.denied(self.tokens['alice'])
        from agenthub.enterprise import Denied
        with self.assertRaises(Denied):
            self.store.refresh_credential(self.alice['refresh_token'], replacement_access_token=secrets.token_urlsafe(48),
                                          replacement_refresh_token=secrets.token_urlsafe(48))
        explicit = decide_as('admin', line('bob', 'project_grant', project='maple'), 'revoke').json()
        self.assertEqual(explicit['outcome']['status'], 'applied')
        directory = decide_as('admin', line('bob', 'project_grant', project='oak'), 'revoke').json()
        self.assertEqual(directory['outcome']['status'], 'directory_grant_remains')
        self.assertEqual(self.sql("SELECT project,active FROM enterprise_memberships WHERE principal='bob' ORDER BY project"),
                         [{'project': 'maple', 'active': 0}, {'project': 'oak', 'active': 1}])
        self.assertEqual(decide_as('admin', line('svc', 'delegation'), 'revoke').json()['outcome']['status'], 'applied')
        self.denied(self.tokens['svc'])
        self.assertEqual(decide_as('admin', line('bob', 'document_grant'), 'revoke').json()['outcome']['status'], 'applied')
        self.assertEqual(json.loads(self.sql("SELECT readers FROM backend_releases")[0]['readers']), ['dave'])
        reader = decide_as('admin', line('bob', 'connection_reader'), 'revoke').json()
        self.assertEqual(reader['outcome']['status'], 'requires_owner')
        self.assertEqual(decide_as('admin', line('auditor', 'settings_admin'), 'revoke').json()['outcome']['status'], 'applied')
        self.assertEqual(self.post(client, 'auditor', 'report').status_code, 404)

        # The serving route cannot write the control plane; the operator finishes it.
        binding = line('bob', 'binding')
        pending = decide_as('admin', binding, 'revoke', 'unused login').json()
        self.assertEqual(pending['outcome']['status'], 'pending_operator')
        finished = decide(self.store, review, binding, 'revoke', reviewer='security-officer',
                          control_admin=self.control_admin)
        self.assertEqual((finished['outcome']['status'], finished['reviewer'], finished['comment']),
                         ('applied', 'admin', 'unused login'))
        with self.registry.open_control() as db:
            self.assertEqual(db.execute('SELECT active FROM cloud_identity_bindings WHERE subject=%s',
                                        ('sub-bob',)).fetchone()['active'], 0)

        evidence = self.post(client, 'admin', 'evidence', {'review_id': review}).json()
        decided = {item['line_id']: item for item in evidence['lines'] if item['decision'] != 'pending'}
        self.assertEqual(decided[line('alice', 'principal')]['comment'], 'Synthetic quarterly review: still on team')
        self.assertEqual(decided[line('alice', 'principal')]['reviewer'], 'admin')
        self.assertIsNotNone(decided[binding]['applied'])
        self.assertEqual(evidence['summary']['approve'], 1)
        self.assertEqual(evidence['summary']['revoke'], 9)
        self.assertEqual(evidence['summary']['revocations_unfinished'], 0)
        self.assertEqual(evidence['summary']['lines'], opened['line_count'])
        exported = self.post(client, 'admin', 'evidence', {'review_id': review, 'format': 'csv'}).json()['content']
        self.assertIn('decision,reviewer,decided,comment,outcome,applied', exported.splitlines()[0])
        closed = self.post(client, 'admin', 'close', {'review_id': review}).json()
        self.assertIsNotNone(closed['closed'])
        self.assertEqual(decide_as('admin', line('carol', 'principal'), 'approve').status_code, 409)
        decisions = self.audits('access_review_decision')
        self.assertEqual(len(decisions), 10)
        self.assertTrue(all(row['actor'] == 'admin' for row in decisions))
        self.assertIn(('admin', 'applied', line('bob', 'credential')),
                      [tuple(r.values()) for r in self.audits('access_review_revoke')])
        self.assertTrue(self.audits('credential_revocation'))

    def test_revoking_a_principal_line_offboards_the_principal(self):
        from agenthub.access_review import open_review, decide
        review = open_review(self.store, opened_by='security-officer')['review_id']
        from agenthub.enterprise import Denied
        principal_line = self.lines(self.report(), 'dave', 'principal')[0]['line_id']
        with self.assertRaises(Denied):
            decide(self.store, review, principal_line, 'revoke', reviewer='dave')
        result = decide(self.store, review, principal_line, 'revoke', reviewer='security-officer',
                        comment='departed', control_admin=self.control_admin)
        self.assertEqual(result['outcome']['status'], 'applied')
        self.denied(self.tokens['dave'])
        self.assertEqual(self.sql("SELECT active FROM enterprise_principals WHERE id='dave'")[0]['active'], 0)

    # --- hygiene -------------------------------------------------------------------
    def test_hygiene_dry_run_then_apply_expires_only_idle_access(self):
        from agenthub.access_review import hygiene
        self.backdate('bob', 120)
        self.backdate('carol', 120)
        dry = hygiene(self.store, credential_idle_days=90)
        self.assertTrue(dry['dry_run'])
        self.assertEqual(sorted(item['principal'] for item in dry['credentials']), ['bob', 'carol'])
        self.store.authenticate(self.tokens['bob'])
        self.assertFalse(self.audits('access_hygiene'))
        # carol is used again between the scan and the apply: the recheck keeps her.
        real = hygiene.__globals__['collect']
        def collect_then_use(*args, **kwargs):
            value = real(*args, **kwargs)
            self.sql("UPDATE enterprise_credentials SET created=%s WHERE principal='carol'", (time.time(),))
            return value
        with mock.patch.dict(hygiene.__globals__, collect=collect_then_use):
            applied = hygiene(self.store, credential_idle_days=90, apply=True)
        outcomes = {item['principal']: item['outcome']['status'] for item in applied['credentials']}
        self.assertEqual(outcomes, {'bob': 'applied', 'carol': 'skipped_recent_use'})
        self.denied(self.tokens['bob'])
        for user in ('admin', 'alice', 'carol'):
            self.store.authenticate(self.tokens[user])
        self.assertEqual([tuple(r.values()) for r in self.audits('access_credential_revocation')],
                         [('access_hygiene', 'applied', 'bob:bob-cli')])
        self.assertEqual(len(self.audits('access_hygiene')), 1)
        # Binding deactivation is opt-in, needs control-plane credentials, and
        # never-used bindings are only included on request.
        options = dict(credential_idle_days=90, deactivate_bindings=True, binding_idle_days=30)
        self.assertEqual(hygiene(self.store, **options)['bindings'], [])
        with self.assertRaisesRegex(ValueError, 'control_plane_admin'):
            hygiene(self.store, include_never_used_bindings=True, apply=True, **options)
        result = hygiene(self.store, include_never_used_bindings=True, apply=True,
                         control_admin=self.control_admin, **options)
        self.assertEqual(sorted(item['subject'] for item in result['bindings']), ['=cmd|calc', 'sub-bob'])
        with self.registry.open_control() as db:
            active = {r['subject']: r['active'] for r in db.execute(
                'SELECT subject,active FROM cloud_identity_bindings WHERE tenant=%s', (self.tenant,))}
        self.assertEqual(active, {'sub-alice': 1, 'sub-bob': 0, '=cmd|calc': 0})

    # --- offboarding ------------------------------------------------------------------
    def test_offboarding_removes_every_access_path_keeps_content_and_is_idempotent(self):
        from agenthub.access_review import offboard
        before = self.sql('SELECT count(*) AS n FROM enterprise_sources WHERE active=1')[0]['n']
        plan = offboard(self.store, 'alice', actor='security-officer', reason='synthetic departure', apply=False)
        self.assertTrue(plan['dry_run'])
        self.assertEqual(plan['plan']['delegations'], [{'principal': 'svc', 'acting_for': 'alice'}])
        self.assertEqual(plan['plan']['identity_bindings'][0]['subject'], 'sub-alice')
        self.store.authenticate(self.tokens['alice'])
        result = offboard(self.store, 'alice', actor='security-officer', reason='synthetic departure',
                          control_admin=self.control_admin)
        self.assertEqual(result['status'], 'applied')
        changes = result['changes']
        self.assertTrue(changes['principal_deactivated'])
        self.assertEqual(changes['refresh_families_revoked'], 2)  # alice's login and svc acting for alice
        self.assertEqual(changes['delegations_revoked'], 1)
        self.assertEqual(changes['project_grants_removed'], 1)
        self.assertEqual(changes['identity_bindings_deactivated'], 1)
        for token in (self.tokens['alice'], self.tokens['svc']):
            self.denied(token)
        from agenthub.enterprise import Denied
        with self.assertRaises(Denied):
            self.store.refresh_credential(self.alice['refresh_token'], replacement_access_token=secrets.token_urlsafe(48),
                                          replacement_refresh_token=secrets.token_urlsafe(48))
        self.assertEqual(self.sql("SELECT count(*) AS n FROM enterprise_memberships WHERE principal='alice' AND active=1")[0]['n'], 0)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM cloud_explicit_memberships WHERE principal='alice'")[0]['n'], 0)
        self.assertEqual(self.sql('SELECT count(*) AS n FROM enterprise_sources WHERE active=1')[0]['n'], before)
        self.assertEqual(self.sql('SELECT body FROM memories WHERE id=%s', (self.source,))[0]['body'], self.body)
        remaining = result['remaining']
        self.assertEqual(remaining['owned_sources']['active'], 1)
        self.assertEqual([c['connection'] for c in remaining['owned_connections']], ['notes-' + self.tenant])
        self.assertEqual((remaining['live_access_tokens'], remaining['identity_bindings']), (0, []))
        again = offboard(self.store, 'alice', actor='security-officer', reason='synthetic departure',
                         control_admin=self.control_admin)
        self.assertEqual(again['status'], 'already_offboarded')
        self.assertFalse(any(again['changes'].values()))
        self.assertEqual([r['disposition'] for r in self.audits('offboarding')], ['applied', 'already_offboarded'])
        # Nothing reviewable remains for alice except grants others hold on her behalf.
        self.assertEqual([item['kind'] for item in self.report()['lines'] if item['principal'] == 'alice'], [])

    def test_offboarding_directory_member_without_control_credentials_reports_what_remains(self):
        from agenthub.access_review import offboard
        result = offboard(self.store, 'bob', actor='security-officer', reason='synthetic departure')
        self.assertEqual(result['changes']['project_grants_removed'], 2)
        self.assertEqual(result['changes']['release_reader_grants_removed'], 1)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM enterprise_memberships WHERE principal='bob' AND active=1")[0]['n'], 0)
        self.assertEqual(json.loads(self.sql('SELECT readers FROM backend_releases')[0]['readers']), ['dave'])
        remaining = result['remaining']
        self.assertEqual(remaining['identity_bindings'][0]['subject'], 'sub-bob')
        self.assertEqual(remaining['connection_reader_grants'], ['notes-' + self.tenant])
        self.assertEqual(remaining['directory_users'], [{'external_id': 'ext-bob', 'directory_active': True}])
        self.assertEqual(len(remaining['actions_required']), 3)
        self.denied(self.tokens['bob'])
        # Settings administrators lose the flag as well as their credentials.
        offboard(self.store, 'auditor', actor='security-officer', reason='synthetic role change')
        self.assertEqual(self.sql("SELECT active,settings_admin FROM enterprise_principals WHERE id='auditor'"),
                         [{'active': 0, 'settings_admin': 0}])

    # --- operator command --------------------------------------------------------------
    def test_operator_command_writes_private_report_and_receipted_offboarding(self):
        from agenthub.access_review import main
        profile = Path(self.temp.name) / 'profile'
        profile.mkdir(mode=0o700)
        for name, value in (('runtime.json', {'control_dsn': self.control_dsn, 'provider_mode': 'off'}),
                            ('operator.json', {'control_admin_dsn': self.control_dsn})):
            path = profile / name
            path.write_text(json.dumps(value)); path.chmod(0o600)
        base = ['--profile', str(profile), '--tenant', self.tenant]
        output = profile / 'reports' / 'review.csv'
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            main(['report', *base, '--format', 'csv', '--output', str(output)])
        self.assertEqual(json.loads(printed.getvalue())['format'], 'csv')
        self.assertEqual(output.stat().st_mode & 0o077, 0)
        self.assertTrue(output.read_text().startswith('line_id,principal,'))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(['hygiene', *base, '--apply'])
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            main(['offboard', *base, '--principal', 'carol', '--reason', 'synthetic departure',
                  '--reviewer', 'security-officer'])
        value = json.loads(printed.getvalue())
        self.assertEqual(value['result']['status'], 'applied')
        self.assertTrue(Path(value['receipt']).exists())
        self.denied(self.tokens['carol'])
        self.assertIn(('security-officer', 'applied', 'carol'), [tuple(r.values()) for r in self.audits('offboarding')])


if __name__ == '__main__':
    unittest.main()
