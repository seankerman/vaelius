"""Owner self-service: find, delete, change sharing and grant one's own content.

Synthetic PostgreSQL cases, no model calls. Only the owner acts on their sources;
deletion is the canonical purge and a legal hold wins; an item is private or
shared with its workspace, and sharing needs the writer role and never passes
the origin; shares name readers (principals or groups), are revocable and audited
and stand in for the workspace audience only; derived documents need the
audience of every source; moderation follows the workspace admin role and hides
but never deletes. Three enforcement layers (Python oracle, indexed SQL
predicate, row-level security) must agree.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Denied, EnterpriseStore
from test_cloud_postgres import PostgresFixture, SERVICES

QUERY_VERSION = 'enterprise-local-1'
FULL = ['ingest', 'read', 'source_read', 'correct', 'withdraw', 'share', 'manage', 'policy', 'curate']


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class OwnerControlTests(PostgresFixture, unittest.TestCase):
    """maple: writers alice, bob, carol and an organization reader entry (dave reads
    through it). birch: writers alice, carol."""

    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_corpus = 'sources'
        self.store.create_project('acme', 'birch')
        for name in ('carol', 'dave'):
            self.store.create_principal('acme', name)
        for name in ('alice', 'carol'):
            self.store.set_membership('acme', 'birch', name, True)
        self.store.set_membership('acme', 'maple', 'carol', True)
        for name in ('alice', 'bob', 'carol', 'dave'):
            self.tokens[name] = self.store.enroll('acme', name, name + '-full', FULL)
        self.alice, self.bob, self.carol, self.dave = (self.store.authenticate(self.tokens[n])
                                                       for n in ('alice', 'bob', 'carol', 'dave'))
        for ident, project, visibility in (('a-maple', 'maple', 'organization'), ('a-birch', 'birch', 'organization')):
            self.store.enroll_connection(self.alice, ident, ident, project, ['agent'], visibility=visibility)
        self.store.enroll_connection(self.bob, 'b-maple', 'b', 'maple', ['agent'], visibility='organization')
        self.store.operator_workspace('set-entry', 'maple', subject_kind='organization', role='reader')
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/raw_default_v1.json').read_text())

    def tearDown(self):
        self.postgres_teardown()

    # Helpers ------------------------------------------------------------------

    def personal(self, ctx, value):
        return self.store.sharing_setting(ctx, {'contributions_default': value})

    def ingest(self, connection='a-maple', project='maple', *, index=0, sharing='private', ctx=None,
               session='s1', tag='', origin=None):
        row = self.fixture['messages'][index]
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit' if row['role'] == 'user' else 'Stop',
            'event_id': row['id'] + ':' + connection + tag, 'session_id': session,
            'turn_id': row['id'] + tag, 'timestamp': row['time'], 'prompt': row['text'],
            'last_assistant_message': row['text'], 'revision': '1'}, project, connection, sharing=sharing)
        if origin is not None:
            event['origin_label'] = origin  # as origin-labelled capture sends it (agentclient.origin)
        return self.store.ingest_general(ctx or self.alice, event)['source_id']

    def drain(self):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('no LLM')):
            while SourceIndex(self.store).run(max_sources=50, max_seconds=30)['completed_sources']:
                pass

    def found(self, ctx, project='maple', **kwargs):
        cards = self.store.search(ctx, {'version': QUERY_VERSION, 'query': self.fixture['query'],
                                        'project': project, **kwargs})['results']
        with self.store.open() as state:
            owners = {row['document_id']: row['source_id'] for row in state.db.execute(
                'SELECT document_id,source_id FROM enterprise_dependencies WHERE document_id=ANY(?::text[])',
                ([c['id'] for c in cards],))}
        return {owners[c['id']] for c in cards}

    def documents(self, representation='raw'):
        clause = "ed.representation='raw'" if representation == 'raw' else "ed.representation!='raw'"
        with self.store.open() as state:
            return {r[0]: r[1] for r in state.db.execute('''SELECT ed.id,x.source_id FROM enterprise_documents ed
                JOIN enterprise_dependencies x ON x.document_id=ed.id WHERE ed.active=1 AND ''' + clause)}

    def layers(self, ctx, representation='raw', *, project=None, reach=None):
        from agenthub.search_reader import restrict
        documents = self.documents(representation)
        with patch.object(self.store, 'current_identity', lambda db, context: context):
            with self.store.open() as state:
                oracle = {d for d in documents if EnterpriseStore._document_allowed(self.store, state.db, ctx, d)}
                indexed = set(self.store._authorized_documents(state, ctx)) & set(documents)
            with self.store.open() as state, restrict(self.store, state, ctx, project, reach=reach):
                rls = {r[0] for r in state.db.execute('SELECT id FROM enterprise_documents') if r[0] in documents}
        return oracle, indexed, rls

    def assertLayers(self, ctx, expected, representation='raw', msg=None):
        documents = self.documents(representation)
        oracle, indexed, rls = self.layers(ctx, representation)
        expected = {d for d, s in documents.items() if s in set(expected)}
        self.assertEqual(oracle, expected, ('oracle', ctx['actor'], msg))
        self.assertEqual(indexed, expected, ('indexed', ctx['actor'], msg))
        self.assertEqual(rls, expected, ('row_level_security', ctx['actor'], msg))

    def apply(self, method, ctx, value):
        preview = getattr(self.store, method)(ctx, dict(value, confirm=False))
        self.assertTrue(preview['dry_run'])
        self.assertTrue(preview['confirm_token'], preview)
        return preview, getattr(self.store, method)(ctx, dict(value, confirm=preview['confirm_token']))

    def audit(self, operation):
        with self.store.open() as state:
            return [dict(r) for r in state.db.execute('''SELECT actor,disposition,target_id FROM enterprise_audit
                WHERE operation=? ORDER BY id''', (operation,))]

    # Find ---------------------------------------------------------------------

    def test_mine_lists_only_the_callers_sources_with_privacy_and_why(self):
        mine = self.ingest(sharing='organization')
        other = self.ingest('a-birch', 'birch', index=1, session='s2')
        bobs = self.ingest('b-maple', ctx=self.bob, index=2)
        listing = self.store.mine(self.alice, {})
        self.assertEqual({i['source_id'] for i in listing['items']}, {mine, other})
        item = next(i for i in listing['items'] if i['source_id'] == mine)
        self.assertEqual((item['sharing']['private'], item['sharing']['effective'], item['sharing']['reasons']),
                         (False, 'workspace', []))
        item = next(i for i in listing['items'] if i['source_id'] == other)
        self.assertEqual((item['sharing']['private'], item['sharing']['reasons']), (True, ['private']))
        # Filters: project, privacy, current session, capture time, sessions view.
        self.assertEqual([i['source_id'] for i in self.store.mine(self.alice, {'project': 'birch'})['items']], [other])
        self.assertEqual([i['source_id'] for i in self.store.mine(self.alice, {'sharing': 'private'})['items']], [other])
        self.assertEqual([i['source_id'] for i in self.store.mine(self.alice, {'sharing': 'shared'})['items']], [mine])
        self.assertEqual([i['source_id'] for i in self.store.mine(
            self.alice, {'project': 'maple', 'session': 'current'})['items']], [mine])
        self.assertEqual([i['source_id'] for i in self.store.mine(self.alice, {'session': 's2'})['items']], [other])
        self.assertEqual(self.store.mine(self.alice, {'until': '2000-01-01'})['items'], [])
        sessions = self.store.mine(self.alice, {'view': 'sessions'})['sessions']
        self.assertEqual(sorted(s['project'] for s in sessions), ['birch', 'maple'])
        self.assertEqual([i['source_id'] for i in self.store.mine(self.bob, {})['items']], [bobs])
        with self.assertRaises(Denied):
            self.store.mine(self.store.authenticate(self.store.enroll('acme', 'alice', 'a-ingest', ['ingest'])), {})

    # Delete -------------------------------------------------------------------

    def test_forget_previews_then_purges_through_the_canonical_path(self):
        from agenthub.data_lifecycle import CONTENT_STORES, inventory
        first = self.ingest(sharing='organization')
        second = self.ingest(index=1, session='s2', sharing='organization')
        self.drain()
        self.assertEqual(self.found(self.bob), {first, second})
        preview = self.store.forget(self.alice, {'project': 'maple', 'session': 'current'})
        self.assertEqual((preview['dry_run'], preview['sources']), (True, 1))
        self.assertEqual(self.found(self.bob), {first, second}, 'a preview changes nothing')
        # A token that no longer matches the selection is previewed again, not applied.
        stale = self.store.forget(self.alice, {'project': 'maple', 'session': 'current', 'confirm': '0' * 32})
        self.assertEqual(stale['status'], 'selection_changed')
        _, applied = self.apply('forget', self.alice, {'project': 'maple', 'session': 'current'})
        self.assertEqual((applied['deleted'], applied['held']), (1, 0))
        self.assertEqual(self.found(self.bob), {first})
        self.assertEqual(self.found(self.alice), {first})
        with self.store.open() as state:
            after = inventory(state.db, 'acme', source_ids=[second])
            journal = [r[0] for r in state.db.execute(
                'SELECT operation FROM enterprise_deletion_journal WHERE source_id=?', (second,))]
        self.assertEqual({key: after[key] for key in CONTENT_STORES if key != 'pending_object_deletes'},
                         {key: 0 for key in CONTENT_STORES if key != 'pending_object_deletes'})
        self.assertEqual(journal, ['delete'])
        self.assertEqual(self.audit('owner_delete'), [{'actor': 'alice', 'disposition': 'deleted', 'target_id': second}])
        self.assertEqual(self.store.mine(self.alice, {'state': 'all', 'session': 's2'})['items'][0]['state'], 'deleted')
        # A selection is required; deleted sources are not selected again.
        with self.assertRaises(ValueError):
            self.store.forget(self.alice, {})
        self.assertEqual(self.store.forget(self.alice, {'source_ids': [second]})['sources'], 0)

    def test_legal_hold_beats_owner_deletion_and_is_reported(self):
        holder = self.store.authenticate(self.store.enroll('acme', 'admin', 'admin-hold', ['legal_hold']))
        source = self.ingest()
        self.store.create_legal_hold(holder, 'source', source, 'synthetic matter 1')
        preview, applied = self.apply('forget', self.alice, {'source_ids': [source]})
        self.assertEqual(preview['held'], 1)
        self.assertEqual((applied['deleted'], applied['held']), (0, 1))
        with self.store.open() as state:
            row = state.db.execute('SELECT active FROM enterprise_sources WHERE id=?', (source,)).fetchone()
            body = state.db.execute('SELECT body FROM memories WHERE id=?', (source,)).fetchone()['body']
            journal = sorted(r[0] for r in state.db.execute(
                'SELECT operation FROM enterprise_deletion_journal WHERE source_id=?', (source,)))
        self.assertEqual(row['active'], 0)
        self.assertTrue(body, 'held content is preserved')
        self.assertEqual(journal, ['delete_held', 'withdraw'])
        item = self.store.mine(self.alice, {'state': 'all'})['items'][0]
        self.assertEqual(item['state'], 'deletion_held')

    # Only the owner ---------------------------------------------------------------

    def test_non_owners_cannot_delete_change_sharing_or_grant(self):
        source = self.ingest()
        self.drain()
        for method, extra in (('forget', {}), ('change_sharing', {'sharing': 'shared'})):
            preview = getattr(self.store, method)(self.bob, dict({'source_ids': [source]}, **extra))
            self.assertEqual((preview['sources'], preview['confirm_token']), (0, None), method)
        with self.assertRaises(Denied):
            self.store.grant(self.bob, {'scope': 'source', 'ids': [source], 'grantee_kind': 'principal',
                                        'grantee': 'bob'})
        with self.assertRaises(Denied):
            self.store.revoke(self.store.authenticate(self.store.enroll('acme', 'bob', 'b-read', ['read'])),
                              {'scope': 'source', 'ids': [source], 'grantee_kind': 'principal', 'grantee': 'bob',
                               'confirm': True})
        with self.assertRaises(Denied):
            self.store.forget(self.store.authenticate(self.store.enroll('acme', 'alice', 'a-read', ['read'])),
                              {'source_ids': [source]})
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT active,private FROM enterprise_sources WHERE id=?',
                                              (source,)).fetchone(), {'active': 1, 'private': 1})

    # Change sharing -------------------------------------------------------------------

    def test_making_private_hides_on_the_next_search_and_every_change_is_audited(self):
        source = self.ingest(sharing='organization')
        self.drain()
        self.assertEqual(self.found(self.dave), {source}, 'the organization entry reaches dave')
        self.assertEqual(self.found(self.bob), {source})
        _, applied = self.apply('change_sharing', self.alice, {'source_ids': [source], 'sharing': 'private'})
        self.assertEqual(applied['changed'], 1)
        self.assertEqual(self.found(self.dave), set())
        self.assertEqual(self.found(self.bob), set())
        self.assertEqual(self.found(self.alice), {source})
        self.apply('change_sharing', self.alice, {'source_ids': [source], 'sharing': 'shared'})
        self.assertEqual(self.found(self.bob), {source})
        with self.store.open() as state:
            changes = [dict(r) for r in state.db.execute('''SELECT actor,tier,from_level,to_level,requested
                FROM enterprise_sharing_changes WHERE source_id=? ORDER BY id''', (source,))]
        self.assertEqual(changes, [
            {'actor': 'alice', 'tier': 'passages', 'from_level': 'shared', 'to_level': 'private', 'requested': 'private'},
            {'actor': 'alice', 'tier': 'passages', 'from_level': 'private', 'to_level': 'shared', 'requested': 'shared'}])
        self.assertEqual([a['disposition'] for a in self.audit('source_sharing')], ['shared>private', 'private>shared'])
        self.assertLayers(self.bob, {source})
        self.assertLayers(self.dave, {source})

    def test_shared_items_follow_the_current_access_list(self):
        source = self.ingest(sharing='private')
        self.drain()
        self.assertEqual(self.found(self.bob), set())
        preview, applied = self.apply('change_sharing', self.alice, {'source_ids': [source], 'sharing': 'shared'})
        self.assertEqual((preview['raises'], applied['shared_after']), (1, 1))
        self.assertLayers(self.bob, {source})
        self.assertLayers(self.dave, {source})
        # Removing the organization entry narrows every item of the workspace at once.
        self.store.operator_workspace('remove-entry', 'maple', subject_kind='organization')
        self.assertEqual(self.found(self.dave), set())
        self.assertLayers(self.dave, set())
        self.assertLayers(self.bob, {source})
        # The person's own default makes new items private; existing ones on request only.
        self.personal(self.alice, 'private')
        later = self.ingest(index=1, sharing='organization', session='s2')
        self.drain()
        self.assertLayers(self.bob, {source})
        result = self.store.sharing_setting(self.alice, {'contributions_default': 'private', 'apply_to_existing': True})
        self.assertEqual(result['existing']['sources'], 1)
        self.store.sharing_setting(self.alice, {'contributions_default': 'private', 'apply_to_existing': True,
                                                'confirm': result['existing']['confirm_token']})
        self.assertLayers(self.bob, set())
        self.assertLayers(self.alice, {source, later})

    def test_raising_needs_share_and_the_operator_can_restrict_it_tenant_wide(self):
        source = self.ingest(sharing='private')
        lower_only = self.store.authenticate(self.store.enroll('acme', 'alice', 'a-withdraw', ['read', 'withdraw']))
        refused = self.store.change_sharing(lower_only, {'source_ids': [source], 'sharing': 'shared'})
        self.assertEqual((refused['status'], refused['reason'], refused['confirm_token']),
                         ('refused', 'sharing_needs_the_share_action', None))
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_organizations SET owner_sharing='lower_only' WHERE id='acme'")
        refused = self.store.change_sharing(self.alice, {'source_ids': [source], 'sharing': 'shared'})
        self.assertEqual(refused['reason'], 'operator_restricts_owner_sharing_to_lowering')
        refused = self.store.grant(self.alice, {'scope': 'source', 'ids': [source], 'grantee_kind': 'principal',
                                                'grantee': 'dave'})
        self.assertEqual(refused['status'], 'refused')
        shared = self.ingest(index=1, sharing='organization', session='s2')
        _, applied = self.apply('change_sharing', lower_only, {'source_ids': [shared], 'sharing': 'private'})
        self.assertEqual(applied['changed'], 1, 'making private stays available')

    def test_origin_ceiling_caps_a_raise_and_a_grant(self):
        """A source whose origin is private stays private, even for its owner's change."""
        source = self.ingest(sharing='private', origin={'ceiling': 'private', 'kinds': ['outside_repo'],
                                                        'reason': 'personal_content', 'projects': [],
                                                        'inherited': False})
        plain = self.ingest(index=1, sharing='private', session='s2')
        self.drain()
        preview, applied = self.apply('change_sharing', self.alice, {'source_ids': [source, plain],
                                                                     'sharing': 'shared'})
        self.assertEqual((preview['private_after'], preview['shared_after']), (1, 1))
        self.assertIn('origin', preview['capped'])
        with self.store.open() as state:
            stored = {r['id']: r['private'] for r in state.db.execute(
                'SELECT id,private FROM enterprise_sources WHERE id=ANY(?::text[])', ([source, plain],))}
        self.assertEqual(stored, {source: 1, plain: 0})
        item = next(i for i in self.store.mine(self.alice, {})['items'] if i['source_id'] == source)
        self.assertIn('origin', item['sharing']['reasons'])
        # No grant carries it past its origin, and there is no override.
        grant = self.apply('grant', self.alice, {'scope': 'source', 'ids': [source, plain],
                                                 'grantee_kind': 'principal', 'grantee': 'dave'})[0]
        self.assertEqual(grant['kept_by_origin'], 1)
        self.assertEqual(self.found(self.dave), {plain})
        self.assertLayers(self.dave, {plain})
        self.assertLayers(self.bob, {plain})

    # Grants -----------------------------------------------------------------------------

    def test_grants_name_readers_are_revocable_and_audited(self):
        from agenthub.source_evidence import document_evidence
        source = self.ingest(sharing='private')
        self.drain()
        self.assertEqual(self.found(self.bob), set())
        preview, granted = self.apply('grant', self.alice, {'scope': 'source', 'ids': [source],
                                                            'grantee_kind': 'principal', 'grantee': 'dave'})
        self.assertEqual((preview['sources_covered'], granted['granted']), (1, 1))
        # The source is private: the share alone admits dave.
        self.assertEqual(self.found(self.dave), {source})
        self.assertEqual(self.found(self.dave, project='birch'), {source}, 'a share reaches across workspaces')
        self.assertEqual(self.found(self.bob), set())
        self.assertLayers(self.dave, {source})
        self.assertLayers(self.bob, set())
        card = next(iter(self.documents()))
        with self.store.open() as state:
            revision = state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',
                                        (card,)).fetchone()[0]
        self.assertTrue(document_evidence(self.store, self.dave, card, revision=revision)['sources'])
        with self.assertRaises(Denied):
            self.store.general_source(self.dave, source)
        listing = self.store.grants(self.alice)
        self.assertEqual([(g['grantee'], g['tiers']) for g in listing['issued']], [('dave', ['passages'])])
        self.assertEqual([g['from'] for g in self.store.grants(self.dave)['received']], ['alice'])
        with self.assertRaises(ValueError):
            self.store.revoke(self.alice, {'scope': 'source', 'ids': [source], 'grantee_kind': 'principal',
                                           'grantee': 'dave'})
        revoked = self.store.revoke(self.alice, {'scope': 'source', 'ids': [source], 'grantee_kind': 'principal',
                                                 'grantee': 'dave', 'confirm': True})
        self.assertEqual(revoked['revoked'], 1)
        self.assertEqual(self.found(self.dave), set())
        self.assertLayers(self.dave, set())
        self.assertEqual([a['disposition'] for a in self.audit('owner_grant')], ['granted', 'revoked'])
        self.assertFalse(self.store.grants(self.alice)['issued'])
        self.assertEqual(self.store.grants(self.alice, {'include_revoked': True})['issued'][0]['revoked_by'], 'alice')

    def test_session_and_group_shares_cover_later_captures_and_members_only(self):
        first = self.ingest(sharing='private', session='shared-session')
        self.drain()
        with self.assertRaisesRegex(ValueError, 'project_grantee_retired'):
            self.store.grant(self.alice, {'scope': 'session', 'ids': ['shared-session'], 'project': 'maple',
                                          'grantee_kind': 'project', 'grantee': 'birch'})
        self.apply('grant', self.alice, {'scope': 'session', 'ids': ['shared-session'], 'project': 'maple',
                                         'grantee_kind': 'group', 'grantee': 'birch-members', 'originals': True})
        later = self.ingest(index=1, sharing='private', session='shared-session')
        elsewhere = self.ingest(index=2, sharing='private', session='other-session')
        self.drain()
        # carol is in birch-members; bob is not; originals also need source_read.
        self.assertEqual(self.found(self.carol), {first, later})
        self.assertEqual(self.found(self.bob), set())
        self.assertLayers(self.carol, {first, later})
        self.assertLayers(self.bob, set())
        self.assertEqual(self.store.general_source(self.carol, later)['source_id'], later)
        with self.assertRaises(Denied):
            self.store.general_source(self.carol, elsewhere)
        reader = self.store.authenticate(self.store.enroll('acme', 'carol', 'c-read', ['read']))
        with self.assertRaises(Denied):
            self.store.general_source(reader, later)
        # Leaving the group ends access.
        self.store.set_membership('acme', 'birch', 'carol', False)
        self.assertLayers(self.carol, set())

    def test_operator_restriction_switches_off_owner_grants_but_not_policy_grants(self):
        from agenthub import raw_access
        source = self.ingest(sharing='private')
        other = self.ingest(index=1, sharing='private', session='s2')
        self.drain()
        self.apply('grant', self.alice, {'scope': 'source', 'ids': [source], 'grantee_kind': 'principal',
                                         'grantee': 'dave'})
        operator = self.store.authenticate(self.store.enroll('acme', 'alice', 'a-policy', ['read', 'policy']))
        self.apply('grant', operator, {'scope': 'source', 'ids': [other], 'grantee_kind': 'principal',
                                       'grantee': 'dave'})
        self.assertLayers(self.dave, {source, other})
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_organizations SET owner_sharing='lower_only' WHERE id='acme'")
        self.assertLayers(self.dave, {other})
        # The operator-style command keeps its originals-only semantics.
        raw_access.grant(self.store, operator, 'source', source, 'principal', 'carol')
        self.assertLayers(self.carol, set())

    # Derived documents -------------------------------------------------------------------

    def note(self, source, ctx=None):
        return self.store.accept_reviewed_note(ctx or self.alice, source, 'Orchard export ' + source[:8],
                                               'The orchard export ' + source[:8] + ' is written under the drafts '
                                               'folder.')['document_id']

    def test_derived_documents_need_the_audience_of_every_source_in_every_layer(self):
        private = self.ingest(sharing='private')
        shared = self.ingest(index=1, sharing='organization', session='s2')
        notes = {self.note(private): private, self.note(shared): shared}
        self.assertEqual(set(self.documents('derived').values()), {private, shared})
        with self.store.open() as state:
            self.assertEqual({r[0] for r in state.db.execute(
                'SELECT level_capped FROM enterprise_documents WHERE id=ANY(?::text[])', (list(notes),))}, {1})
        self.assertLayers(self.alice, {private, shared}, 'derived')
        self.assertLayers(self.bob, {shared}, 'derived')
        self.assertLayers(self.dave, {shared}, 'derived')
        self.store.operator_workspace('remove-entry', 'maple', subject_kind='organization')
        self.assertLayers(self.dave, set(), 'derived', 'the access list applies at query time')
        self.apply('change_sharing', self.alice, {'source_ids': [shared], 'sharing': 'private'})
        self.assertLayers(self.bob, set(), 'derived')
        self.assertLayers(self.alice, {private, shared}, 'derived')
        # A share of the passage never reaches a document derived from it.
        self.apply('grant', self.alice, {'scope': 'source', 'ids': [private], 'grantee_kind': 'principal',
                                         'grantee': 'dave'})
        self.assertLayers(self.dave, set(), 'derived')

    def test_parity_across_privacy_shares_and_derived_documents(self):
        sources = [self.ingest(sharing='private', tag='p0'), self.ingest(index=1, sharing='team', tag='p1'),
                   self.ingest('a-birch', 'birch', index=2, sharing='organization', tag='p2'),
                   self.ingest('b-maple', ctx=self.bob, sharing='organization', tag='p3'),
                   self.ingest(index=1, sharing='private', session='s9', tag='p4')]
        self.drain()
        for source, owner in zip(sources, (self.alice,) * 3 + (self.bob, self.alice)):
            self.note(source, owner)
        self.apply('grant', self.alice, {'scope': 'source', 'ids': [sources[0]], 'grantee_kind': 'principal',
                                         'grantee': 'dave'})
        self.apply('grant', self.alice, {'scope': 'session', 'ids': ['s9'], 'project': 'maple',
                                         'grantee_kind': 'group', 'grantee': 'birch-members'})
        self.store.create_principal('acme', 'helper')
        self.store.set_delegation('acme', 'helper', 'carol', ['read'], ['maple'], True)
        helper = self.store.authenticate(self.store.enroll('acme', 'helper', 'helper-p', ['read'], acting_for='carol'))
        readers = (self.alice, self.bob, self.carol, self.dave, helper)

        def compare(label):
            sizes = {}
            for ctx in readers:
                for representation in ('raw', 'derived'):
                    oracle, indexed, rls = self.layers(ctx, representation)
                    self.assertEqual(indexed, oracle, (label, ctx['actor'], representation, 'indexed'))
                    self.assertEqual(rls, oracle, (label, ctx['actor'], representation, 'row_level_security'))
                    sizes[(ctx['principal'], representation)] = len(oracle)
            return sizes
        baseline = compare('baseline')
        # A legacy `team` request in a workspace with an organization entry stays private (p1).
        self.assertEqual((baseline[('dave', 'raw')], baseline[('carol', 'raw')]), (2, 3))
        self.store.quarantine_principal('dave', reason='synthetic review')
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_organizations SET owner_sharing='lower_only' WHERE id='acme'")
        self.store.operator_workspace('remove-entry', 'maple', subject_kind='organization')
        lowered = compare('lowered')
        self.assertEqual(lowered[('dave', 'raw')], 0)
        self.assertLess(lowered[('carol', 'raw')], baseline[('carol', 'raw')])

    # Moderation ---------------------------------------------------------------------------

    def test_moderation_follows_the_admin_role_hides_but_never_deletes_or_reads_originals(self):
        source = self.ingest('b-maple', ctx=self.bob, sharing='organization')
        self.drain()
        # carol administers maple; an older credential holding `moderate` keeps working.
        self.store.operator_workspace('set-entry', 'maple', subject_kind='principal', subject='carol', role='admin')
        moderator = self.store.authenticate(self.store.enroll('acme', 'carol', 'c-moderate', ['read', 'moderate']))
        outsider = self.store.authenticate(self.store.enroll('acme', 'dave', 'd-moderate', ['read', 'moderate']))
        with self.assertRaises(Denied):
            self.store.moderate(outsider, {'project': 'maple', 'source_ids': [source], 'reason': 'off topic'})
        with self.assertRaises(Denied):
            self.store.moderate(self.alice, {'project': 'maple', 'source_ids': [source], 'reason': 'off topic'})
        plain = self.store.authenticate(self.store.enroll('acme', 'carol', 'c-plain', ['read', 'withdraw']))
        with self.assertRaises(Denied):
            self.store.moderate(plain, {'project': 'maple', 'source_ids': [source], 'reason': 'off topic'})
        preview, applied = self.apply('moderate', moderator, {'project': 'maple', 'source_ids': [source],
                                                              'reason': 'off topic'})
        self.assertEqual((preview['sources'], applied['hidden']), (1, 1))
        self.assertEqual(self.found(self.alice), set())
        self.assertEqual(self.found(self.bob), set())
        with self.store.open() as state:
            body = state.db.execute('SELECT body FROM memories WHERE id=?', (source,)).fetchone()['body']
            journal = [r[0] for r in state.db.execute(
                'SELECT operation FROM enterprise_deletion_journal WHERE source_id=?', (source,))]
        self.assertTrue(body, 'moderation never deletes')
        self.assertEqual(journal, ['withdraw'])
        self.assertEqual(self.audit('moderate_withdraw'), [{'actor': 'carol', 'disposition': 'applied',
                                                            'target_id': source}])
        item = self.store.mine(self.bob, {'state': 'all'})['items'][0]
        self.assertEqual((item['state'], item['moderation']['by'], item['moderation']['reason']),
                         ('hidden_by_moderator', 'carol', 'off topic'))
        with self.assertRaises(Denied):
            self.store.forget(moderator, {'source_ids': [source]})
        self.assertEqual(self.store.forget(self.carol, {'source_ids': [source]})['sources'], 0)
        with self.assertRaises(Denied):
            self.store.general_source(self.carol, source)
        # The owner can still delete it.
        _, deleted = self.apply('forget', self.bob, {'source_ids': [source]})
        self.assertEqual(deleted['deleted'], 1)


