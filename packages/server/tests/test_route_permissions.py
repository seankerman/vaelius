"""Route permissions: every authenticated route has an action gate, and the
unauthenticated login, discovery, revocation and readiness routes reveal nothing
about which tenants exist or which tokens are valid. All data is synthetic.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from starlette.testclient import TestClient

from agenthub import cloud_api
from agenthub.cloud_api import (ANY_CREDENTIAL, CREDENTIAL_ROUTES, PREFIX_ACTIONS, ROUTE_ACTIONS,
    SELF_SERVICE, create_app, route_rule)
from agenthub.enterprise import Denied
from agenthub.oauth_provider import ACTIONS, OAuthRegistry

EVERY_ACTION = frozenset(ACTIONS | {'legal_hold'})


class RecordingStore:
    """Stands in for a tenant store; records every business method it is asked to run."""
    tenant_id = 'acme'
    dsn = None
    serving_reranker = None
    decorate_delivery = None
    ingest_limits = None

    def __init__(self):
        self.calls = []

    @contextmanager
    def delivery_read_lock(self):
        yield

    delivery_lock = delivery_read_lock

    def require_ready(self):
        pass

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return {}
        return call


class GrantRegistry:
    """Bearer `grant:<a>,<b>` authenticates with exactly those actions."""
    def __init__(self, store):
        self.store = store

    def authenticate_token(self, token, request_id=None):
        if not token.startswith('grant:'):
            raise Denied()
        actions = {a for a in token.removeprefix('grant:').split(',') if a}
        return self.store, {'tenant': 'acme', 'principal': 'alice', 'actor': 'alice', 'acting_for': None,
                            'enrollment': 'laptop', 'actions': actions, 'delegated_projects': None,
                            'request_id': request_id}


def concrete(method, path):
    """A request path for a route key (prefix routes get a synthetic identifier)."""
    if (method, path) in PREFIX_ACTIONS:
        return path + ('report' if 'access-review' in path else ('Users/u-1' if 'scim' in path else 'doc-1'))
    return path


def body(method, path):
    if method in ('GET', 'DELETE'):
        return None
    if path == '/enterprise/v1/lifecycle':
        return {'operation': 'withdraw'}
    return {}


class RouteGateTests(unittest.TestCase):
    def request(self, method, path, actions, *, data=None, authenticated=True):
        store = RecordingStore()
        app = create_app(GrantRegistry(store), allowed_hosts=['testserver'],
                         document_factory=lambda _: RecordingStore())
        headers = {'Authorization': 'Bearer grant:' + ','.join(sorted(actions))} if authenticated else {}
        with TestClient(app) as client:
            response = client.request(method, path, headers=headers,
                                      content=None if data is None else json.dumps(data))
        return response, store.calls

    def routes(self):
        yield from ((m, p, rule) for (m, p), rule in ROUTE_ACTIONS.items())
        yield from ((m, p, rule) for (m, p), rule in PREFIX_ACTIONS.items())
        yield 'POST', '/enterprise/v1/lifecycle', route_rule('POST', '/enterprise/v1/lifecycle', {'operation': 'withdraw'})

    def test_each_route_refuses_a_credential_missing_its_action(self):
        for method, path, rule in self.routes():
            if rule is ANY_CREDENTIAL:
                continue
            for group in rule:
                for missing in group:
                    # Drop this action and one action of every other alternative.
                    denied = EVERY_ACTION - {missing} - {other[0] for other in rule if other is not group}
                    with self.subTest(method=method, path=path, missing=missing):
                        response, calls = self.request(method, concrete(method, path), denied,
                                                       data=body(method, path))
                        self.assertEqual(response.status_code, 404, response.text)
                        self.assertEqual(calls, [])

    def test_each_route_admits_a_credential_holding_exactly_its_action(self):
        for method, path, rule in self.routes():
            group = rule[0]
            with self.subTest(method=method, path=path):
                response, _ = self.request(method, concrete(method, path), set(group) or {'audit'},
                                           data=body(method, path))
                self.assertNotEqual(response.status_code, 404, response.text)

    def test_status_requires_read(self):
        response, calls = self.request('GET', '/enterprise/v1/status', {'ingest', 'audit'})
        self.assertEqual((response.status_code, calls), (404, []))
        response, calls = self.request('GET', '/enterprise/v1/status', {'read'})
        self.assertEqual((response.status_code, calls), (200, ['status']))

    def test_writes_need_their_write_action_not_read(self):
        for path, needed in (('/enterprise/v1/boundary', {'ingest'}),
                             ('/enterprise/v1/receipts', {'ingest', 'read'}),
                             ('/enterprise/v3/feedback', {'feedback', 'read'}),
                             ('/enterprise/v1/reviewed-notes', {'curate'}),
                             ('/enterprise/v3/preferences/curate', {'curate'})):
            with self.subTest(path=path):
                response, calls = self.request('POST', path, {'read', 'correct'}, data={})
                self.assertEqual((response.status_code, calls), (404, []))
                response, _ = self.request('POST', path, needed, data={})
                self.assertNotEqual(response.status_code, 404)

    def test_self_service_routes_are_documented_and_still_authenticate(self):
        open_routes = {key for key, rule in ROUTE_ACTIONS.items() if rule is ANY_CREDENTIAL}
        self.assertEqual(open_routes, set(SELF_SERVICE))
        for (method, path), reason in SELF_SERVICE.items():
            self.assertTrue(reason)
            with self.subTest(path=path):
                response, calls = self.request(method, path, {'audit'}, data=body(method, path))
                self.assertNotEqual(response.status_code, 404)
                response, calls = self.request(method, path, set(), data=body(method, path), authenticated=False)
                self.assertEqual((response.status_code, calls), (404, []))

    def test_changing_ones_own_sharing_needs_ingest_or_policy(self):
        change = {'max_sharing': 'organization'}
        response, calls = self.request('POST', '/enterprise/v3/sharing', {'read', 'audit'}, data=change)
        self.assertEqual((response.status_code, calls), (404, []))
        for actions in ({'ingest'}, {'policy'}):
            response, calls = self.request('POST', '/enterprise/v3/sharing', actions, data=change)
            self.assertEqual((response.status_code, calls), (200, ['sharing_setting']))
        response, calls = self.request('POST', '/enterprise/v3/sharing', {'read'}, data={})
        self.assertEqual((response.status_code, calls), (200, ['sharing_setting']))

    def test_unknown_route_is_refused_before_any_business_method(self):
        response, calls = self.request('POST', '/enterprise/v9/unclassified', EVERY_ACTION, data={})
        self.assertEqual((response.status_code, calls), (404, []))

    def test_malformed_body_is_reported_only_after_authentication(self):
        store = RecordingStore()
        app = create_app(GrantRegistry(store), allowed_hosts=['testserver'])
        with TestClient(app) as client:
            for path in ('/enterprise/v3/search', '/enterprise/v3/scim/Users/u-1', '/enterprise/v3/scim/Nope'):
                with self.subTest(path=path):
                    self.assertEqual(client.post(path, content=b'{not json').status_code, 404)
                    self.assertEqual(client.post(path, content=b'[1]').status_code, 404)
            self.assertEqual(client.post('/enterprise/v3/search', content=b'{not json',
                headers={'Authorization': 'Bearer grant:read'}).status_code, 400)
        self.assertEqual(store.calls, [])

    def test_every_route_in_the_dispatcher_is_classified(self):
        # Guards future edits: a path compared in the dispatcher or used by an
        # MCP tool must be a credential route or have an entry in the action table.
        sources = [Path(cloud_api.__file__).read_text()]
        import agenthub.mcp_tools as tools
        sources.append(Path(tools.__file__).read_text())
        literals = set()
        for source in sources:
            literals.update(re.findall(r"'(/enterprise/v\d/[A-Za-z0-9_/-]*)'", source))
        classified = {path for _, path in ROUTE_ACTIONS} | {path for _, path in PREFIX_ACTIONS}
        classified |= set(CREDENTIAL_ROUTES) | {'/enterprise/v1/lifecycle'}
        unclassified = {path for path in literals if path not in classified
                        and not any(path.startswith(prefix) for _, prefix in PREFIX_ACTIONS)}
        self.assertEqual(unclassified, set())


class ReadinessTests(unittest.TestCase):
    def app(self, *, fail=False, ttl=60.0):
        sweeps = []

        class Tenant:
            def require_ready(self):
                if fail:
                    raise ValueError('restore_requires_reconciliation')

        class Control:
            def execute(self, sql, params=()):
                sweeps.append(sql)
                return [('acme',), ('bravo',)]

        class Registry:
            max_stores = 4
            opened = []

            @contextmanager
            def open_control(self):
                yield Control()

            def resolve(self, tenant):
                self.opened.append(tenant)
                return Tenant()

            def store_for_token(self, token):
                raise Denied()
        registry = Registry()
        return create_app(registry, allowed_hosts=['testserver'], readiness_ttl=ttl), registry, sweeps

    def test_probes_share_one_bounded_sweep_per_interval(self):
        app, registry, sweeps = self.app()
        with TestClient(app) as client:
            responses = [client.get('/ready') for _ in range(25)]
        self.assertEqual({(r.status_code, r.text) for r in responses},
                         {(200, json.dumps({'ready': True, 'provider_dispatch': 'off'}, separators=(',', ':')))})
        self.assertEqual((len(sweeps), registry.opened), (1, ['acme', 'bravo']))

    def test_interval_expiry_sweeps_again(self):
        app, registry, sweeps = self.app(ttl=0.0)
        with TestClient(app) as client:
            for _ in range(3):
                client.get('/ready')
        self.assertEqual(len(sweeps), 3)

    def test_failure_is_cached_and_carries_no_tenant_detail(self):
        app, registry, sweeps = self.app(fail=True)
        with TestClient(app) as client:
            first, second = client.get('/ready'), client.get('/ready')
        for response in (first, second):
            self.assertEqual((response.status_code, response.json()), (503, {'ready': False, 'provider_dispatch': 'off'}))
        self.assertEqual(len(sweeps), 1)


def oauth_registry(providers):
    registry = OAuthRegistry.__new__(OAuthRegistry)
    registry.resource = 'https://memory.example/mcp'
    registry.legacy_interactive = False
    registry.providers = providers

    def store_for_token(token):
        raise Denied()
    registry.store_for_token = store_for_token
    return registry


class DiscoveryTests(unittest.TestCase):
    def test_tenant_metadata_is_identical_for_known_and_unknown_tenants(self):
        from types import SimpleNamespace
        registry = oauth_registry([
            SimpleNamespace(issuer='https://identity.example/realms/acme', tenant='acme', actions={'read', 'feedback'}),
            SimpleNamespace(issuer='https://identity.example/realms/bravo', tenant='bravo', actions={'read'})])
        with TestClient(create_app(registry, allowed_hosts=['testserver'])) as client:
            answers = {path: client.get('/.well-known/oauth-protected-resource' + path)
                       for path in ('/mcp', '/mcp/acme', '/mcp/bravo', '/mcp/no-such-tenant', '/mcp/x' * 1)}
            malformed = client.get('/.well-known/oauth-protected-resource/mcp/a/b')
        self.assertEqual({(r.status_code, r.content) for r in answers.values()},
                         {(200, answers['/mcp'].content)})
        self.assertEqual(malformed.status_code, 404)
        document = answers['/mcp/acme'].json()
        self.assertEqual(document['authorization_servers'],
                         ['https://identity.example/realms/acme', 'https://identity.example/realms/bravo'])
        self.assertEqual(document['vaelius_authorization_servers'][1],
                         {'issuer': 'https://identity.example/realms/bravo', 'scopes_supported': ['vaelius:read']})
        self.assertNotIn('acme', json.dumps({k: v for k, v in document.items()
                                             if k not in ('authorization_servers', 'vaelius_authorization_servers')}))


@unittest.skipUnless(os.environ.get('CLOUD_TEST_SERVICES'), 'explicit local PostgreSQL fixture required')
class LoginAndRevocationPostgresTests(unittest.TestCase):
    ISSUER = 'https://identity.example/realms/orchard'

    @classmethod
    def setUpClass(cls):
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from agenthub.postgres import connect, migrate, TenantRegistry
        cls.services = json.loads(Path(os.environ['CLOUD_TEST_SERVICES']).read_text())
        cls.admin = cls.services['admin_dsn'];cls.created = []
        cls.tmp = tempfile.TemporaryDirectory(prefix='route-permissions-')
        for kind in ('control', 'tenant'):
            name = 'route_permissions_' + kind + '_' + uuid.uuid4().hex[:10]
            with connect(cls.admin, autocommit=True) as db:
                db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            cls.created.append(name)
            config = conninfo_to_dict(cls.admin);config['dbname'] = name
            setattr(cls, kind + '_dsn', make_conninfo(**config))
            migrate(getattr(cls, kind + '_dsn'), control=kind == 'control')
        cls.registry = TenantRegistry(cls.control_dsn, Path(cls.tmp.name) / 'stores')
        cls.registry.register('orchard', cls.tenant_dsn)
        cls.store = cls.registry.resolve('orchard');cls.store.create_organization('orchard')
        cls.store.create_principal('orchard', 'alice')

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql
        from agenthub.postgres import connect
        for name in reversed(cls.created):
            with connect(cls.admin, autocommit=True) as db:
                db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        cls.tmp.cleanup()

    def setUp(self):
        from agenthub.cloud_identity import IdentityError
        issuer = self.ISSUER

        class Broker:
            def begin(self):
                state = secrets.token_urlsafe(32)
                return {'url': issuer + '/auth?state=' + state, 'state': state, 'nonce': secrets.token_urlsafe(16),
                        'verifier': secrets.token_urlsafe(32), 'redirect_uri': 'http://127.0.0.1:1/cb',
                        'issuer': issuer, 'expires': time.time() + 300}

            def exchange(self, code, request, *, callback_uri):
                if code != 'good-code' or request['issuer'] != issuer:
                    raise IdentityError('identity_callback_denied')
                return {'issuer': issuer, 'subject': 'subject-alice', 'provider': 'native'}
        self.store.identity_bindings.bind(issuer, 'subject-alice', 'orchard', 'alice')
        with self.registry.open_control() as db, db:
            db.execute('DELETE FROM cloud_login_requests')
        self.client = TestClient(create_app(self.registry, allowed_hosts=['testserver'],
                                            brokers={'fixture': Broker()}))

    def begin(self, tenant, broker='fixture'):
        return self.client.post('/enterprise/v3/auth/begin', json={'tenant': tenant, 'broker': broker})

    def complete(self, tenant, state, code='good-code', enrollment='laptop'):
        return self.client.post('/enterprise/v3/auth/complete', json={'tenant': tenant, 'broker': 'fixture',
            'state': state, 'code': code, 'callback_uri': 'http://127.0.0.1:1/cb', 'enrollment': enrollment})

    def test_begin_is_the_same_for_existing_and_unknown_tenants(self):
        with patch.object(type(self.registry), 'resolve', side_effect=AssertionError('tenant consulted')):
            known, unknown = self.begin('orchard'), self.begin('no-such-tenant')
        for response in (known, unknown):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(set(response.json()), {'url', 'state', 'expires_at'})
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual((self.begin('orchard', 'missing').status_code,
                          self.begin('no-such-tenant', 'missing').status_code), (400, 400))
        with self.registry.open_control() as db:
            stored = sorted(r['tenant'] for r in db.execute('SELECT tenant FROM cloud_login_requests'))
        self.assertEqual(stored, ['no-such-tenant', 'orchard'])

    def test_complete_fails_identically_until_a_valid_state_for_an_existing_tenant(self):
        unknown_state = self.begin('no-such-tenant').json()['state']
        known_state = self.begin('orchard').json()['state']
        bogus = secrets.token_urlsafe(32)
        failures = [self.complete('orchard', bogus), self.complete('no-such-tenant', bogus),
                    self.complete('no-such-tenant', unknown_state),
                    self.complete('no-such-tenant', known_state)]
        for response in failures:
            self.assertEqual((response.status_code, response.json()), (404, {'error': 'not_found'}))
        # The state was consumed by the mismatched attempt; a fresh one completes.
        state = self.begin('orchard').json()['state']
        self.assertEqual(self.complete('orchard', state, code='bad-code').status_code, 404)
        state = self.begin('orchard').json()['state']
        issued = self.complete('orchard', state)
        self.assertEqual(issued.status_code, 200, issued.text)
        ctx = self.store.authenticate(issued.json()['credential'])
        self.assertEqual(ctx['actor'], 'alice')
        self.assertEqual(ctx['actions'], {'ingest', 'read', 'feedback', 'correct', 'withdraw', 'share'})
        self.assertEqual(self.complete('orchard', state).status_code, 404)

    def test_pending_logins_are_bounded_without_changing_the_response(self):
        self.registry.LOGIN_PENDING_PER_TENANT = 3
        try:
            states = [self.begin('orchard') for _ in range(5)]
        finally:
            del self.registry.LOGIN_PENDING_PER_TENANT
        self.assertEqual({r.status_code for r in states}, {200})
        with self.registry.open_control() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cloud_login_requests WHERE tenant='orchard'").fetchone()[0], 3)
        self.assertEqual(self.complete('orchard', states[0].json()['state']).status_code, 404)
        self.assertEqual(self.complete('orchard', states[-1].json()['state'], enrollment='bounded').status_code, 200)

    def test_revocation_answers_identically_for_valid_and_unknown_long_tokens(self):
        from agenthub.enterprise import _digest
        token = self.store.enroll('orchard', 'alice', 'revoke-' + uuid.uuid4().hex[:8], ['read'])
        long_token = 'eyJ' + secrets.token_urlsafe(600)
        with self.store.open() as state, state.db:
            state.db.execute('''INSERT INTO enterprise_credentials(digest,tenant,principal,enrollment,actions,active,
                created,expires_at,oauth_provider) SELECT %s,tenant,principal,enrollment,actions,1,created,expires_at,%s
                FROM enterprise_credentials WHERE digest=%s''', (_digest(long_token), self.ISSUER, _digest(token)))
        self.registry.bind_credential(long_token, 'orchard')
        answers = [self.client.post('/enterprise/v3/auth/revoke', json={'token': value})
                   for value in (long_token, 'eyJ' + secrets.token_urlsafe(600), long_token)]
        self.assertEqual({(r.status_code, r.content) for r in answers}, {(200, b'{"revoked":true}')})
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT active FROM enterprise_credentials WHERE digest=%s',
                (_digest(long_token),)).fetchone()[0], 0)
        self.assertEqual(self.client.post('/enterprise/v3/auth/revoke', json={'token': 'x' * 16385}).status_code, 400)



from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schemas required')
class ActionEnforcementPostgresTests(PostgresFixture, unittest.TestCase):
    """The business methods enforce the same actions as the route gate."""
    def setUp(self):
        from agentclient.enterprise_contract import VERSION
        from agenthub.cloud_runtime import CloudStore
        self.postgres_setup();self.store = CloudStore(self.store.home, self.dsn, 'acme')
        owner = self.store.authenticate(self.tokens['alice'])
        self.version = VERSION
        self.source = self.store.ingest(owner, {'version': VERSION, 'external_id': 'gate-source', 'session': 's',
            'turn': '1', 'project': 'maple', 'kind': 'Stop', 'occurred_at': 100, 'visibility': 'team',
            'body': 'The synthetic orchard export lives in /synthetic/orchard.csv.'})['source_id']
        self.doc = self.store.accept_reviewed_note(owner, self.source, 'Orchard export',
            'The synthetic orchard export lives in /synthetic/orchard.csv.')['document_id']
        with self.store.open() as state:
            self.revision = state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',
                (self.doc,)).fetchone()[0]

    def tearDown(self):
        self.postgres_teardown()

    def grant(self, actions, *, principal='alice', legacy=False):
        from agenthub.enterprise import _digest
        token = self.store.enroll('acme', principal, 'gate-' + uuid.uuid4().hex[:10], actions)
        if legacy:  # As migration 043 marks every grant that predates it.
            with self.store.open() as state, state.db:
                state.db.execute('UPDATE enterprise_credentials SET legacy_actions=1 WHERE digest=?', (_digest(token),))
        return self.store.authenticate(token)

    def feedback(self, ctx, key):
        from agenthub.feedback import record_feedback
        return record_feedback(self.store, ctx, {'id': self.doc, 'revision': self.revision,
                                                 'request_key': key, 'outcome': 'helpful'})

    def note(self, ctx):
        return self.store.accept_reviewed_note(ctx, self.source, 'Orchard export again',
            'The synthetic orchard export lives in /synthetic/orchard.csv.')

    def test_feedback_requires_the_feedback_action(self):
        with self.assertRaises(Denied):
            self.feedback(self.grant(['read', 'ingest', 'correct']), 'without')
        self.assertEqual(self.feedback(self.grant(['read', 'feedback']), 'with')['status'], 'recorded')

    def test_curation_requires_the_curate_action(self):
        with self.assertRaises(Denied):
            self.note(self.grant(['ingest', 'read', 'correct', 'withdraw']))
        with self.assertRaises(Denied):
            self.store.curate_preference(self.grant(['ingest', 'read', 'correct']), self.source,
                                         {'key': 'synthetic', 'value': 'x'})
        self.assertIn('document_id', self.note(self.grant(['read', 'curate'])))

    def test_boundary_and_receipts_require_ingest(self):
        boundary = {'version': self.version, 'event': {'hook_event_name': 'PostCompact', 'trigger': 'auto',
                                                       'session_id': 'session-1', 'turn_id': 'turn-1'}}
        receipt = {'version': self.version, 'session': 'session-1', 'serialized_chars': 10,
                   'cards': [{'id': self.doc, 'revision': self.revision}]}
        reader = self.grant(['read', 'feedback'])
        with self.assertRaises(Denied):
            self.store.boundary(reader, boundary)
        with self.assertRaises(Denied):
            self.store.receipt(reader, receipt)
        capture = self.grant(['read', 'ingest'])
        self.assertIn('epoch', self.store.boundary(capture, boundary))
        self.assertEqual(self.store.receipt(capture, receipt)['accepted'], 1)

    def test_status_requires_read(self):
        with self.assertRaises(Denied):
            self.store.status(self.grant(['ingest']))
        self.assertEqual(self.store.status(self.grant(['read']))['principal'], 'alice')

    def test_grants_that_predate_enforcement_keep_feedback_and_curation(self):
        legacy = self.grant(['ingest', 'read', 'correct', 'withdraw'], legacy=True)
        self.assertTrue({'feedback', 'curate'} <= legacy['actions'])
        self.assertEqual(self.feedback(legacy, 'legacy')['status'], 'recorded')
        self.assertIn('document_id', self.note(legacy))
        # Implication follows the stored grant: no 'read' means no feedback.
        self.assertNotIn('feedback', self.grant(['ingest'], legacy=True)['actions'])

    def test_legacy_refresh_family_keeps_abilities_and_reports_the_stored_actions(self):
        import secrets as token_secrets
        issued = self.store.enroll_with_refresh('acme', 'alice', 'gate-family', ['ingest', 'read', 'correct'])
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_credential_families SET legacy_actions=1')
            state.db.execute('UPDATE enterprise_credentials SET legacy_actions=1 WHERE family IS NOT NULL')
        access = token_secrets.token_urlsafe(48)
        renewed = self.store.refresh_credential(issued['refresh_token'], replacement_access_token=access,
            replacement_refresh_token=token_secrets.token_urlsafe(48))
        # Clients compare renewed actions with what they saw before; they are unchanged.
        self.assertEqual(renewed['actions'], ['correct', 'ingest', 'read'])
        ctx = self.store.authenticate(access)
        self.assertEqual(self.feedback(ctx, 'family')['status'], 'recorded')
        self.assertIn('document_id', self.note(ctx))

    def test_action_checks_add_no_sql(self):
        from agenthub.enterprise import _digest
        from agenthub.postgres import PostgresConnection
        original = PostgresConnection.execute

        def statements(token, operation):
            calls = []
            def counted(db, sql, params=None):
                calls.append(sql);return original(db, sql, params)
            with patch.object(PostgresConnection, 'execute', counted):
                operation(self.store.authenticate(token))
            return len(calls)
        explicit = self.store.enroll('acme', 'alice', 'sql-explicit', ['read', 'feedback'])
        legacy = self.store.enroll('acme', 'alice', 'sql-legacy', ['read'])
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_credentials SET legacy_actions=1 WHERE digest=?', (_digest(legacy),))
        for operation in (lambda ctx: None, self.store.status, lambda ctx: self.feedback(ctx, uuid.uuid4().hex)):
            self.assertEqual(statements(explicit, operation), statements(legacy, operation))

    def test_new_explicit_grants_do_not_imply_actions(self):
        ctx = self.grant(['ingest', 'read', 'correct'])
        self.assertFalse({'feedback', 'curate'} & ctx['actions'])
        self.store.create_principal('acme', 'carol')
        self.store.set_membership('acme', 'maple', 'carol', True)
        self.store.set_delegation('acme', 'bob', 'carol', ['read', 'curate'], ['maple'], True)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT legacy_actions FROM enterprise_delegations').fetchone()[0], 0)


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schemas required')
class LegacyGrantMigrationTests(unittest.TestCase):
    def test_grants_present_at_migration_are_marked_and_later_grants_are_not(self):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from agenthub.postgres import connect
        services = json.loads(Path(SERVICES).read_text())
        admin = services['tenants']['acme']['admin_dsn']
        schema = 'test_grants_' + uuid.uuid4().hex[:12]
        folder = Path(__import__('agenthub.postgres', fromlist=['x']).__file__).parent / 'migrations'
        files = [p for p in sorted(folder.glob('*.sql')) if '_control' not in p.name]
        before = [p for p in files if int(p.name.split('_', 1)[0]) < 43]
        grants = folder / '043_action_grants.sql'
        with connect(admin, autocommit=True) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        dsn = make_conninfo(admin, options='-c search_path=' + schema + ',public')
        try:
            with connect(dsn) as db:
                for path in before:
                    db.execute(path.read_text())
                db.execute("INSERT INTO enterprise_organizations VALUES('acme')")
                db.execute("INSERT INTO enterprise_principals(tenant,id) VALUES('acme','alice'),('acme','bob')")
                db.execute("""INSERT INTO enterprise_credentials(digest,tenant,principal,enrollment,actions,created)
                    VALUES('old','acme','alice','laptop','["read"]',1)""")
                db.execute("""INSERT INTO enterprise_delegations(tenant,principal,acting_for,actions,projects)
                    VALUES('acme','bob','alice','["correct", "read"]','["maple"]')""")
                db.execute(grants.read_text())
                db.execute("""INSERT INTO enterprise_credentials(digest,tenant,principal,enrollment,actions,created)
                    VALUES('new','acme','alice','laptop','["read"]',2)""")
                rows = {r[0]: r[1] for r in db.execute('SELECT digest,legacy_actions FROM enterprise_credentials').fetchall()}
                delegation = db.execute('SELECT legacy_actions,actions FROM enterprise_delegations').fetchone()
            self.assertEqual(rows, {'old': 1, 'new': 0})
            self.assertEqual((delegation[0], delegation[1]), (1, '["correct", "read"]'))
        finally:
            with connect(admin, autocommit=True) as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

if __name__ == '__main__':
    unittest.main()
