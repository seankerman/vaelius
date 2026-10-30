"""Workspaces, groups, access lists and the single read predicate (docs/ACCESS_MODEL.md, PR A).

Synthetic PostgreSQL cases, no model calls. The predicate "reader is in the
item's audience" is enforced three times: the Python oracle
(agenthub.workspace_access through EnterpriseStore._document_allowed), the
indexed SQL filter (agenthub.search_permissions) and row-level security
(search_document_allowed / search_reader_in_audience). Every case asserts all
three layers agree and match the expected reader set.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agentclient.general_contract import normalize_remote
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Conflict, Denied, EnterpriseStore
from agenthub.workspace_access import RepoNotLinked
from test_cloud_postgres import PostgresFixture, SERVICES

FULL = ['ingest', 'read', 'source_read', 'feedback', 'correct', 'curate', 'withdraw', 'share', 'manage', 'policy']


def label(ceiling, reason='', projects=(), kinds=None):
    restricted = ceiling != 'organization' or projects
    return {'ceiling': ceiling, 'kinds': kinds or (['other_project'] if projects else ['web']),
            'reason': reason or ('other_project' if restricted else ''), 'projects': sorted(projects),
            'inherited': False}


class WorkspaceFixture(PostgresFixture):
    """maple (operator workspace): maple-members writer {alice, bob, quinn}; group platform
    reader {carol, group core {dave}}; alice admin. birch: alice admin, bob reader.
    oak: alice admin, organization reader. erin is in no entry; quinn is quarantined."""

    def workspace_setup(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_corpus = 'sources'
        for name in ('carol', 'dave', 'erin', 'quinn', 'frank'):
            self.store.create_principal('acme', name)
        self.store.set_membership('acme', 'maple', 'quinn', True)
        self.tokens['alice'] = self.store.enroll('acme', 'alice', 'alice-full', FULL)
        for name in ('bob', 'carol', 'dave', 'erin', 'quinn'):
            self.tokens[name] = self.store.enroll('acme', name, name + '-full', FULL)
        self.ctx = self.alice = self.store.authenticate(self.tokens['alice'])
        self.bob, self.carol, self.dave, self.erin, self.quinn = (
            self.store.authenticate(self.tokens[n]) for n in ('bob', 'carol', 'dave', 'erin', 'quinn'))
        self.store.operator_workspace('set-entry', 'maple', subject_kind='principal', subject='alice', role='admin')
        for name in ('birch', 'oak'):
            self.store.create_workspace(self.alice, {'name': name})
        self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal', 'subject': 'bob',
                                          'role': 'reader'})
        self.store.set_entry(self.alice, {'workspace': 'oak', 'subject_kind': 'organization', 'role': 'reader'})
        for name in ('platform', 'core'):
            self.store.create_group(self.alice, {'name': name})
        self.store.add_group_member(self.alice, {'group': 'platform', 'member_kind': 'principal', 'member': 'carol'})
        self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'principal', 'member': 'dave'})
        self.store.add_group_member(self.alice, {'group': 'platform', 'member_kind': 'group', 'member': 'core'})
        self.store.set_entry(self.alice, {'workspace': 'maple', 'subject_kind': 'group', 'subject': 'platform',
                                          'role': 'reader'})
        for name in ('maple', 'birch', 'oak'):
            self.store.enroll_connection(self.alice, 'a-' + name, 'a-' + name, name, ['agent'], visibility='organization')
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/raw_default_v1.json').read_text())

    def ingest(self, workspace='maple', origin_label=None, *, index=0, tag='', sharing='organization', ctx=None,
               connection=None, remote=None):
        row = self.fixture['messages'][index]
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit' if row['role'] == 'user' else 'Stop',
            'event_id': row['id'] + ':' + tag, 'session_id': self.fixture['conversation'] + tag,
            'turn_id': row['id'], 'timestamp': row['time'], 'prompt': row['text'],
            'last_assistant_message': row['text'], 'revision': '1'}, workspace, connection or 'a-' + workspace,
            sharing=sharing)
        if origin_label is not None:
            event['origin_label'] = origin_label
        if remote is not None:
            event['remote'] = remote
        return self.store.ingest_general(ctx or self.alice, event)

    def drain(self):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('no LLM')):
            while SourceIndex(self.store).run(max_sources=50, max_seconds=30)['completed_sources']:
                pass

    def documents(self, raw=True):
        with self.store.open() as state:
            return {r[0]: r[1] for r in state.db.execute('''SELECT ed.id,x.source_id FROM enterprise_documents ed
                JOIN enterprise_dependencies x ON x.document_id=ed.id WHERE ed.active=1 AND '''
                + ("ed.representation='raw'" if raw else "ed.representation!='raw'"))}

    def layers(self, ctx, raw=True):
        from agenthub.search_reader import restrict
        documents = self.documents(raw)
        with patch.object(self.store, 'current_identity', lambda db, context: context):
            with self.store.open() as state:
                oracle = {documents[d] for d in documents
                          if EnterpriseStore._document_allowed(self.store, state.db, ctx, d)}
                indexed = {documents[d] for d in set(self.store._authorized_documents(state, ctx)) & set(documents)}
            with self.store.open() as state, restrict(self.store, state, ctx, None):
                rls = {documents[r[0]] for r in state.db.execute('SELECT id FROM enterprise_documents')
                       if r[0] in documents}
        return oracle, indexed, rls

    def assertLayers(self, ctx, expected, msg=None, *, raw=True):
        oracle, indexed, rls = self.layers(ctx, raw)
        names = {v: k for k, v in self.items.items()} if hasattr(self, 'items') else {}
        show = lambda found: sorted(names.get(i, i) for i in found)
        self.assertEqual(show(oracle), show(expected), ('oracle', msg))
        self.assertEqual(show(indexed), show(expected), ('indexed', msg))
        self.assertEqual(show(rls), show(expected), ('row_level_security', msg))


class ParityItems(WorkspaceFixture):
    def parity_setup(self):
        self.workspace_setup()
        source = lambda result: result['source_id']
        self.items = {
            'm_shared': source(self.ingest(tag='m-shared')),
            'm_private': source(self.ingest(tag='m-private', sharing='private', index=1)),
            'm_birch': source(self.ingest(origin_label=label('organization', projects=['birch']), tag='m-birch', index=2)),
            'm_team': source(self.ingest(origin_label=label('team', 'recalled_memory', kinds=['recalled_memory']),
                                         tag='m-team')),
            'm_team_oak': source(self.ingest(origin_label=label('team', projects=['oak']), tag='m-team-oak', index=1)),
            'm_origin_private': source(self.ingest(origin_label=label('private', 'personal_content',
                                                                      kinds=['outside_repo']), tag='m-op', index=2)),
            'o_shared': source(self.ingest('oak', tag='o-shared')),
            'o_maple': source(self.ingest('oak', origin_label=label('organization', projects=['maple']),
                                          tag='o-maple', index=1)),
            'm_share_erin': source(self.ingest(tag='m-share-erin', sharing='private', index=2)),
            'm_share_group': source(self.ingest(tag='m-share-group', sharing='private')),
            'm_birch_share': source(self.ingest(origin_label=label('organization', projects=['birch']),
                                                tag='m-birch-share', sharing='private', index=1)),
        }
        for item, kind, grantee in (('m_share_erin', 'principal', 'erin'), ('m_share_group', 'group', 'platform'),
                                    ('m_birch_share', 'principal', 'carol')):
            preview = self.store.grant(self.alice, {'scope': 'source', 'ids': [self.items[item]],
                                                    'grantee_kind': kind, 'grantee': grantee})
            self.store.grant(self.alice, {'scope': 'source', 'ids': [self.items[item]], 'grantee_kind': kind,
                                          'grantee': grantee, 'confirm': preview['confirm_token']})
        self.store.quarantine_principal('quinn', reason='synthetic review')
        self.drain()

    def expect(self, *names):
        return {self.items[name] for name in names}


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class PredicateParityTests(ParityItems, unittest.TestCase):
    def setUp(self):
        self.parity_setup()

    def tearDown(self):
        self.postgres_teardown()

    def test_every_layer_agrees_on_roles_groups_private_shares_carried_origin_and_quarantine(self):
        everything = set(self.items.values())
        self.assertLayers(self.alice, everything, 'owner reads everything of theirs')
        self.assertLayers(self.bob, self.expect('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple'),
                          'writer in maple, reader in birch, organization reader of oak')
        group_reader = self.expect('m_shared', 'm_team', 'o_shared', 'o_maple', 'm_share_group')
        self.assertLayers(self.carol, group_reader, 'direct member of a group entry; group share')
        self.assertLayers(self.dave, group_reader, 'nested group member')
        self.assertLayers(self.erin, self.expect('o_shared', 'm_share_erin'), 'organization entry and a share only')
        self.assertLayers(self.quinn, self.expect('m_shared', 'm_team'),
                          'quarantined: direct entries stay, organization and share paths go')

    def test_delegated_credential_has_no_organization_role_and_stays_in_its_workspaces(self):
        self.store.set_delegation('acme', 'frank', 'bob', ['read', 'source_read'], ['maple'], True)
        token = self.store.enroll('acme', 'frank', 'frank-for-bob', ['read'], acting_for='bob')
        frank = self.store.authenticate(token)
        self.assertLayers(frank, self.expect('m_shared', 'm_team'),
                          'carried birch is outside the delegated workspaces')
        self.store.set_delegation('acme', 'frank', 'bob', ['read', 'source_read'], ['maple', 'oak', 'birch'], True)
        frank = self.store.authenticate(token)
        self.assertLayers(frank, self.expect('m_shared', 'm_team', 'm_birch'),
                          'never the organization entry, even in a delegated workspace')

    def test_widest_entry_suspends_wider_entries_at_query_time_and_raising_restores(self):
        self.store.tenant_policy(None, {'widest_entry': 'people_and_groups'})
        self.assertLayers(self.erin, self.expect('m_share_erin'), 'organization entry suspended')
        self.assertLayers(self.bob, self.expect('m_shared', 'm_birch', 'm_team'), 'organization entry suspended')
        self.store.tenant_policy(None, {'widest_entry': 'none'})
        self.assertLayers(self.bob, set(), 'only principal admin entries count')
        self.assertLayers(self.carol, self.expect('m_share_group'), 'groups are suspended but shares are not')
        self.assertLayers(self.alice, set(self.items.values()))
        with self.store.open() as state:
            entries = state.db.execute("SELECT count(*) FROM workspace_entries WHERE tenant='acme'").fetchone()[0]
        self.assertGreater(entries, 0, 'lowering never deletes entries')
        self.store.tenant_policy(None, {'widest_entry': 'organization'})
        self.assertLayers(self.bob, self.expect('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple'), 'restored')

    def test_changes_apply_on_the_next_request(self):
        self.store.remove_group_member(self.alice, {'group': 'core', 'member_kind': 'principal', 'member': 'dave'})
        self.assertLayers(self.dave, self.expect('o_shared'), 'removed from the nested group (o_maple carries maple)')
        self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'group', 'subject': 'platform',
                                          'role': 'reader'})
        self.assertLayers(self.carol, self.expect('m_shared', 'm_team', 'o_shared', 'o_maple', 'm_share_group',
                                                  'm_birch', 'm_birch_share'),
                          'adding an entry widens past items; the carried workspace now admits carol')
        self.store.workspace_settings(self.alice, {'workspace': 'birch', 'active': False})
        self.assertLayers(self.bob, self.expect('m_shared', 'm_team', 'o_shared', 'o_maple'),
                          'archiving a carried workspace removes its readers')
        self.assertLayers(self.alice, set(self.items.values()), 'owners keep everything')
        self.store.change_sharing(self.alice, {'source_ids': [self.items['o_shared']], 'sharing': 'private',
            'confirm': self.store.change_sharing(self.alice, {'source_ids': [self.items['o_shared']],
                                                              'sharing': 'private'})['confirm_token']})
        self.assertLayers(self.erin, self.expect('m_share_erin'), 'made private')

    def test_derived_documents_need_the_audience_of_every_source_and_never_follow_shares(self):
        for number, name in enumerate(('m_shared', 'm_private', 'm_share_erin', 'o_maple')):
            self.store.accept_reviewed_note(self.alice, self.items[name], 'Synthetic derived note ' + str(number),
                                            'Synthetic derived dataset ' + str(number) + ' lives at /synthetic/d'
                                            + str(number) + '.csv.')
        self.assertLayers(self.alice, self.expect('m_shared', 'm_private', 'm_share_erin', 'o_maple'), raw=False)
        self.assertLayers(self.bob, self.expect('m_shared', 'o_maple'), raw=False)
        self.assertLayers(self.erin, set(), 'a passage share never reaches derived documents', raw=False)

    def test_origin_is_fixed_and_an_unknown_carried_workspace_admits_only_the_owner(self):
        unknown = self.ingest(origin_label=label('organization', projects=['nowhere']), tag='unknown')['source_id']
        self.drain()
        bob = self.expect('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple')
        self.assertLayers(self.bob, bob, 'an unknown carried workspace has no audience')
        self.assertLayers(self.alice, set(self.items.values()) | {unknown})
        private = self.items['m_origin_private']
        for column, value in (('origin_ceiling', "'organization'"), ('origin_ceiling', 'NULL'),
                              ('origin_projects', "'[\"oak\"]'"), ('origin_reason', 'NULL')):
            with self.subTest(column=column, value=value):
                with self.assertRaisesRegex(Exception, 'origin_label_is_fixed'):
                    with self.store.open() as state, state.db:
                        state.db.execute('UPDATE enterprise_sources SET ' + column + '=' + value + ' WHERE id=?',
                                         (private,))
        # Even a hand-cleared private flag never passes an origin-private source.
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_sources SET private=0 WHERE id=?', (private,))
        self.assertLayers(self.bob, bob)

    def test_project_reach_adds_only_organization_wide_items_from_other_workspaces(self):
        from agenthub.search_reader import restrict
        documents = self.documents()
        with self.store.open() as state, restrict(self.store, state, self.bob, 'maple', reach='project'):
            reached = {documents[r[0]] for r in state.db.execute('SELECT id FROM enterprise_documents')
                       if r[0] in documents}
        # o_maple carries maple, which has no organization entry: not organization-wide.
        self.assertEqual(reached, self.expect('m_shared', 'm_birch', 'm_team', 'o_shared'))
        with self.store.open() as state, restrict(self.store, state, self.bob, 'maple', reach='all'):
            reached = {documents[r[0]] for r in state.db.execute('SELECT id FROM enterprise_documents')
                       if r[0] in documents}
        self.assertEqual(reached, self.expect('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple'))

    def test_explain_lists_the_inherited_audience(self):
        with self.store.open() as state:
            row = state.db.execute('SELECT * FROM enterprise_sources WHERE id=?', (self.items['m_shared'],)).fetchone()
            readers = self.store._passage_readers(state.db, dict(row))
            private = state.db.execute('SELECT * FROM enterprise_sources WHERE id=?',
                                       (self.items['m_private'],)).fetchone()
            self.assertEqual(self.store._passage_readers(state.db, dict(private)), ['alice'])
        self.assertEqual(readers, ['alice', 'bob', 'carol', 'dave', 'quinn'])

    def test_search_runs_the_same_number_of_statements_for_owner_member_and_organization_reader(self):
        from agenthub.postgres import PostgresConnection
        def measure(ctx):
            sql = []
            execute = PostgresConnection.execute
            def counted(db, query, params=None):
                sql.append(query)
                return execute(db, query, params)
            with patch.object(PostgresConnection, 'execute', counted):
                result = self.store.search(ctx, {'version': 'enterprise-local-1', 'query': self.fixture['query'],
                                                 'project': 'maple'})
            return len(sql), len(result['results'])
        owner, member, organization = measure(self.alice), measure(self.bob), measure(self.erin)
        self.assertGreater(owner[1], member[1])
        self.assertEqual(owner[0], member[0])
        self.assertEqual(owner[0], organization[0])


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class WorkspaceManagementTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        self.workspace_setup()

    def tearDown(self):
        self.postgres_teardown()

    def audience(self, workspace):
        with self.store.open() as state:
            rows = state.db.execute('''SELECT who.id,(search_workspace_role(who.tenant,who.id,?,NULL)).role role
                FROM enterprise_principals who WHERE who.tenant='acme' AND who.active=1 ORDER BY who.id''',
                (workspace,)).fetchall()
        return {r['id']: r['role'] for r in rows if r['role']}

    def test_every_mode_of_the_access_list_table_is_an_access_list(self):
        rank = {'reader': 1, 'writer': 2, 'admin': 3}
        modes = {
            'fully-private': [],
            'chosen-people': [('principal', 'bob', 'writer'), ('principal', 'carol', 'reader')],
            'team': [('group', 'platform', 'writer')],
            'org-read-team-write': [('group', 'platform', 'writer'), ('organization', None, 'reader')],
            'org-read-write': [('organization', None, 'writer')],
            'read-only-group': [('group', 'core', 'reader')],
        }
        everyone = {'alice', 'bob', 'carol', 'dave', 'erin', 'frank', 'quinn', 'admin'}
        expected = {
            'fully-private': {'alice': 3},
            'chosen-people': {'alice': 3, 'bob': 2, 'carol': 1},
            'team': {'alice': 3, 'carol': 2, 'dave': 2},
            'org-read-team-write': dict({name: 1 for name in everyone}, alice=3, carol=2, dave=2),
            'org-read-write': dict({name: 2 for name in everyone}, alice=3),
            'read-only-group': {'alice': 3, 'dave': 1},
        }
        for mode, entries in modes.items():
            with self.subTest(mode=mode):
                self.store.create_workspace(self.alice, {'name': mode})
                for kind, subject, role in entries:
                    self.store.set_entry(self.alice, {'workspace': mode, 'subject_kind': kind, 'subject': subject,
                                                      'role': role})
                self.assertEqual(self.audience(mode), expected[mode])
                self.assertTrue(all(value in rank.values() for value in expected[mode].values()))

    def test_self_service_creation_admin_rule_and_tenant_restriction(self):
        created = self.store.create_workspace(self.bob, {'name': 'willow'})
        self.assertEqual(created['role'], 'admin')
        with self.assertRaises(Conflict):
            self.store.create_workspace(self.carol, {'name': 'willow'})
        no_manage = self.store.authenticate(self.store.enroll('acme', 'erin', 'erin-plain', ['read', 'ingest']))
        with self.assertRaises(Denied):
            self.store.create_workspace(no_manage, {'name': 'elder'})
        self.store.tenant_policy(None, {'workspace_creation': 'admins'})
        with self.assertRaises(Denied):
            self.store.create_workspace(self.carol, {'name': 'elder'})
        settings_admin = self.store.authenticate(self.store.enroll('acme', 'admin', 'admin-full', FULL + ['settings']))
        self.store.create_workspace(settings_admin, {'name': 'elder'})
        # The last counting admin stays; a synced group never counts towards it.
        with self.store.open() as state, state.db:
            state.db.execute('''INSERT INTO groups(tenant,id,name,source,external_id,created)
                VALUES('acme','scim-x','scim-x','scim','x',0)''')
            state.db.execute('''INSERT INTO group_edges(tenant,group_id,member_kind,member,created)
                VALUES('acme','scim-x','principal','carol',0)''')
            state.db.execute("SELECT access_refresh_groups('acme',ARRAY['scim-x'])")
        self.store.set_entry(self.bob, {'workspace': 'willow', 'subject_kind': 'group', 'subject': 'scim-x',
                                        'role': 'admin'})
        with self.assertRaisesRegex(ValueError, 'last_admin'):
            self.store.remove_entry(self.bob, {'workspace': 'willow', 'subject_kind': 'principal', 'subject': 'bob'})
        self.store.set_entry(self.bob, {'workspace': 'willow', 'subject_kind': 'principal', 'subject': 'dave',
                                        'role': 'admin'})
        self.store.remove_entry(self.bob, {'workspace': 'willow', 'subject_kind': 'principal', 'subject': 'bob'})
        # Break-glass: a settings administrator manages any access list, audited.
        self.store.set_entry(settings_admin, {'workspace': 'willow', 'subject_kind': 'principal', 'subject': 'erin',
                                              'role': 'reader'})
        with self.store.open() as state:
            self.assertTrue(state.db.execute('''SELECT 1 FROM enterprise_audit WHERE operation='workspace_admin_override'
                AND actor='admin' ''').fetchone())
        # A writer cannot manage the access list.
        with self.assertRaises(Denied):
            self.store.set_entry(self.bob, {'workspace': 'maple', 'subject_kind': 'principal', 'subject': 'erin',
                                            'role': 'reader'})
        # widest_entry refuses entries wider than it allows.
        self.store.tenant_policy(None, {'widest_entry': 'people_and_groups'})
        with self.assertRaisesRegex(ValueError, 'entry_wider_than_tenant_allows'):
            self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'organization', 'role': 'reader'})

    def test_roles_bound_credential_actions_and_owners_keep_self_service(self):
        # bob is a reader in birch: no capture connection, no share, no moderation there.
        with self.assertRaises(Denied):
            self.store.enroll_connection(self.bob, 'b-birch', 'b-birch', 'birch', ['agent'], visibility='organization')
        with self.store.open() as state:
            self.assertEqual(self.store._effective_actions(state.db, self.bob, 'birch'),
                             {'read', 'feedback', 'source_read'})
        with self.assertRaises(Denied):
            self.store.set_entry(self.bob, {'workspace': 'birch', 'subject_kind': 'principal', 'subject': 'erin',
                                            'role': 'reader'})
        # bob writes in maple; a writer cannot moderate (admin role).
        self.store.enroll_connection(self.bob, 'b-maple', 'b-maple', 'maple', ['agent'], visibility='organization')
        mine = self.ingest(ctx=self.bob, connection='b-maple', tag='bob-own')['source_id']
        theirs = self.ingest(tag='alice-own', index=1)['source_id']
        with self.assertRaises(Denied):
            self.store.moderate(self.bob, {'project': 'maple', 'source_ids': [theirs], 'reason': 'synthetic'})
        preview = self.store.moderate(self.alice, {'project': 'maple', 'source_ids': [mine], 'reason': 'synthetic'})
        self.assertEqual(preview['sources'], 1)
        # Demoted to nothing, bob still finds, makes private and deletes his own items, but cannot share.
        self.store.set_membership('acme', 'maple', 'bob', False)
        found = self.store.mine(self.bob, {'project': 'maple'})
        self.assertEqual([item['source_id'] for item in found['items']], [mine])
        made = self.store.change_sharing(self.bob, {'source_ids': [mine], 'sharing': 'private'})
        self.assertIsNotNone(made['confirm_token'])
        self.store.change_sharing(self.bob, {'source_ids': [mine], 'sharing': 'private',
                                             'confirm': made['confirm_token']})
        refused = self.store.change_sharing(self.bob, {'source_ids': [mine], 'sharing': 'shared'})
        self.assertEqual(refused.get('reason'), 'sharing_needs_the_writer_role')
        refused = self.store.grant(self.bob, {'scope': 'source', 'ids': [mine], 'grantee_kind': 'principal',
                                              'grantee': 'erin'})
        self.assertEqual(refused.get('reason'), 'sharing_needs_the_writer_role')
        with self.assertRaises(Denied):
            self.ingest(ctx=self.bob, connection='b-maple', tag='bob-after')
        gone = self.store.forget(self.bob, {'source_ids': [mine]})
        self.store.forget(self.bob, {'source_ids': [mine], 'confirm': gone['confirm_token']})
        # A legacy project grantee is refused; grants name principals or groups.
        with self.assertRaisesRegex(ValueError, 'project_grantee_retired'):
            self.store.grant(self.alice, {'scope': 'source', 'ids': [theirs], 'grantee_kind': 'project',
                                          'grantee': 'maple'})

    def test_repository_links(self):
        for url in ('https://example.com/Example-Org/Maple.git', 'ssh://git@example.com:22/example-org/maple',
                    'git@example.com:example-org/maple.git'):
            self.assertEqual(normalize_remote(url), 'example.com/example-org/maple')
        self.assertEqual(normalize_remote('https://example.com/group/sub/repo/'), 'example.com/group/sub/repo')
        linked = self.store.link_repo(self.alice, {'workspace': 'maple',
                                                   'remote': 'https://example.com/Example-Org/Maple.git'})
        self.assertEqual(linked['remote'], 'example.com/example-org/maple')
        with self.assertRaises(Conflict):
            self.store.link_repo(self.alice, {'workspace': 'birch', 'remote': 'example.com/example-org/maple'})
        with self.assertRaises(Denied):
            self.store.link_repo(self.bob, {'workspace': 'maple', 'remote': 'example.com/example-org/other'})
        # Captures: linked remote accepted; a remote linked elsewhere, or an unlinked one from a
        # non-admin, is rejected; no remote is accepted for a writer.
        self.store.enroll_connection(self.bob, 'b-maple', 'b-maple', 'maple', ['agent'], visibility='organization')
        self.ingest(ctx=self.bob, connection='b-maple', tag='linked', remote='example.com/example-org/maple')
        self.ingest(ctx=self.bob, connection='b-maple', tag='no-remote')
        with self.assertRaises(RepoNotLinked):
            self.ingest(ctx=self.bob, connection='b-maple', tag='unlinked', remote='example.com/example-org/birch')
        self.store.link_repo(self.alice, {'workspace': 'birch', 'remote': 'example.com/example-org/birch'})
        with self.assertRaises(RepoNotLinked):
            self.ingest(ctx=self.bob, connection='b-maple', tag='elsewhere', remote='example.com/example-org/birch')
        # An admin's first capture from an unlinked repository links it (audited, with a notice).
        result = self.ingest(tag='auto', remote='example.com/example-org/fresh')
        self.assertIn('linked example.com/example-org/fresh', result['notice'])
        with self.store.open() as state:
            row = state.db.execute("SELECT workspace FROM workspace_repos WHERE remote='example.com/example-org/fresh'").fetchone()
            audited = state.db.execute("SELECT disposition FROM enterprise_audit WHERE operation='repo_link'").fetchall()
        self.assertEqual(row['workspace'], 'maple')
        self.assertIn('automatic', [r['disposition'] for r in audited])
        self.store.unlink_repo(self.alice, {'workspace': 'maple', 'remote': 'example.com/example-org/fresh'})
        with self.assertRaises(RepoNotLinked):
            self.ingest(ctx=self.bob, connection='b-maple', tag='after-unlink', remote='example.com/example-org/fresh')

    def test_groups_refuse_cycles_rebuild_closure_and_stale_groups_contribute_nobody(self):
        with self.assertRaisesRegex(ValueError, 'group_cycle'):
            self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'group', 'member': 'platform'})
        with self.assertRaisesRegex(ValueError, 'group_cycle'):
            self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'group', 'member': 'core'})
        def closure(group):
            with self.store.open() as state:
                return sorted(r[0] for r in state.db.execute(
                    "SELECT principal FROM group_closure WHERE tenant='acme' AND group_id=?", (group,)))
        # A local group's creator is its first admin, and a member.
        self.assertEqual(closure('platform'), ['alice', 'carol', 'dave'])
        self.store.create_group(self.alice, {'name': 'leaf'})
        self.store.add_group_member(self.alice, {'group': 'leaf', 'member_kind': 'principal', 'member': 'erin'})
        self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'group', 'member': 'leaf'})
        self.assertEqual(closure('platform'), ['alice', 'carol', 'dave', 'erin'])
        self.assertEqual(self.audience('maple').get('erin'), 1)
        self.store.remove_group_member(self.alice, {'group': 'core', 'member_kind': 'group', 'member': 'leaf'})
        self.assertEqual(closure('platform'), ['alice', 'carol', 'dave'])
        self.store.delete_group(self.alice, {'group': 'leaf'})
        # A SCIM group awaiting reconcile (stale) contributes nobody.
        with self.store.open() as state, state.db:
            state.db.execute('''INSERT INTO groups(tenant,id,name,source,external_id,sync_state,created)
                VALUES('acme','scim-y','scim-y','scim','y','stale',0)''')
            state.db.execute('''INSERT INTO group_edges(tenant,group_id,member_kind,member,created)
                VALUES('acme','scim-y','principal','erin',0)''')
            state.db.execute("SELECT access_refresh_groups('acme',ARRAY['scim-y'])")
        self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'group', 'subject': 'scim-y',
                                          'role': 'reader'})
        self.assertNotIn('erin', self.audience('birch'))
        with self.assertRaisesRegex(ValueError, 'synced_group_is_read_only'):
            self.store.add_group_member(self.alice, {'group': 'scim-y', 'member_kind': 'principal', 'member': 'dave'})
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE groups SET sync_state='current' WHERE id='scim-y'")
            state.db.execute("SELECT access_refresh_groups('acme',ARRAY['scim-y'])")
        self.assertEqual(self.audience('birch').get('erin'), 1)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class SessionLabelTests(ParityItems, unittest.TestCase):
    """PR B: the superset list behind a session label and the recall filter in all three layers.

    Audiences: maple {alice, bob, quinn (quarantined), carol, dave}; birch {alice, bob};
    oak: the organization (every active principal but quinn)."""

    def setUp(self):
        self.parity_setup()

    def tearDown(self):
        self.postgres_teardown()

    def label(self, ctx, workspace, **extra):
        return self.store.session_filter(ctx, dict(workspace=workspace, **extra))

    def superset(self, ctx, workspace, **extra):
        found = self.label(ctx, workspace, **extra)['session_label']
        return found['private'], found['workspaces']

    def test_superset_is_extensional_on_current_membership(self):
        self.assertEqual(self.superset(self.alice, 'maple'), (False, ['maple']),
                         'birch is narrower; oak misses quinn, whom quarantine removes from the organization')
        self.assertEqual(self.superset(self.alice, 'birch'), (False, ['birch', 'maple', 'oak']))
        self.assertEqual(self.superset(self.alice, 'oak'), (False, ['oak']), 'an organization session')
        self.assertEqual(self.superset(self.bob, 'birch'), (False, ['birch', 'maple', 'oak']))
        self.assertEqual(self.superset(self.alice, 'maple', narrowed=['birch']), (False, ['birch', 'maple', 'oak']),
                         'narrowing intersects: maple ∩ birch = {alice, bob}')
        self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'principal', 'member': 'erin'})
        self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal', 'subject': 'erin',
                                          'role': 'reader'})
        self.assertEqual(self.superset(self.alice, 'birch'), (False, ['birch', 'maple', 'oak']),
                         'erin is in maple through platform > core now')
        self.store.remove_group_member(self.alice, {'group': 'core', 'member_kind': 'principal', 'member': 'erin'})
        self.assertEqual(self.superset(self.alice, 'birch'), (False, ['birch', 'oak']),
                         'removing erin from the nested group takes maple out at once')

    def test_private_sessions(self):
        self.assertEqual(self.superset(self.alice, 'maple', private=True)[0], True)
        self.assertEqual(self.superset(self.alice, 'maple', sharing='private')[0], True)
        self.store.create_workspace(self.alice, {'name': 'solo'})
        self.assertEqual(self.superset(self.alice, 'solo'), (True, ['birch', 'maple', 'oak', 'solo']),
                         'an audience of the person alone is a private session: everything they can read')
        self.store.sharing_setting(self.alice, {'contributions_default': 'private'})
        self.assertTrue(self.superset(self.alice, 'maple')[0], 'contributions_default private')
        self.store.sharing_setting(self.alice, {'contributions_default': 'shared'})
        self.store.workspace_settings(self.alice, {'workspace': 'birch', 'item_default': 'private'})
        self.assertTrue(self.superset(self.alice, 'birch')[0], 'item_default private')
        with self.assertRaises(Denied):
            self.label(self.alice, 'nowhere')

    def test_recall_filter_agrees_in_every_layer(self):
        cases = (
            (self.alice, 'birch', {}, ('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple'),
             'own private items, origin-private items and a team origin carrying oak are left out'),
            (self.alice, 'maple', {}, ('m_shared', 'm_team'), 'birch and oak are narrower than maple'),
            (self.alice, 'maple', {'narrowed': ['birch']}, ('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple'),
             'maple ∩ birch'),
            (self.carol, 'maple', {}, ('m_shared', 'm_team'), 'a group share to platform does not contain maple'),
            (self.erin, 'oak', {}, ('o_shared',), 'a share to erin alone does not contain the organization'),
            (self.bob, 'birch', {}, ('m_shared', 'm_birch', 'm_team', 'o_shared', 'o_maple'), 'reader of birch'),
            (self.alice, 'maple', {'private': True}, tuple(self.items), 'a private session reads everything'),
        )
        for ctx, workspace, extra, expected, message in cases:
            with self.subTest(message):
                self.assertLayers(self.label(ctx, workspace, **extra), self.expect(*expected), message)

    def test_a_share_whose_group_contains_the_session_audience_is_recalled(self):
        self.store.create_workspace(self.carol, {'name': 'pf'})
        self.store.set_entry(self.carol, {'workspace': 'pf', 'subject_kind': 'group', 'subject': 'platform',
                                          'role': 'writer'})
        found = self.label(self.carol, 'pf')['session_label']
        self.assertEqual((found['private'], found['groups']), (False, ['platform']))
        self.assertLayers(self.label(self.carol, 'pf'),
                          self.expect('m_shared', 'm_team', 'o_shared', 'o_maple', 'm_share_group'),
                          'the platform share contains {carol, dave}')
        self.assertLayers(self.label(self.dave, 'pf'),
                          self.expect('m_shared', 'm_team', 'o_shared', 'o_maple', 'm_share_group'))

    def test_widening_a_workspace_later_does_not_expose_carried_content(self):
        # An item captured in a maple session that read birch carries birch.
        self.assertLayers(self.erin, self.expect('o_shared', 'm_share_erin'))
        self.store.set_entry(self.alice, {'workspace': 'maple', 'subject_kind': 'organization', 'role': 'reader'})
        visible = self.layers(self.erin)[0]
        self.assertIn(self.items['m_shared'], visible, 'maple is now organization-wide')
        self.assertNotIn(self.items['m_birch'], visible, 'm_birch still needs birch')

    def test_descriptor_lists_the_superset_and_refuses_unreadable_targets(self):
        descriptor = self.store.session_descriptor(self.alice, {'workspace': 'birch'})
        self.assertEqual(descriptor['session'], {'workspace': 'birch', 'narrowed': [], 'private': False})
        self.assertEqual([w['id'] for w in descriptor['contains_session_audience']], ['birch', 'maple', 'oak'])
        self.assertEqual([w['id'] for w in descriptor['readable']], ['birch', 'maple', 'oak'])
        self.assertEqual(descriptor['role'], 'admin')
        self.assertEqual(descriptor['refresh_after'] - descriptor['fetched_at'], 900)
        self.assertEqual(descriptor['expires_at'] - descriptor['fetched_at'], 86400)
        before = descriptor['epoch']
        self.store.link_repo(self.alice, {'workspace': 'birch', 'remote': 'example.com/example-org/birch'})
        narrowed = self.store.session_descriptor(self.alice, {'workspace': 'maple',
                                                              'narrowed': ['example.com/example-org/birch']})
        self.assertEqual(narrowed['session']['narrowed'], ['birch'], 'a linked remote names its workspace')
        self.assertGreater(narrowed['epoch'], before, 'linking moves the epoch')
        with self.assertRaises(Denied):
            self.store.session_descriptor(self.erin, {'workspace': 'oak', 'narrowed': ['birch']})
        with self.assertRaises(Denied):
            self.store.session_descriptor(self.erin, {'workspace': 'maple'})
        with self.assertRaises(ValueError):
            self.store.session_descriptor(self.alice, {'workspace': 'maple', 'extra': 1})

    def test_descriptor_names_only_missing_people_from_the_sessions_own_access_list(self):
        # frank reads birch only; he is in nobody's session audience but birch's.
        self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal', 'subject': 'frank',
                                          'role': 'reader'})
        descriptor = self.store.session_descriptor(self.alice, {'workspace': 'maple'})
        narrower = {item['id']: item for item in descriptor['narrower']}
        self.assertEqual(sorted(narrower), ['birch', 'oak'], 'readable but not containing maple')
        birch = narrower['birch']['missing_readers']
        self.assertEqual(birch['count'], 3, 'carol, dave and quinn are in maple, not birch')
        self.assertEqual(birch['names'], ['maple-members (group)', 'platform (group)'],
                         "named only through maple's own entries")
        self.assertEqual(birch['more'], 0)
        self.assertEqual(narrower['oak']['missing_readers']['count'], 1, 'quarantined quinn is outside oak')
        shown = json.dumps(descriptor['narrower'])
        for name in ('frank', 'alice', 'bob', 'erin', 'core'):
            self.assertNotIn(name, shown, 'never a member of the target, nor anyone outside the session audience')
        self.assertEqual(narrower['birch']['hidden_items'], self.shared_items('birch'))
        alone = self.store.session_descriptor(self.alice, {'workspace': 'maple', 'narrowed': ['birch']})
        self.assertEqual(alone['narrower'], [], 'maple ∩ birch is contained by every readable workspace')
        self.assertEqual(self.store.session_descriptor(self.alice, {'workspace': 'maple', 'private': True})['narrower'],
                         [], 'a private session has no narrower workspace')

    def shared_items(self, workspace):
        with self.store.open() as state:
            return state.db.execute('''SELECT count(*) FROM enterprise_sources WHERE tenant='acme'
                AND external_project=? AND active=1 AND private=0''', (workspace,)).fetchone()[0]

    def test_access_changes_report_newly_unreadable_workspaces(self):
        added = self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal',
                                                  'subject': 'erin', 'role': 'reader'})
        self.assertEqual(added['newly_unreadable'], ['maple'], 'erin is not in maple; oak still contains her')
        self.assertIn('sessions in birch can no longer read maple', added['newly_unreadable_note'])
        self.assertNotIn('\n', added['newly_unreadable_note'])
        again = self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal',
                                                  'subject': 'erin', 'role': 'reader'})
        self.assertEqual(again['newly_unreadable'], [], 'an unchanged entry loses nothing')
        joined = self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'principal', 'member': 'erin'})
        self.assertEqual(joined['newly_unreadable'], [], 'widening maple loses nothing for birch')
        left = self.store.remove_group_member(self.alice, {'group': 'core', 'member_kind': 'principal',
                                                           'member': 'erin'})
        self.assertEqual(left['newly_unreadable'], ['maple'], 'removing someone from another workspace')
        removed = self.store.remove_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal',
                                                       'subject': 'erin'})
        self.assertEqual(removed['newly_unreadable'], [], 'narrowing birch only widens what its sessions read')
        # A synced group change has no admin to answer: it is logged.
        self.store.add_group_member(self.alice, {'group': 'core', 'member_kind': 'principal', 'member': 'erin'})
        self.store.set_entry(self.alice, {'workspace': 'birch', 'subject_kind': 'principal', 'subject': 'erin',
                                          'role': 'reader'})
        with self.assertLogs('agenthub.workspace_access', 'WARNING') as logged:
            with self.store.open() as state, state.db:
                before = self.store.sync_containment(state.db, 'acme', ['core'])
                state.db.execute("DELETE FROM group_edges WHERE tenant='acme' AND group_id='core'")
                self.store._refresh_groups(state.db, 'acme', ['core'])
                self.store.log_newly_unreadable(state.db, 'acme', before,
                                                self.store.sync_containment(state.db, 'acme', ['core']), 'test')
        self.assertIn('"newly_unreadable": ["maple"]', logged.output[0], 'erin left maple')

    def test_labelled_search_and_mcp_carry_the_filter(self):
        from agenthub.mcp_tools import MemoryTools
        from types import SimpleNamespace
        query = {'version': 'enterprise-local-1', 'query': self.fixture['query'], 'project': 'maple'}
        unlabelled = self.store.search(self.alice, query)
        labelled = self.store.search(self.label(self.alice, 'maple'), query)
        ids = lambda result: {card['id'] for card in result['results']}
        self.assertTrue(ids(unlabelled))
        allowed = self.layers(self.label(self.alice, 'maple'))[0]
        documents = self.documents()
        self.assertTrue({documents[i] for i in ids(labelled) if i in documents} <= allowed)
        # The API path computes the same filter lazily inside the request's transaction.
        holder = {}
        lazy = self.store.search(dict(self.alice, session_label_request={'workspace': 'maple'},
                                      session_label_cache=holder), query)
        self.assertEqual(ids(lazy), ids(labelled))
        self.assertEqual(holder['filter']['workspaces'], ['maple'], 'computed once and shared')
        oracle, indexed, rls = self.layers(dict(self.alice, session_label_request={'workspace': 'maple'}))
        self.assertEqual((oracle, indexed, rls), (self.expect('m_shared', 'm_team'),) * 3)
        sent = []
        tools = MemoryTools('maple', SimpleNamespace(request=lambda path, value=None: sent.append((path, value)) or
                                                     {'results': []}), label={'workspace': 'maple'})
        tools.call('search_memory', {'query': 'synthetic'})
        self.assertEqual(sent[-1][1]['session_label'], {'workspace': 'maple'})
        self.assertNotIn('scope', sent[-1][1])
        with self.assertRaises(ValueError):
            tools.call('search_memory', {'query': 'synthetic', 'scope': 'all'})