class OwnerToolTests(unittest.TestCase):
    """MCP catalogue: confirm is required and the tools are never captured back."""

    def tools(self, responses=None):
        from types import SimpleNamespace
        from agenthub.mcp_tools import MemoryTools
        calls = []

        def request(path, value=None):
            calls.append((path, value))
            return (responses or {}).get(path, {'dry_run': True, 'confirm_token': None})
        return MemoryTools('maple', SimpleNamespace(request=request)), calls

    def test_destructive_and_sharing_tools_require_confirm(self):
        from agenthub.mcp_tools import OWNER_TOOLS, enterprise_tools
        catalogue = {t['name']: t for t in enterprise_tools()}
        for name in ('forget_memories', 'change_memory_sharing', 'share_memories', 'unshare_memories'):
            self.assertIn('confirm', catalogue[name]['inputSchema']['required'], name)
        self.assertTrue(catalogue['forget_memories']['annotations']['destructiveHint'])
        self.assertTrue(set(OWNER_TOOLS) <= set(catalogue))
        tools, calls = self.tools()
        for args in ({'session': 'current'}, {'session': 'current', 'confirm': True},
                     {'session': 'current', 'confirm': 'not-a-token'}):
            with self.assertRaises(ValueError):
                tools.call('forget_memories', args)
        with self.assertRaises(ValueError):
            tools.call('unshare_memories', {'session': 'current', 'with_group': 'birch', 'confirm': False})
        self.assertEqual(calls, [])
        tools.call('forget_memories', {'session': 'current', 'confirm': False})
        self.assertEqual(calls[-1], ('/enterprise/v3/my-data/forget',
                                     {'session': 'current', 'project': 'maple', 'confirm': False}))
        tools.call('share_memories', {'session': 'current', 'with_group': 'maple', 'confirm': 'a' * 32})
        self.assertEqual(calls[-1][1]['confirm'], 'a' * 32)

    def test_owner_tools_are_self_tools_and_never_captured(self):
        from agentclient.enterprise_capture import self_tool
        from agenthub.mcp_tools import OWNER_TOOLS
        for name in OWNER_TOOLS:
            for host_name in ('mcp__vaelius__' + name, 'mcp__plugin_vaelius_vaelius__' + name,
                              'mcp__agentnetwork_memory__' + name):
                self.assertTrue(self_tool(host_name), host_name)


if __name__ == '__main__':
    unittest.main()
