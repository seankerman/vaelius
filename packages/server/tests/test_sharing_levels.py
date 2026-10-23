"""One sharing ladder (private < team < organization) and project-scoped retrieval.

Synthetic PostgreSQL cases, no model calls. Effective sharing is the minimum of
the author's own setting, the scope-file request, the project and tenant
ceilings, the connection visibility and the origin ceiling; it is stored per
source and every ceiling is applied again at query time. Retrieval returns the
current project first and other projects' organization-shared content as a later,
labelled tier. Three enforcement layers must agree: the Python oracle, the
indexed SQL predicate and the row-level security function.
"""
import hashlib
import json
from pathlib import Path
import time
import unittest
import uuid
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Denied, EnterpriseStore
from agenthub.postgres import PostgresConnection, connect, migrate
from test_cloud_postgres import PostgresFixture, SERVICES

QUERY_VERSION = 'enterprise-local-1'


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class SharingLevelTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_corpus = 'sources'
        self.store.create_project('acme', 'birch')
        for name in ('carol', 'dave', 'erin', 'frank'):
            self.store.create_principal('acme', name)
        for project, people in (('birch', ('alice', 'bob', 'carol')),):
            for name in people:
                self.store.set_membership('acme', project, name, True)
        full = ['ingest', 'read', 'source_read', 'correct', 'withdraw', 'policy']
        for name in ('carol', 'erin', 'frank'):
            self.tokens[name] = self.store.enroll('acme', name, name, full)
        self.tokens['dave'] = self.store.enroll('acme', 'dave', 'dave', ['read', 'source_read'])
        self.alice = self.ctx
        self.bob, self.carol, self.dave, self.erin, self.frank = (
            self.store.authenticate(self.tokens[name]) for name in ('bob', 'carol', 'dave', 'erin', 'frank'))
        for ident, project, visibility in (('a-maple-org', 'maple', 'organization'),
                                           ('a-maple-team', 'maple', 'team'),
                                           ('a-birch-org', 'birch', 'organization')):
            self.store.enroll_connection(self.alice, ident, ident, project, ['agent'], visibility=visibility)
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/raw_default_v1.json').read_text())

    def tearDown(self):
        self.postgres_teardown()

    # Helpers ------------------------------------------------------------------

    def personal(self, ctx, level):
        return self.store.sharing_setting(ctx, {'max_sharing': level})

    def ingest(self, connection, project='maple', *, index=0, sharing='organization', originals='private',
               ctx=None, tag=''):
        row = self.fixture['messages'][index]
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit' if row['role'] == 'user' else 'Stop',
            'event_id': row['id'] + ':' + connection + tag, 'session_id': self.fixture['conversation'] + project,
            'turn_id': row['id'], 'timestamp': row['time'], 'prompt': row['text'],
            'last_assistant_message': row['text'], 'revision': '1'}, project, connection,
            sharing=sharing, originals=originals)
        return self.store.ingest_general(ctx or self.alice, event)['source_id']

    def drain(self):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('no LLM')):
            while SourceIndex(self.store).run(max_sources=50, max_seconds=30)['completed_sources']:
                pass

    def stored(self, source):
        with self.store.open() as state:
            row = state.db.execute('''SELECT sharing,sharing_requested,originals_sharing,originals_requested
                FROM enterprise_sources WHERE id=?''', (source,)).fetchone()
        return dict(row)

    def request(self, project='maple', **kwargs):
        return {'version': QUERY_VERSION, 'query': self.fixture['query'], 'project': project, **kwargs}

    def search(self, ctx, project='maple', **kwargs):
        return self.store.search(ctx, self.request(project, **kwargs))

    def found(self, ctx, project='maple', **kwargs):
        """Source IDs behind the delivered cards, in delivery order."""
        cards = self.search(ctx, project, **kwargs)['results']
        with self.store.open() as state:
            owners = {row['document_id']: row['source_id'] for row in state.db.execute(
                '''SELECT x.document_id,x.source_id FROM enterprise_dependencies x
                   WHERE x.document_id=ANY(?::text[])''', ([c['id'] for c in cards],))}
        return [owners[c['id']] for c in cards]

    def delivered(self, ctx, project='maple', **kwargs):
        request = self.request(project, **kwargs)
        return self.store.decorate_delivery('/enterprise/v3/search', self.store.search(ctx, request), request)

    def raw_documents(self):
        with self.store.open() as state:
            return {r[0]: r[1] for r in state.db.execute('''SELECT ed.id,x.source_id FROM enterprise_documents ed
                JOIN enterprise_dependencies x ON x.document_id=ed.id WHERE ed.representation='raw' AND ed.active=1''')}

    def layers(self, ctx, *, project=None, reach=None):
        """Raw-passage source IDs each enforcement layer admits for ctx.

        Identity rechecks are bypassed so a stale context (a principal deactivated
        after authenticating) reaches the policy SQL itself.
        """
        from agenthub.search_reader import restrict
        documents = self.raw_documents()
        with patch.object(self.store, 'current_identity', lambda db, context: context):
            with self.store.open() as state:
                oracle = {documents[d] for d in documents
                          if EnterpriseStore._document_allowed(self.store, state.db, ctx, d)}
                indexed = {documents[d] for d in set(self.store._authorized_documents(state, ctx)) & set(documents)}
            with self.store.open() as state, restrict(self.store, state, ctx, project, reach=reach):
                rls = {documents[r[0]] for r in state.db.execute(
                    "SELECT id FROM enterprise_documents WHERE representation='raw'") if r[0] in documents}
        return oracle, indexed, rls

    def assertLayers(self, ctx, expected, msg=None):
        oracle, indexed, rls = self.layers(ctx)
        self.assertEqual(oracle, set(expected), ('oracle', msg))
        self.assertEqual(indexed, set(expected), ('indexed', msg))
        self.assertEqual(rls, set(expected), ('row_level_security', msg))

    # The minimum ---------------------------------------------------------------

    def test_new_tenant_defaults_and_effective_level_is_the_minimum_of_every_term(self):
        with self.store.open() as state:
            tenant = state.db.execute('SELECT default_max_sharing,max_sharing FROM enterprise_organizations WHERE id=?',
                                      ('acme',)).fetchone()
            person = state.db.execute("SELECT max_sharing FROM enterprise_principals WHERE tenant='acme' AND id='alice'").fetchone()
        self.assertEqual(dict(tenant), {'default_max_sharing': 'organization', 'max_sharing': 'organization'})
        self.assertEqual(person['max_sharing'], 'private')
        # The person's own default keeps a full organization request private.
        source = self.ingest('a-maple-org', tag='default')
        self.assertEqual(self.stored(source)['sharing'], 'private')
        self.personal(self.alice, 'organization')
        cases = (('everything organization', lambda: None, 'a-maple-org', 'organization'),
                 ('team connection', lambda: None, 'a-maple-team', 'team'),
                 ('person team', lambda: self.personal(self.alice, 'team'), 'a-maple-org', 'team'),
                 ('project team', lambda: self.store.set_project_sharing('maple', 'team'), 'a-maple-org', 'team'),
                 ('tenant default team', lambda: self.store.set_tenant_sharing_default('team'), 'a-maple-org', 'team'),
                 ('tenant ceiling team', lambda: self.store.set_tenant_sharing_ceiling('team'), 'a-maple-org', 'team'),
                 ('project private', lambda: self.store.set_project_sharing('maple', 'private'), 'a-maple-org', 'private'))
        for number, (label, change, connection, expected) in enumerate(cases):
            self.personal(self.alice, 'organization')
            self.store.set_project_sharing('maple', None)
            self.store.set_tenant_sharing_default('organization')
            self.store.set_tenant_sharing_ceiling('organization')
            change()
            source = self.ingest(connection, tag=str(number))
            self.assertEqual(self.stored(source)['sharing'], expected, label)
        # The origin ceiling is a named term of the minimum; it is a no-op until supplied.
        self.store.set_project_sharing('maple', None)
        with patch('agenthub.project_sharing.origin_ceiling', return_value='team'):
            source = self.ingest('a-maple-org', tag='origin')
        self.assertEqual(self.stored(source)['sharing'], 'team')
        with self.store.open() as state:
            capped = state.db.execute('''SELECT disposition,count(*) FROM enterprise_audit
                WHERE operation='sharing_request' GROUP BY disposition''').fetchall()
        self.assertEqual({row[0] for row in capped}, {'capped_private', 'capped_team'})

    def test_persons_own_setting_caps_sharing_and_lowering_it_hides_at_once(self):
        stored_private = self.ingest('a-maple-org', tag='before')
        self.drain()
        self.assertEqual(self.stored(stored_private)['sharing'], 'private')
        for reader in (self.bob, self.carol):
            self.assertEqual(self.found(reader), [])
        self.personal(self.alice, 'organization')
        shared = self.ingest('a-maple-org', index=1, tag='after')
        self.drain()
        # Raising the setting applies to new captures only.
        self.assertEqual(self.found(self.carol), [shared])
        self.assertLayers(self.carol, {shared})
        # Lowering it hides at once: organization -> team -> private.
        self.personal(self.alice, 'team')
        self.assertEqual(self.found(self.carol), [])
        self.assertLayers(self.carol, set())
        self.assertLayers(self.bob, {shared})
        self.personal(self.alice, 'private')
        self.assertLayers(self.bob, set())
        self.assertEqual(self.found(self.bob), [])
        self.assertEqual(sorted(self.found(self.alice)), sorted([stored_private, shared]))
        # Only the person changes it: a delegated credential reads but cannot change it.
        self.store.create_principal('acme', 'helper')
        self.store.set_delegation('acme', 'helper', 'alice', ['read', 'ingest'], ['maple'], True)
        delegated = self.store.authenticate(self.store.enroll('acme', 'helper', 'helper-a', ['read', 'ingest'],
                                                              acting_for='alice'))
        self.assertEqual(self.store.sharing_setting(delegated, {})['max_sharing'], 'private')
        with self.assertRaises(Denied):
            self.store.sharing_setting(delegated, {'max_sharing': 'organization'})
        with self.assertRaises(ValueError):
            self.personal(self.alice, 'everyone')
        with self.store.open() as state:
            audit = [r[0] for r in state.db.execute('''SELECT disposition FROM enterprise_audit
                WHERE operation='personal_sharing' AND actor='alice' ORDER BY created''')]
        self.assertEqual(audit, ['organization', 'team', 'private'])

    def test_every_ceiling_is_applied_again_at_query_time(self):
        self.personal(self.alice, 'organization')
        shared = self.ingest('a-maple-org')
        self.drain()
        self.assertLayers(self.carol, {shared})
        for lower, restore in ((lambda: self.store.set_project_sharing('maple', 'team'),
                                lambda: self.store.set_project_sharing('maple', None)),
                               (lambda: self.store.set_tenant_sharing_default('team'),
                                lambda: self.store.set_tenant_sharing_default('organization')),
                               (lambda: self.store.set_tenant_sharing_ceiling('team'),
                                lambda: self.store.set_tenant_sharing_ceiling('organization'))):
            lower()
            self.assertLayers(self.carol, set())
            self.assertLayers(self.bob, {shared})   # still a team member's
            restore()
            self.assertLayers(self.carol, {shared})
        # Narrowing the connection is permanent and also re-indexes the passage.
        self.store.connection_policy(self.alice, 'a-maple-org', visibility='team')
        self.drain()
        self.assertEqual(self.found(self.carol), [])
        self.assertLayers(self.carol, set())

    # Who the organization is ----------------------------------------------------

    def test_organization_reaches_active_principals_only(self):
        self.personal(self.alice, 'organization')
        shared = self.ingest('a-maple-org')
        self.drain()
        # dave belongs to no project and holds only read credentials: still the organization.
        for reader in (self.bob, self.carol, self.dave, self.erin, self.frank):
            self.assertLayers(reader, {shared}, reader['actor'])
        self.assertEqual(self.found(self.dave), [shared])
        # A deactivated principal is not the organization, even with a context
        # obtained before deactivation; normal requests are refused outright.
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_principals SET active=0 WHERE tenant='acme' AND id='frank'")
        self.assertLayers(self.frank, set())
        with self.assertRaises(Denied):
            self.store.authenticate(self.tokens['frank'])
        # A quarantined principal is not the organization either.
        self.store.quarantine_principal('erin', reason='synthetic review')
        self.assertLayers(self.erin, set())
        self.assertEqual(self.found(self.erin), [])
        self.store.release_principal('erin', reason='review cleared')
        self.assertLayers(self.erin, {shared})
        # A credential without `read` (a capture or backend credential) reads nothing.
        capture_only = self.store.authenticate(self.store.enroll('acme', 'dave', 'dave-capture', ['ingest']))
        with self.assertRaises(Denied):
            self.search(capture_only)
        # A delegated credential stays inside its delegated projects.
        self.store.create_principal('acme', 'helper')
        self.store.set_delegation('acme', 'helper', 'carol', ['read'], ['birch'], True)
        helper = self.store.authenticate(self.store.enroll('acme', 'helper', 'helper-c', ['read'], acting_for='carol'))
        self.assertEqual(self.store.search(helper, {'version': QUERY_VERSION, 'query': self.fixture['query']})['results'], [])

    def test_non_member_never_sees_team_level_content(self):
        self.personal(self.alice, 'organization')
        self.store.set_project_sharing('maple', 'team')
        team = self.ingest('a-maple-org')
        self.drain()
        self.assertEqual(self.stored(team)['sharing'], 'team')
        self.assertLayers(self.bob, {team})
        for reader in (self.carol, self.dave):
            self.assertLayers(reader, set(), reader['actor'])
            self.assertEqual(self.found(reader), [])
            self.assertEqual(self.found(reader, 'birch', scope='all'), [])

    # Originals --------------------------------------------------------------------

    def test_originals_follow_their_own_setting_not_the_passages(self):
        self.personal(self.alice, 'organization')
        private_original = self.ingest('a-maple-org', tag='p')
        team_original = self.ingest('a-maple-org', index=1, originals='team', tag='t')
        org_original = self.ingest('a-maple-org', index=2, originals='organization', tag='o')
        capped = self.ingest('a-maple-team', index=2, sharing='team', originals='organization', tag='c')
        self.drain()
        self.assertEqual(self.stored(private_original)['originals_sharing'], 'private')
        # Originals are never shared above their passages.
        self.assertEqual(self.stored(capped), {'sharing': 'team', 'sharing_requested': 'team',
                                               'originals_sharing': 'team', 'originals_requested': 'organization'})
        # Every passage is organization-shared...
        self.assertLayers(self.carol, {private_original, team_original, org_original})

        def readable(ctx, source):
            try:
                return self.store.general_source(ctx, source)['source_id'] == source
            except Denied:
                return False
        # ...but each original follows its own level.
        for reader, expected in ((self.alice, {private_original, team_original, org_original, capped}),
                                 (self.bob, {team_original, org_original, capped}),
                                 (self.carol, {org_original})):
            got = {s for s in (private_original, team_original, org_original, capped) if readable(reader, s)}
            self.assertEqual(got, expected, reader['actor'])
        # Originals still need source_read, and every ceiling applies at query time.
        reader = self.store.authenticate(self.store.enroll('acme', 'carol', 'carol-read', ['read']))
        self.assertFalse(readable(reader, org_original))
        self.personal(self.alice, 'team')
        self.assertFalse(readable(self.carol, org_original))
        self.assertTrue(readable(self.bob, org_original))
        self.personal(self.alice, 'private')
        self.assertFalse(readable(self.bob, team_original))
        # An explicit owner grant still works when levels are private.
        from agenthub import raw_access
        raw_access.grant(self.store, self.alice, 'source', private_original, 'principal', 'carol')
        self.assertTrue(readable(self.carol, private_original))

    # Project-scoped retrieval ---------------------------------------------------

    def test_default_retrieval_is_the_current_project_then_the_organization_tier(self):
        self.personal(self.alice, 'organization')
        self.store.enroll_connection(self.alice, 'a-birch-team', 'a-birch-team', 'birch', ['agent'], visibility='team')
        maple_team = self.ingest('a-maple-team', sharing='team')
        maple_private = self.ingest('a-maple-org', index=1, sharing='private')
        birch_team = self.ingest('a-birch-team', 'birch', index=1, sharing='team')
        birch_private = self.ingest('a-birch-org', 'birch', index=1, sharing='private', tag='p')
        birch_org = self.ingest('a-birch-org', 'birch', index=0, sharing='organization')
        self.drain()
        # The author searching in maple: maple first, then birch's organization tier.
        # Her own private and team content in birch stays out unless asked for.
        self.assertEqual(set(self.found(self.alice)), {maple_team, maple_private, birch_org})
        self.assertNotIn(birch_team, self.found(self.alice))
        self.assertEqual(set(self.found(self.alice, scope='all')),
                         {maple_team, maple_private, birch_team, birch_private, birch_org})
        # A member of both projects: same rule.
        self.assertEqual(set(self.found(self.bob)), {maple_team, birch_org})
        self.assertEqual(set(self.found(self.bob, scope='all')), {maple_team, birch_team, birch_org})
        self.assertEqual(set(self.found(self.bob, 'birch')), {birch_team, birch_org})
        # Not a maple member: only the organization tier from birch.
        self.assertEqual(self.found(self.dave), [birch_org])
        self.assertEqual(self.found(self.dave, scope='all'), [birch_org])
        # Ordering and labels: current project first, then the organization tier.
        result = self.delivered(self.bob)
        tiers = [card['tier'] for card in result['results']]
        self.assertEqual(tiers, sorted(tiers, key=['project', 'other_project', 'organization'].index))
        self.assertEqual(tiers, ['project', 'organization'])
        current, other = result['results']
        self.assertEqual(current['origin'], {'project': 'maple', 'sharing': 'team'})
        self.assertNotIn('from', current)
        self.assertEqual(other['origin'], {'project': 'birch', 'sharing': 'organization'})
        self.assertEqual(other['from'], 'from birch · alice · ' + other['provenance']['captured_at'][:10])
        self.assertEqual(other['provenance']['author'], 'alice')
        everything = self.delivered(self.bob, scope='all')['results']
        self.assertEqual([card['tier'] for card in everything], ['project', 'other_project', 'organization'])
        self.assertEqual(everything[1]['origin'], {'project': 'birch', 'sharing': 'team'})
        # Without a current project nothing is narrowed and every item says where it is from.
        unscoped = self.store.search(self.bob, {'version': QUERY_VERSION, 'query': self.fixture['query']})
        self.assertEqual(len(unscoped['results']), 3)
        decorated = self.store.decorate_delivery('/enterprise/v3/search', unscoped,
                                                 {'query': self.fixture['query']})
        self.assertTrue(all(card['from'].startswith('from ') and 'tier' not in card for card in decorated['results']))
        with self.assertRaises(ValueError):
            self.search(self.bob, scope='everything')

    def test_current_project_fills_the_candidate_pool_first(self):
        # Many matching organization-shared passages elsewhere must not crowd out
        # the one current-project passage from the bounded candidate list.
        self.personal(self.alice, 'organization')
        for number in range(self.store.retrieval_candidate_limit + 5):
            self.ingest('a-birch-org', 'birch', index=number % 2, tag='crowd' + str(number))
        mine = self.ingest('a-maple-team', index=1, sharing='team', ctx=self.alice)
        self.drain()
        self.assertEqual(self.found(self.bob)[0], mine)

    def test_retrieval_scope_adds_no_statements(self):
        self.personal(self.alice, 'organization')
        self.ingest('a-maple-org');self.ingest('a-birch-org', 'birch', index=1)
        self.drain()

        def statements(ctx, **kwargs):
            sql = [];execute = PostgresConnection.execute

            def counted(db, query, params=None):
                sql.append(query);return execute(db, query, params)
            with patch.object(PostgresConnection, 'execute', counted):
                self.search(ctx, **kwargs)
            return len(sql)
        counts = {(ctx['actor'], scope): statements(ctx, scope=scope)
                  for ctx in (self.alice, self.carol) for scope in ('project', 'all')}
        self.assertEqual(len(set(counts.values())), 1, counts)

    # Parity and migration ---------------------------------------------------------

    def test_indexed_predicate_row_security_and_python_oracle_agree_across_levels(self):
        self.store.enroll_connection(self.bob, 'b-maple-org', 'b', 'maple', ['agent'], visibility='organization')
        self.store.enroll_connection(self.bob, 'b-birch-team', 'bt', 'birch', ['agent'], visibility='team')
        self.personal(self.alice, 'organization')
        self.personal(self.bob, 'team')
        for index, (connection, project, sharing, ctx) in enumerate((
                ('a-maple-org', 'maple', 'organization', self.alice), ('a-maple-org', 'maple', 'team', self.alice),
                ('a-maple-org', 'maple', 'private', self.alice), ('a-maple-team', 'maple', 'organization', self.alice),
                ('a-birch-org', 'birch', 'organization', self.alice), ('b-maple-org', 'maple', 'organization', self.bob),
                ('b-birch-team', 'birch', 'team', self.bob))):
            self.ingest(connection, project, index=index % 3, sharing=sharing, ctx=ctx, tag='p' + str(index))
        self.drain()
        self.assertEqual(len(self.raw_documents()), 7)
        # A delegated (service) credential never gets the organization scope.
        self.store.create_principal('acme', 'helper')
        self.store.set_delegation('acme', 'helper', 'carol', ['read'], ['birch', 'maple'], True)
        helper = self.store.authenticate(self.store.enroll('acme', 'helper', 'helper-p', ['read'], acting_for='carol'))
        readers = (self.alice, self.bob, self.carol, self.dave, self.erin, helper)

        def compare(label):
            sizes = {}
            for ctx in readers:
                oracle, indexed, rls = self.layers(ctx)
                self.assertEqual(indexed, oracle, (label, ctx['actor'], 'indexed'))
                self.assertEqual(rls, oracle, (label, ctx['actor'], 'row_level_security'))
                sizes[ctx['principal']] = len(oracle)
            return sizes
        # p0 alice maple organization, p1 alice maple team, p2 alice maple private,
        # p3 alice maple team connection (team), p4 alice birch organization,
        # p5 bob maple (bob's own setting team: team), p6 bob birch team.
        self.assertEqual(compare('baseline'), {'alice': 7, 'bob': 6, 'carol': 3, 'dave': 2, 'erin': 2, 'helper': 1})
        self.store.quarantine_principal('erin', reason='synthetic review')
        self.store.set_tenant_sharing_ceiling('team')
        self.personal(self.bob, 'private')
        self.assertEqual(compare('lowered'), {'alice': 5, 'bob': 6, 'carol': 1, 'dave': 0, 'erin': 0, 'helper': 0})
        self.store.set_tenant_sharing_ceiling('organization')
        self.store.set_project_sharing('birch', 'private')
        self.assertEqual(compare('birch private'), {'alice': 5, 'bob': 5, 'carol': 1, 'dave': 1, 'erin': 0, 'helper': 0})

    def test_migration_keeps_existing_tenants_people_and_originals_where_they_were(self):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        item = self.services['tenants']['acme']
        schema = 'test_levels_' + uuid.uuid4().hex[:12]
        with connect(item['admin_dsn']) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        dsn = make_conninfo(item['admin_dsn'], options='-c search_path=' + schema + ',public')
        try:
            folder = Path(migrate.__code__.co_filename).parent / 'migrations'
            with connect(dsn) as db:
                db.execute('CREATE TABLE cloud_schema(version INTEGER PRIMARY KEY,name TEXT NOT NULL,'
                           'sha256 TEXT NOT NULL,applied DOUBLE PRECISION NOT NULL)')
                for path in sorted(folder.glob('*.sql')):
                    version = int(path.name.split('_', 1)[0])
                    if '_control' in path.name or version >= 45:
                        continue
                    db.execute(path.read_text())
                    db.execute('INSERT INTO cloud_schema VALUES(%s,%s,%s,%s)',
                               (version, path.name, hashlib.sha256(path.read_bytes()).hexdigest(), time.time()))
                db.execute("INSERT INTO enterprise_organizations(id,default_max_sharing) VALUES('old','private'),('older','team')")
                db.execute("""INSERT INTO enterprise_principals(tenant,id) VALUES('old','sharer'),('old','quiet'),('older','other')""")
                db.execute("INSERT INTO enterprise_projects(tenant,id,max_sharing) VALUES('old','maple','team')")
                for ident, owner, sharing in (('s1', 'sharer', 'team'), ('s2', 'quiet', 'private')):
                    db.execute("""INSERT INTO enterprise_sources(id,tenant,owner,external_project,internal_project,
                        external_id,enrollment,payload_hash,visibility,occurred_at,created,sharing,sharing_requested)
                        VALUES(%s,'old',%s,'maple','scope',%s,'e','h','team','unknown',0,%s,%s)""",
                        (ident, owner, ident, sharing, sharing))
                before = db.execute('SELECT count(*) FROM search_policies').fetchone()[0]
            migrate(dsn)
            with connect(dsn) as db:
                tenants = {row['id']: (row['default_max_sharing'], row['max_sharing']) for row in db.execute(
                    'SELECT id,default_max_sharing,max_sharing FROM enterprise_organizations')}
                people = {row['id']: row['max_sharing'] for row in db.execute(
                    "SELECT id,max_sharing FROM enterprise_principals WHERE tenant='old'")}
                originals = {row['originals_sharing'] for row in db.execute('SELECT originals_sharing FROM enterprise_sources')}
                after = db.execute('SELECT count(*) FROM search_policies').fetchone()[0]
                from agenthub.search_permissions import inspect_projection
                projection = inspect_projection(PostgresConnection(db))
                db.execute("INSERT INTO enterprise_organizations(id) VALUES('new')")
                new = db.execute("SELECT default_max_sharing,max_sharing FROM enterprise_organizations WHERE id='new'").fetchone()
                db.execute("INSERT INTO enterprise_principals(tenant,id) VALUES('new','fresh')")
                fresh = db.execute("SELECT max_sharing FROM enterprise_principals WHERE id='fresh'").fetchone()[0]
            # Nothing widens: existing tenants keep their default and get the old
            # maximum (team) as their ceiling; only a principal already sharing keeps team.
            self.assertEqual(tenants['old'], ('private', 'team'))
            self.assertEqual(tenants['older'], ('team', 'team'))
            self.assertEqual(people, {'sharer': 'team', 'quiet': 'private'})
            self.assertEqual(originals, {'private'})
            self.assertEqual(after, before)
            self.assertEqual((projection['requirement_mismatches'], projection['source_policy_mismatches']), (0, 0))
            self.assertEqual((new['default_max_sharing'], new['max_sharing']), ('organization', 'organization'))
            self.assertEqual(fresh, 'private')
        finally:
            with connect(item['admin_dsn']) as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    # Reports ---------------------------------------------------------------------

    def test_status_explain_and_capture_notice_report_levels(self):
        self.personal(self.alice, 'organization')
        source = self.ingest('a-maple-org', originals='team')
        self.drain()
        status = self.store.sharing_policy_status()
        self.assertEqual((status['default_max_sharing'], status['max_sharing']), ('organization', 'organization'))
        self.assertEqual(status['people']['organization'], 1)
        maple = [p for p in status['projects'] if p['project'] == 'maple'][0]
        self.assertEqual(maple['active_sources']['organization'], 1)
        self.assertEqual(maple['active_sources']['originals']['team'], 1)
        readers = self.store.explain(self.store.authenticate(self.tokens['admin']), source)['raw_readers']
        self.assertEqual(readers, ['admin', 'alice', 'bob', 'carol', 'dave', 'erin', 'frank'])
        notice = self.store.capture_notice(self.alice, {'project': 'maple', 'connection': 'a-maple-org'})
        self.assertEqual({k: notice['readers'][k] for k in ('personal_sharing', 'project_ceiling', 'passage_sharing_limit')},
                         {'personal_sharing': 'organization', 'project_ceiling': 'organization',
                          'passage_sharing_limit': 'organization'})
        team_notice = self.store.capture_notice(self.alice, {'project': 'maple', 'connection': 'a-maple-team'})
        self.assertEqual(team_notice['readers']['passage_sharing_limit'], 'team')
        # Access reviews show each person's own ceiling.
        from agenthub.access_review import collect
        people = {item['principal']: item['max_sharing'] for item in collect(self.store)['principals']}
        self.assertEqual((people['alice'], people['bob']), ('organization', 'private'))


if __name__ == '__main__':
    unittest.main()
