"""Origin labels enforced by the service: the origin ceiling and project sets.

A captured event may carry what it touched (agentclient.origin): a ceiling, a
reason category and the other projects whose content it carries. The service
stores them per source, applies the ceiling and every other project's current
ceiling as terms of the sharing minimum at ingest and at query time, requires a
team reader to be a member of every project, and never lets the origin widen.
Three enforcement layers must agree: the Python oracle, the indexed SQL
predicate and the row-level security function. Synthetic data, no model calls.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import EnterpriseStore
from test_cloud_postgres import PostgresFixture, SERVICES

QUERY_VERSION = 'enterprise-local-1'


def label(ceiling, reason='', projects=(), kinds=None, inherited=False):
    return {'ceiling': ceiling, 'kinds': kinds if kinds is not None else (['outside_repo'] if reason else ['web']),
            'reason': reason, 'projects': sorted(projects), 'inherited': inherited}


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class OriginCeilingTests(PostgresFixture, unittest.TestCase):
    """maple: alice, bob, carol. birch: alice, bob. erin: in the tenant, in no project."""

    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_corpus = 'sources'
        self.store.create_project('acme', 'birch')
        for name in ('carol', 'erin'):
            self.store.create_principal('acme', name)
        self.store.set_membership('acme', 'maple', 'carol', True)
        for name in ('alice', 'bob'):
            self.store.set_membership('acme', 'birch', name, True)
        full = ['ingest', 'read', 'source_read', 'correct', 'withdraw', 'policy']
        for name in ('carol', 'erin'):
            self.tokens[name] = self.store.enroll('acme', name, name, full)
        self.alice = self.ctx
        self.bob, self.carol, self.erin = (self.store.authenticate(self.tokens[n]) for n in ('bob', 'carol', 'erin'))
        self.store.enroll_connection(self.alice, 'a-maple-org', 'a-maple-org', 'maple', ['agent'], visibility='organization')
        self.store.sharing_setting(self.alice, {'max_sharing': 'organization'})
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/raw_default_v1.json').read_text())

    def tearDown(self):
        self.postgres_teardown()

    def ingest(self, origin_label=None, *, index=0, tag='', sharing='organization'):
        """One synthetic event. The label is attached after the client's own request
        lowering, as an outdated or careless client would send it."""
        row = self.fixture['messages'][index]
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit' if row['role'] == 'user' else 'Stop',
            'event_id': row['id'] + ':' + tag, 'session_id': self.fixture['conversation'],
            'turn_id': row['id'], 'timestamp': row['time'], 'prompt': row['text'],
            'last_assistant_message': row['text'], 'revision': '1'}, 'maple', 'a-maple-org', sharing=sharing)
        if origin_label is not None:
            event['origin_label'] = origin_label
        return self.store.ingest_general(self.alice, event)['source_id']

    def drain(self):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('no LLM')):
            while SourceIndex(self.store).run(max_sources=50, max_seconds=30)['completed_sources']:
                pass

    def stored(self, source):
        with self.store.open() as state:
            row = state.db.execute('''SELECT sharing,sharing_requested,origin_ceiling,origin_reason,origin_projects
                FROM enterprise_sources WHERE id=?''', (source,)).fetchone()
        return dict(row)

    def raw_documents(self, raw=True):
        """{document: source} for raw passages, or for curated and derived documents."""
        with self.store.open() as state:
            return {r[0]: r[1] for r in state.db.execute('''SELECT ed.id,x.source_id FROM enterprise_documents ed
                JOIN enterprise_dependencies x ON x.document_id=ed.id
                WHERE (ed.representation='raw')=? AND ed.active=1''', (raw,))}

    def layers(self, ctx, raw=True):
        from agenthub.search_reader import restrict
        documents = self.raw_documents(raw)
        with patch.object(self.store, 'current_identity', lambda db, context: context):
            with self.store.open() as state:
                oracle = {documents[d] for d in documents
                          if EnterpriseStore._document_allowed(self.store, state.db, ctx, d)}
                indexed = {documents[d] for d in set(self.store._authorized_documents(state, ctx)) & set(documents)}
            with self.store.open() as state, restrict(self.store, state, ctx, None):
                rls = {documents[r[0]] for r in state.db.execute(
                    "SELECT id FROM enterprise_documents") if r[0] in documents}
        return oracle, indexed, rls

    def assertLayers(self, ctx, expected, msg=None, *, raw=True):
        oracle, indexed, rls = self.layers(ctx, raw)
        self.assertEqual(oracle, set(expected), ('oracle', msg))
        self.assertEqual(indexed, set(expected), ('indexed', msg))
        self.assertEqual(rls, set(expected), ('row_level_security', msg))

    def readers(self, source):
        with self.store.open() as state:
            row = state.db.execute('SELECT * FROM enterprise_sources WHERE id=?', (source,)).fetchone()
            return self.store._passage_readers(state.db, dict(row))

    # The ceiling ---------------------------------------------------------------

    def test_origin_ceiling_caps_the_stored_level_and_every_layer(self):
        plain = self.ingest(tag='plain')
        private = self.ingest(label('private', 'personal_content'), index=1, tag='private')
        team = self.ingest(label('team', 'recalled_memory', kinds=['recalled_memory']), index=2, tag='team')
        web = self.ingest(label('organization', kinds=['web']), index=0, tag='web')
        self.drain()
        self.assertEqual(self.stored(plain), {'sharing': 'organization', 'sharing_requested': 'organization',
                                              'origin_ceiling': None, 'origin_reason': None, 'origin_projects': '[]'})
        self.assertEqual(self.stored(private), {'sharing': 'private', 'sharing_requested': 'organization',
                                                'origin_ceiling': 'private', 'origin_reason': 'personal_content',
                                                'origin_projects': '[]'})
        self.assertEqual((self.stored(team)['sharing'], self.stored(web)['sharing']), ('team', 'organization'))
        self.assertLayers(self.alice, {plain, private, team, web})
        self.assertLayers(self.carol, {plain, team, web})
        self.assertLayers(self.erin, {plain, web})
        self.assertEqual(self.readers(private), ['alice'])
        with self.store.open() as state:
            audited = state.db.execute('''SELECT disposition,count(*) n FROM enterprise_audit
                WHERE operation='origin_label' GROUP BY disposition''').fetchall()
            definitions = {r['source_id']: r['definition'] for r in state.db.execute(
                'SELECT source_id,definition FROM search_source_policy_definitions')}
        self.assertEqual({r['disposition']: r['n'] for r in audited}, {'private': 1, 'team': 1})
        # Only restricting origins add policy keys: other definitions are unchanged.
        self.assertNotIn('origin_ceiling', definitions[plain])
        self.assertNotIn('origin_ceiling', definitions[web])
        self.assertEqual(definitions[private]['origin_ceiling'], 'private')

    def test_origin_is_fixed_and_never_widened_by_later_changes(self):
        private = self.ingest(label('private', 'credential_file'), tag='fixed')
        self.drain()
        # Every ceiling already allows organization; raising them again changes nothing.
        self.store.set_project_sharing('maple', 'organization')
        self.store.set_tenant_sharing_ceiling('organization')
        self.store.sharing_setting(self.alice, {'max_sharing': 'organization'})
        self.assertLayers(self.carol, set())
        # Even a stored level rewritten by hand stays under the stored origin at query time.
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_sources SET sharing='organization' WHERE id=?", (private,))
        self.assertLayers(self.carol, set())
        self.assertLayers(self.erin, set())
        self.assertEqual(self.readers(private), ['alice'])
        # And the origin itself cannot be changed.
        for column, value in (('origin_ceiling', "'organization'"), ('origin_ceiling', 'NULL'),
                              ('origin_projects', "'[\"birch\"]'"), ('origin_reason', 'NULL')):
            with self.subTest(column=column, value=value):
                with self.assertRaisesRegex(Exception, 'origin_label_is_fixed'):
                    with self.store.open() as state, state.db:
                        state.db.execute('UPDATE enterprise_sources SET ' + column + '=' + value + ' WHERE id=?', (private,))
        self.assertEqual(self.stored(private)['origin_ceiling'], 'private')

    def test_derived_documents_inherit_the_origin_of_every_source(self):
        """A curated document built from a tainted passage follows its connection's
        visibility (organization here) only as far as the source's origin allows."""
        plain = self.ingest(tag='d-plain')
        private = self.ingest(label('private', 'personal_content'), index=1, tag='d-private')
        team = self.ingest(label('team', 'recalled_memory', kinds=['recalled_memory']), index=2, tag='d-team')
        both = self.ingest(label('team', 'other_project', ['birch'], kinds=['other_project']), tag='d-both')
        self.drain()
        for number, source in enumerate((plain, private, team, both)):
            self.store.accept_reviewed_note(self.alice, source, 'Synthetic derived note ' + str(number),
                                            'Synthetic derived dataset ' + str(number) + ' lives at /synthetic/d' + str(number) + '.csv.')
        self.assertEqual(set(self.raw_documents(raw=False).values()), {plain, private, team, both})
        self.assertLayers(self.alice, {plain, private, team, both}, 'owner', raw=False)
        self.assertLayers(self.bob, {plain, team, both}, 'member of both projects', raw=False)
        self.assertLayers(self.carol, {plain, team}, 'member of the source project only', raw=False)
        self.assertLayers(self.erin, {plain}, 'organization reader', raw=False)
        # The other project's current ceiling applies to derived documents too.
        self.store.set_project_sharing('birch', 'private')
        self.assertLayers(self.bob, {plain, team}, 'other project lowered', raw=False)
        # Since migration 047 a curated document is capped like a passage, so the
        # project ceiling applies to it at query time as well.
        self.store.set_project_sharing('maple', 'private')
        self.assertLayers(self.erin, set(), 'project ceiling applies to new curated documents', raw=False)
        # Curated documents registered before migration 047 keep their previous rules.
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_documents SET level_capped=0')
        self.assertLayers(self.erin, {plain}, 'pre-047 curated documents keep their rules', raw=False)

    # Project sets ----------------------------------------------------------------

    def test_project_set_intersects_readers_and_applies_every_ceiling(self):
        both = self.ingest(label('team', 'other_project', ['birch'], kinds=['other_project']), tag='both')
        org = self.ingest(label('organization', 'other_project', ['birch'], kinds=['other_project']), index=1, tag='org')
        self.drain()
        self.assertEqual(json.loads(self.stored(both)['origin_projects']), ['birch'])
        self.assertEqual(self.stored(both)['sharing'], 'team')
        # team: members of maple AND birch only.
        self.assertLayers(self.bob, {both, org})
        self.assertLayers(self.carol, {org})
        self.assertLayers(self.erin, {org})
        self.assertEqual(self.readers(both), ['alice', 'bob'])
        # The other project's CURRENT ceiling is a term of the minimum.
        self.store.set_project_sharing('birch', 'team')
        self.assertLayers(self.erin, set())
        self.assertLayers(self.carol, set())
        self.assertLayers(self.bob, {both, org})
        self.store.set_project_sharing('birch', 'private')
        self.assertLayers(self.bob, set())
        self.store.set_project_sharing('birch', None)
        self.assertLayers(self.bob, {both, org})
        # Leaving the other project removes access at once.
        self.store.set_membership('acme', 'birch', 'bob', False)
        self.assertLayers(self.bob, {org})
        self.assertEqual(self.readers(both), ['alice'])
        # Recalled items say which other projects they carry.
        delivered = self.store.decorate_delivery('/enterprise/v3/search', self.store.search(self.alice, {
            'version': QUERY_VERSION, 'query': self.fixture['query'], 'project': 'maple'}), {'project': 'maple'})
        origins = [card['origin'] for card in delivered['results'] if card.get('origin')]
        self.assertIn({'project': 'maple', 'sharing': 'team', 'projects': ['birch']}, origins)

    def test_unknown_or_inactive_other_project_is_private(self):
        ghost = self.ingest(label('team', 'other_project', ['ghost'], kinds=['other_project']), tag='ghost')
        self.store.create_project('acme', 'retired')
        self.store.set_membership('acme', 'retired', 'bob', True)
        retired = self.ingest(label('team', 'other_project', ['retired'], kinds=['other_project']), index=1, tag='retired')
        self.drain()
        self.assertEqual(self.stored(ghost)['sharing'], 'private')
        self.assertEqual(self.stored(retired)['sharing'], 'team')
        self.assertLayers(self.bob, {retired})
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_projects SET active=0 WHERE tenant='acme' AND id='retired'")
        self.assertLayers(self.bob, set())

    def test_service_rejects_malformed_labels_and_strips_its_own_project(self):
        for number, bad in enumerate((label('public', 'outside_repo'), label('private', 'matched ~/notes.txt'),
                    dict(label('private', 'outside_repo'), hint='~/Documents'), label('team', '', ['birch']))):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    self.ingest(bad, tag='bad' + str(number))
        own = self.ingest(label('team', 'other_project', ['maple', 'birch'], kinds=['other_project']), tag='own')
        self.assertEqual(json.loads(self.stored(own)['origin_projects']), ['birch'])


if __name__ == '__main__':
    unittest.main()
