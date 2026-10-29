"""GitHub App team sync and the device-flow identity link (docs/ACCESS_MODEL.md section 3, PR C).

A fake GitHub API stands in for every call (installation tokens, teams, members,
device flow, /user); nothing reaches GitHub. Synthetic principals and a disposable
PostgreSQL schema only.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Conflict, Denied
from agenthub.github_sync import (GitHubApp, GitHubReconciler, InvalidSignature, ReplayedDelivery, verify_signature,
                                  webhook)
from test_cloud_postgres import PostgresFixture, SERVICES

FULL = ['ingest', 'read', 'source_read', 'feedback', 'correct', 'curate', 'withdraw', 'share', 'manage', 'policy']
API = 'https://api.github.test'
WEB = 'https://github.test'
ORG = 'example-org'
INSTALLATION = 4242
APP_ID = 777
SECRET = 'synthetic-webhook-secret'


class Response:
    def __init__(self, status, value, headers=None):
        self.status_code = status
        self._value = value
        self.content = json.dumps(value).encode()
        self.headers = headers or {}

    def json(self):
        return self._value


class FakeGitHub:
    """The handful of GitHub endpoints the App uses, with in-memory state."""

    def __init__(self, public_key):
        self.public_key = public_key
        self.teams = {10: {'slug': 'platform', 'name': 'Platform', 'parent': None},
                      11: {'slug': 'core', 'name': 'Core', 'parent': 10}}
        # Direct members per team; reads include child-team members, as GitHub does.
        self.direct = {10: {101: 'alice-gh'}, 11: {102: 'carol-gh'}}
        self.reachable = True
        self.devices = {}
        self.tokens = {}
        self.calls = []
        self.token_requests = []

    def _members(self, team_id):
        members = dict(self.direct.get(team_id, {}))
        for child, team in self.teams.items():
            if team['parent'] == team_id:
                members.update(self._members(child))
        return members

    def _check_installation_token(self, headers):
        return headers.get('Authorization') == 'Bearer installation-token'

    def get(self, url, headers=None, timeout=None, allow_redirects=None):
        self.calls.append(('GET', url))
        if not self.reachable:
            raise ConnectionError('unreachable')
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        if path == '/user':
            user = self.tokens.get(headers.get('Authorization', '').removeprefix('Bearer '))
            return Response(200, {'id': user[0], 'login': user[1]}) if user else Response(401, {})
        if not self._check_installation_token(headers):
            return Response(401, {})
        if path == '/orgs/%s/teams' % ORG:
            # One team per page, to exercise Link pagination.
            page = int(query.get('page', ['1'])[0])
            ids = sorted(self.teams)
            rows = [{'id': i, 'slug': self.teams[i]['slug'], 'name': self.teams[i]['name'],
                     'parent': {'id': self.teams[i]['parent']} if self.teams[i]['parent'] else None}
                    for i in ids[page - 1:page]]
            headers = {}
            if page < len(ids):
                headers['Link'] = '<%s/orgs/%s/teams?per_page=100&page=%d>; rel="next"' % (API, ORG, page + 1)
            return Response(200, rows, headers)
        for team_id, team in self.teams.items():
            if path == '/orgs/%s/teams/%s/members' % (ORG, team['slug']):
                return Response(200, [{'id': i, 'login': login} for i, login in sorted(self._members(team_id).items())])
        return Response(404, {})

    def post(self, url, headers=None, json=None, data=None, timeout=None, allow_redirects=None):
        self.calls.append(('POST', url))
        if not self.reachable:
            raise ConnectionError('unreachable')
        path = urlsplit(url).path
        if path == '/app/installations/%d/access_tokens' % INSTALLATION:
            from joserfc import jwt
            claims = jwt.decode(headers['Authorization'].removeprefix('Bearer '), self.public_key).claims
            self.token_requests.append(json)
            if claims['iss'] != str(APP_ID):
                return Response(401, {})
            return Response(201, {'token': 'installation-token', 'expires_at': '2099-01-01T00:00:00Z'})
        if path == '/login/device/code':
            code = 'device-%d' % (len(self.devices) + 1)
            self.devices[code] = None
            return Response(200, {'device_code': code, 'user_code': 'WXYZ-%04d' % len(self.devices),
                                  'verification_uri': WEB + '/login/device', 'expires_in': 900, 'interval': 5})
        if path == '/login/oauth/access_token':
            user = self.devices.get(data['device_code'], 'missing')
            if user == 'missing':
                return Response(200, {'error': 'incorrect_device_code'})
            if user is None:
                return Response(200, {'error': 'authorization_pending'})
            token = 'user-token-%d' % user[0]
            self.tokens[token] = user
            return Response(200, {'access_token': token, 'token_type': 'bearer'})
        return Response(404, {})

    def approve(self, device_code, user_id, login):
        self.devices[device_code] = (user_id, login)


class Registry:
    def __init__(self, store, app):
        self.store = store
        self.github_app = app

    def resolve(self, tenant):
        return self.store


def sign(body):
    return 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@unittest.skipUnless(SERVICES, 'explicit real PostgreSQL services manifest required')
class GitHubSyncTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        from joserfc.jwk import RSAKey
        key = RSAKey.generate_key(2048)
        self.files = tempfile.TemporaryDirectory(prefix='vaelius-github-')
        folder = Path(self.files.name)
        for name, value in (('app.pem', key.as_pem(private=True).decode()), ('webhook.secret', SECRET)):
            fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(value)
        self.github = FakeGitHub(RSAKey.import_key(key.as_pem(private=False)))
        self.app = GitHubApp({'app_id': APP_ID, 'client_id': 'Iv1.synthetic', 'private_key_file': str(folder / 'app.pem'),
                              'webhook_secret_file': str(folder / 'webhook.secret'),
                              'installations': [{'id': INSTALLATION, 'tenant': 'acme', 'org': ORG}],
                              'api_url': API, 'web_url': WEB, 'reconcile_seconds': 3600,
                              'max_staleness_seconds': 86400}, http=self.github)
        self.store.github = self.app
        for name in ('carol', 'dave'):
            self.store.create_principal('acme', name)
        self.tokens['alice'] = self.store.enroll('acme', 'alice', 'alice-full', FULL)
        for name in ('bob', 'carol', 'dave'):
            self.tokens[name] = self.store.enroll('acme', name, name + '-full', FULL)
        self.ctx = {name: self.store.authenticate(self.tokens[name]) for name in ('alice', 'bob', 'carol', 'dave')}
        self.admin = self.store.authenticate(self.tokens['admin'])
        self.store.create_workspace(self.ctx['alice'], {'name': 'teamspace'})
        self.deliveries = 0

    def tearDown(self):
        self.postgres_teardown()
        self.files.cleanup()

    # Helpers -------------------------------------------------------------------------

    def role(self, name, workspace='teamspace'):
        with self.store.open() as state:
            return self.store._workspace_role(state.db, self.ctx[name], workspace)[0]

    def link(self, name, user_id, login):
        started = self.store.github_link_start(self.ctx[name], {})
        code = 'device-%d' % len(self.github.devices)
        self.github.approve(code, user_id, login)
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE identity_link_flows SET next_poll=0 WHERE id=?', (started['flow'],))
        return self.store.github_link_poll(self.ctx[name], {'flow': started['flow']})

    def grant_platform(self, role='writer'):
        self.store.set_entry(self.ctx['alice'], {'workspace': 'teamspace', 'subject_kind': 'group',
                                                 'subject': 'gh-10', 'role': role})

    def client(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        return TestClient(create_app(Registry(self.store, self.app), allowed_hosts=['testserver']))

    def deliver(self, client, event, payload, *, signature=None, delivery=None):
        self.deliveries += 1
        payload = dict(payload, installation={'id': INSTALLATION})
        body = json.dumps(payload).encode()
        return client.post('/enterprise/v3/github/webhook', content=body, headers={
            'X-GitHub-Event': event, 'X-GitHub-Delivery': delivery or 'delivery-%d' % self.deliveries,
            'X-Hub-Signature-256': sign(body) if signature is None else signature,
            'Content-Type': 'application/json'})

    # Sync ------------------------------------------------------------------------------

    def test_teams_become_read_only_github_groups_with_nesting(self):
        result = self.store.github_reconcile()
        self.assertEqual(result['installations'][0]['teams'], 2)
        # The installation token is narrowed to organization Members: read.
        self.assertEqual(self.github.token_requests[0], {'permissions': {'members': 'read'}})
        with self.store.open() as state:
            groups = {r['id']: dict(r) for r in state.db.execute(
                "SELECT id,name,source,external_id,sync_state FROM groups WHERE tenant='acme' AND source='github'")}
            nested = state.db.execute("""SELECT 1 FROM group_edges WHERE group_id='gh-10' AND member_kind='group'
                AND member='gh-11'""").fetchone()
        self.assertEqual(groups['gh-10']['name'], 'gh_example-org_platform')
        self.assertEqual(groups['gh-11']['external_id'], '11')
        self.assertEqual({g['sync_state'] for g in groups.values()}, {'current'})
        self.assertIsNotNone(nested)
        self.link('alice', 101, 'alice-gh')
        self.link('carol', 102, 'carol-gh')
        self.grant_platform()
        # carol is in the child team only; she reaches a workspace naming the parent.
        self.assertEqual((self.role('alice'), self.role('carol')), (3, 2))
        with self.assertRaisesRegex(ValueError, 'synced_group_is_read_only'):
            self.store.add_group_member(self.ctx['alice'], {'group': 'gh-10', 'member_kind': 'principal',
                                                            'member': 'bob'})
        with self.assertRaisesRegex(ValueError, 'reserved_group_name'):
            self.store.create_group(self.ctx['alice'], {'name': 'gh_example-org_platform'})

    def test_unlinked_principal_gets_no_group_access(self):
        # GitHub user "dave" is a team member; principal dave has not linked.
        self.github.direct[10][104] = 'dave'
        self.store.github_reconcile()
        self.grant_platform()
        self.assertEqual(self.role('dave'), 0)
        status = self.store.github_status()
        self.assertEqual(status['unlinked_members'], 3)
        self.link('dave', 104, 'dave')
        self.assertEqual(self.role('dave'), 2)
        self.assertEqual(self.store.github_unlink(self.ctx['dave'], {})['unlinked'], True)
        self.assertEqual(self.role('dave'), 0)
        with self.store.open() as state:
            audit = [r[0] for r in state.db.execute("""SELECT disposition FROM enterprise_audit
                WHERE operation='identity_link' AND target_id='github:104' ORDER BY created""")]
        self.assertEqual(audit, ['linked', 'unlinked'])

    def test_device_flow_links_the_numeric_id_once_and_keeps_no_token(self):
        started = self.store.github_link_start(self.ctx['bob'], {})
        self.assertEqual(set(started), {'flow', 'user_code', 'verification_uri', 'expires_in', 'interval'})
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE identity_link_flows SET next_poll=0')
        self.assertEqual(self.store.github_link_poll(self.ctx['bob'], {'flow': started['flow']})['status'], 'pending')
        # Another principal cannot poll someone else's flow.
        with self.assertRaises(Denied):
            self.store.github_link_poll(self.ctx['carol'], {'flow': started['flow']})
        self.github.approve('device-1', 103, 'bob-gh')
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE identity_link_flows SET next_poll=0')
        linked = self.store.github_link_poll(self.ctx['bob'], {'flow': started['flow']})
        self.assertEqual((linked['status'], linked['user_id'], linked['login']), ('linked', 103, 'bob-gh'))
        with self.store.open() as state:
            rows = [dict(r) for r in state.db.execute('SELECT * FROM identity_links')]
            flows = state.db.execute('SELECT count(*) FROM identity_link_flows').fetchone()[0]
            dump = json.dumps([dict(r) for r in state.db.execute('SELECT * FROM enterprise_audit')], default=str)
        self.assertEqual([(r['principal'], r['external_id']) for r in rows], [('bob', '103')])
        self.assertEqual(flows, 0)
        self.assertNotIn('user-token', json.dumps(rows) + dump)
        # The same GitHub account cannot link to a second principal.
        with self.assertRaises(Conflict):
            self.link('carol', 103, 'bob-gh')
        # A typed login never links: the poll accepts only the opaque flow id.
        with self.assertRaises(ValueError):
            self.store.github_link_poll(self.ctx['carol'], {'flow': started['flow'], 'login': 'bob-gh'})
        self.assertEqual(self.store.github_identity(self.ctx['carol'], {}), {'provider': 'github', 'linked': False})
        # Relinking replaces the previous account and is audited.
        self.assertEqual(self.link('bob', 105, 'bob-second')['user_id'], 105)
        with self.store.open() as state:
            active = state.db.execute("SELECT external_id FROM identity_links WHERE principal='bob' AND active=1").fetchall()
            audit = [r[0] for r in state.db.execute("""SELECT disposition FROM enterprise_audit
                WHERE operation='identity_link' ORDER BY created""")]
        self.assertEqual([r[0] for r in active], ['105'])
        self.assertIn('relinked', audit)
        self.assertIn('refused_linked_elsewhere', audit)

    def test_login_rename_keeps_the_link_by_id(self):
        self.store.github_reconcile()
        self.link('carol', 102, 'carol-gh')
        self.grant_platform()
        self.github.direct[11] = {102: 'carol-renamed'}
        self.store.github_reconcile()
        self.assertEqual(self.role('carol'), 2)
        self.assertEqual(self.store.github_identity(self.ctx['carol'], {})['login'], 'carol-renamed')

    # Webhooks --------------------------------------------------------------------------

    def test_webhook_member_added_and_removed(self):
        self.store.github_reconcile()
        self.link('bob', 103, 'bob-gh')
        self.grant_platform()
        client = self.client()
        self.assertEqual(self.role('bob'), 0)
        self.github.direct[10][103] = 'bob-gh'
        response = self.deliver(client, 'membership', {'action': 'added', 'scope': 'team',
                                                       'member': {'id': 103, 'login': 'bob-gh'},
                                                       'team': {'id': 10, 'slug': 'platform'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.role('bob'), 2)
        del self.github.direct[10][103]
        response = self.deliver(client, 'membership', {'action': 'removed', 'scope': 'team',
                                                       'member': {'id': 103, 'login': 'bob-gh'},
                                                       'team': {'id': 10, 'slug': 'platform'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.role('bob'), 0)
        # Organization removal applies from the delivery itself, before any refetch.
        self.github.direct[10][103] = 'bob-gh'
        self.store.github_reconcile()
        self.assertEqual(self.role('bob'), 2)
        self.github.reachable = False
        response = self.deliver(client, 'organization', {'action': 'member_removed',
                                                         'membership': {'user': {'id': 103, 'login': 'bob-gh'}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.role('bob'), 0)

    def test_bad_or_missing_signature_is_rejected_and_applies_nothing(self):
        self.store.github_reconcile()
        self.link('bob', 103, 'bob-gh')
        self.grant_platform()
        self.github.direct[10][103] = 'bob-gh'
        client = self.client()
        payload = {'action': 'added', 'team': {'id': 10, 'slug': 'platform'}}
        forged = 'sha256=' + hmac.new(b'wrong-secret', b'{}', hashlib.sha256).hexdigest()
        for signature in (forged, '', 'sha1=abc'):
            response = self.deliver(client, 'membership', payload, signature=signature)
            self.assertEqual((response.status_code, response.json()), (401, {'error': 'invalid_signature'}))
        self.assertEqual(self.role('bob'), 0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM github_webhook_deliveries').fetchone()[0], 0)
        body = b'{"installation":{"id":4242}}'
        self.assertTrue(verify_signature(SECRET, body, sign(body)))
        self.assertFalse(verify_signature(SECRET, body + b' ', sign(body)))
        with self.assertRaises(InvalidSignature):
            webhook(Registry(self.store, self.app), self.app, {'x-hub-signature-256': sign(body)[:-1] + '0',
                                                               'x-github-event': 'ping', 'x-github-delivery': 'x'}, body)

    def test_replayed_delivery_is_rejected(self):
        self.store.github_reconcile()
        client = self.client()
        payload = {'action': 'added', 'team': {'id': 10, 'slug': 'platform'}}
        first = self.deliver(client, 'membership', payload, delivery='fixed-delivery-id')
        again = self.deliver(client, 'membership', payload, delivery='fixed-delivery-id')
        self.assertEqual(first.status_code, 200)
        self.assertEqual((again.status_code, again.json()), (409, {'error': 'replayed_delivery'}))
        with self.assertRaises(ReplayedDelivery):
            self.store.github_webhook(INSTALLATION, 'membership', 'fixed-delivery-id', payload)

    def test_reconcile_fixes_drift_and_missed_webhooks(self):
        self.store.github_reconcile(now=1000)
        self.link('bob', 103, 'bob-gh')
        self.link('alice', 101, 'alice-gh')
        self.grant_platform()
        # Missed deliveries: bob added, alice removed, a team deleted.
        self.github.direct[10] = {103: 'bob-gh'}
        del self.github.teams[11]
        self.assertEqual((self.role('bob'), self.role('alice')), (0, 3))
        ticks = GitHubReconciler(Registry(self.store, self.app), self.app).tick(now=1000 + 3600)
        self.assertEqual(ticks['acme']['reconciled'], [INSTALLATION])
        self.assertEqual((self.role('bob'), self.role('alice')), (2, 3))  # alice stays admin by her own entry
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT active FROM groups WHERE id='gh-11'").fetchone()[0], 0)
            self.assertIsNone(state.db.execute("""SELECT 1 FROM group_edges WHERE group_id='gh-10'
                AND member='alice'""").fetchone())
        # A tick before the interval reconciles nothing.
        self.assertEqual(GitHubReconciler(Registry(self.store, self.app), self.app).tick(now=1000 + 3700)['acme']
                         ['reconciled'], [])

    # Staleness -------------------------------------------------------------------------

    def test_stale_group_contributes_nothing_and_admins_keep_access(self):
        self.store.github_reconcile(now=1000)
        self.link('carol', 102, 'carol-gh')
        self.grant_platform(role='admin')
        self.assertEqual(self.role('carol'), 3)
        self.github.reachable = False
        # Inside the window the outage changes nothing.
        result = self.store.github_run_due(now=1000 + 3600)
        self.assertEqual(result['expired'], [])
        self.assertEqual(self.role('carol'), 3)
        result = self.store.github_run_due(now=1000 + 86400 + 1)
        self.assertEqual(sorted(result['expired']), ['gh-10', 'gh-11'])
        self.assertEqual(self.role('carol'), 0)
        self.assertEqual(self.role('alice'), 3)  # the principal admin keeps access
        status = self.store.github_status(now=1000 + 86400 + 2)
        self.assertEqual({g['group'] for g in status['stale_groups']}, {'gh-10', 'gh-11'})
        from agenthub.access_review import collect
        report = collect(self.store)
        flagged = {g['group']: g['flags'] for g in report['workspaces']['github_groups']}
        self.assertIn('group_stale', flagged['gh-10'])
        # The next good sync restores the members.
        self.github.reachable = True
        self.store.github_reconcile(now=1000 + 86400 + 10)
        self.assertEqual(self.role('carol'), 3)

    def test_uninstalling_the_app_marks_its_groups_failed(self):
        self.store.github_reconcile()
        self.link('carol', 102, 'carol-gh')
        self.grant_platform()
        client = self.client()
        response = self.deliver(client, 'installation', {'action': 'deleted'})
        self.assertEqual(response.status_code, 200)
        with self.store.open() as state:
            states = {r[0] for r in state.db.execute("SELECT sync_state FROM groups WHERE source='github'")}
        self.assertEqual(states, {'failed'})
        self.assertEqual(self.role('carol'), 0)
        self.assertEqual(self.store.github_reconcile()['installations'][0]['status'], 'deleted')

    def test_synced_group_never_counts_as_the_last_admin(self):
        self.store.github_reconcile()
        self.link('carol', 102, 'carol-gh')
        self.grant_platform(role='admin')
        with self.assertRaisesRegex(ValueError, 'last_admin'):
            self.store.remove_entry(self.ctx['alice'], {'workspace': 'teamspace', 'subject_kind': 'principal',
                                                        'subject': 'alice'})
        self.assertEqual(self.role('alice'), 3)

    # Review and offboarding ------------------------------------------------------------

    def test_access_review_lists_links_and_offboarding_removes_them(self):
        self.store.github_reconcile()
        self.link('carol', 102, 'carol-gh')
        self.grant_platform()
        from agenthub.access_review import apply_revocation, collect, offboard
        report = collect(self.store)
        links = [line for line in report['lines'] if line['kind'] == 'identity_link']
        self.assertEqual([(l['principal'], l['target']['user_id']) for l in links], [('carol', 102)])
        groups = {g['group']: g for g in report['workspaces']['github_groups']}
        self.assertEqual((groups['gh-10']['linked_members'], groups['gh-10']['pending_identity_link']), (1, 1))
        self.assertIn('pending_identity_link', groups['gh-10']['flags'])
        access = [line for line in report['lines'] if line['kind'] == 'workspace_access' and line['principal'] == 'carol']
        self.assertEqual(access[0]['target']['groups'][0]['source'], 'github')
        self.assertEqual(apply_revocation(self.store, links[0], actor='reviewer')['status'], 'applied')
        self.assertEqual(self.role('carol'), 0)
        self.link('carol', 102, 'carol-gh')
        result = offboard(self.store, 'carol', actor='reviewer', reason='left')
        self.assertEqual(result['changes']['identity_links_deactivated'], 1)
        with self.store.open() as state:
            self.assertIsNone(state.db.execute("SELECT 1 FROM identity_links WHERE principal='carol' AND active=1")
                              .fetchone())
            self.assertIsNone(state.db.execute("SELECT 1 FROM group_closure WHERE principal='carol'").fetchone())


class GitHubConfigurationTests(unittest.TestCase):
    def test_secret_files_must_be_owner_only_and_settings_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / 'app.pem'
            key.write_text('not-a-key')
            key.chmod(0o644)
            secret = Path(folder) / 'webhook.secret'
            secret.write_text(SECRET)
            secret.chmod(0o600)
            settings = {'app_id': 1, 'client_id': 'Iv1.x', 'private_key_file': str(key),
                        'webhook_secret_file': str(secret), 'installations': [{'id': 1, 'tenant': 'acme', 'org': ORG}]}
            with self.assertRaisesRegex(ValueError, 'provider_credential_permissions'):
                GitHubApp(settings, http=object())
            key.chmod(0o600)
            app = GitHubApp(settings, http=object())
            self.assertEqual((app.reconcile_seconds, app.max_staleness_seconds), (21600, 86400))
            for bad in ({'max_staleness_seconds': 3600, 'reconcile_seconds': 7200}, {'api_url': 'http://example.test'},
                        {'unknown': 1}):
                with self.assertRaises(ValueError):
                    GitHubApp(dict(settings, **bad), http=object())


if __name__ == '__main__':
    unittest.main()
