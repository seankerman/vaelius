"""Team-visible redacted passages: scope-file requests capped by operator project policy.

Synthetic PostgreSQL cases. A client's `sharing: team` request is only honored
when the project's ceiling allows it at ingest AND still allows it at query time,
for active project members, through every existing source rule. Originals stay
owner/grant-only. No model calls.

These cases model a tenant that kept the private default (every tenant created
before migration 045) whose authors raised their own setting to team; the
organization level and the person's own setting are covered in
test_sharing_levels.py.
"""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Denied, EnterpriseStore
from agenthub.postgres import PostgresConnection
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class ProjectSharingTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_corpus = 'sources'
        self.store.create_principal('acme', 'carol')
        self.store.create_project('acme', 'birch')
        self.store.set_membership('acme', 'birch', 'carol', True)
        self.tokens['carol'] = self.store.enroll('acme', 'carol', 'carol',
                                                 ['ingest', 'read', 'source_read', 'correct', 'withdraw', 'policy'])
        self.bob = self.store.authenticate(self.tokens['bob'])
        self.carol = self.store.authenticate(self.tokens['carol'])
        self.store.enroll_connection(self.ctx, 'share-test', 'test', 'maple', ['agent'], visibility='team')
        self.store.set_tenant_sharing_default('private')
        for ctx in (self.ctx, self.bob):
            self.store.sharing_setting(ctx, {'max_sharing': 'team'})
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/raw_default_v1.json').read_text())

    def tearDown(self):
        self.postgres_teardown()

    def ingest(self, index=0, *, sharing='team', connection='share-test', ctx=None, **changes):
        row = dict(self.fixture['messages'][index], **changes)
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit' if row['role'] == 'user' else 'Stop',
            'event_id': row['id'] + ':' + connection, 'session_id': self.fixture['conversation'], 'turn_id': row['id'],
            'timestamp': row['time'], 'prompt': row['text'], 'last_assistant_message': row['text'],
            'revision': row.get('revision', '1')}, 'maple', connection, sharing=sharing)
        return self.store.ingest_general(ctx or self.ctx, event)['source_id']

    def drain(self):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('no LLM')):
            return SourceIndex(self.store).run(max_sources=20, max_seconds=20)

    def search(self, ctx=None, **kwargs):
        return self.store.search(ctx or self.ctx, {'version': 'enterprise-local-1',
            'query': self.fixture['query'], 'project': 'maple', **kwargs})

    def stored(self, source):
        with self.store.open() as state:
            row = state.db.execute('SELECT sharing,sharing_requested FROM enterprise_sources WHERE id=?',
                                   (source,)).fetchone()
        return row['sharing'], row['sharing_requested']

    def lexical_hits(self, ctx):
        """The restricted search-reader role path (row-level security function)."""
        from agenthub.search_reader import restrict
        with self.store.open() as state, restrict(self.store, state, ctx, 'maple'):
            return state.db.execute('SELECT * FROM search_lexical_matches(?)', ('Orchard',)).fetchall()

    def evidence(self, ctx, card):
        from agenthub.source_evidence import document_evidence
        return document_evidence(self.store, ctx, card['id'], revision=card['revision'])

    def context(self, ctx, card):
        from agenthub.source_context import document_context
        return document_context(self.store, ctx, card['id'], revision=card['revision'])

    # Visibility ---------------------------------------------------------------

    def test_member_reads_team_passage_evidence_and_context_but_not_the_original(self):
        self.store.set_project_sharing('maple', 'team')
        source = self.ingest();self.drain()
        self.assertEqual(self.stored(source), ('team', 'team'))
        mine = self.search()['results']
        self.assertTrue(mine)
        self.assertEqual(self.search(self.bob)['results'], mine)
        card = mine[0]
        self.assertIn('/data/drafts/orchard.csv', self.evidence(self.bob, card)['sources'][0]['text'])
        self.assertTrue(self.context(self.bob, card)['sources'])
        self.assertEqual(len(self.lexical_hits(self.bob)), 1)
        # `read` alone is enough for a member's passage tier.
        reader = self.store.authenticate(self.store.enroll('acme', 'bob', 'bob-read', ['read']))
        self.assertEqual(self.search(reader)['results'], mine)
        # Retained originals keep their rule: source_read + owner or explicit grant.
        with self.assertRaises(Denied):
            self.store.general_source(self.bob, source)
        self.assertEqual(self.store.general_source(self.ctx, source)['source_id'], source)
        readers = self.store.explain(self.store.authenticate(self.tokens['admin']), source)['raw_readers']
        self.assertEqual(sorted(readers), ['alice', 'bob'])

    def test_non_member_never_reads_team_passages(self):
        self.store.set_project_sharing('maple', 'team')
        self.ingest();self.drain()
        card = self.search()['results'][0]
        self.assertEqual(self.search(self.carol)['results'], [])
        self.assertEqual(self.lexical_hits(self.carol), [])
        for read in (self.evidence, self.context):
            with self.assertRaises((Denied, PermissionError)):
                read(self.carol, card)
        # An organization-visible connection does not widen passages beyond members.
        self.store.enroll_connection(self.ctx, 'org-test', 'org', 'maple', ['agent'], visibility='organization')
        self.ingest(1, connection='org-test');self.drain()
        self.assertEqual(self.search(self.carol)['results'], [])
        self.assertEqual(len(self.search(self.bob)['results']), 2)

    def test_private_request_stays_owner_only(self):
        self.store.set_project_sharing('maple', 'team')
        source = self.ingest(sharing='private');self.drain()
        self.assertEqual(self.stored(source), ('private', 'private'))
        self.assertTrue(self.search()['results'])
        self.assertEqual(self.search(self.bob)['results'], [])
        self.assertEqual(self.lexical_hits(self.bob), [])

    def test_scope_file_request_cannot_exceed_default_private_policy(self):
        # Anyone can edit a repository's scope file; the ceiling is the operator's.
        source = self.ingest();self.drain()
        self.assertEqual(self.stored(source), ('private', 'team'))
        self.assertEqual(self.search(self.bob)['results'], [])
        with self.store.open() as state:
            capped = state.db.execute('''SELECT count(*) FROM enterprise_audit
                WHERE operation='sharing_request' AND disposition='capped_private' AND target_id=?''',
                (source,)).fetchone()[0]
        self.assertEqual(capped, 1)
        # Raising the ceiling later never exposes what was captured as private.
        self.store.set_project_sharing('maple', 'team')
        self.assertEqual(self.search(self.bob)['results'], [])
        self.assertEqual(self.lexical_hits(self.bob), [])
        self.assertEqual(self.stored(source), ('private', 'team'))

    def test_private_connection_never_shares(self):
        self.store.set_project_sharing('maple', 'team')
        self.store.enroll_connection(self.ctx, 'private-test', 'private', 'maple', ['agent'])
        source = self.ingest(connection='private-test');self.drain()
        self.assertEqual(self.stored(source), ('private', 'team'))
        self.assertEqual(self.search(self.bob)['results'], [])

    def test_lowering_policy_hides_immediately_including_delivered_results(self):
        self.store.set_project_sharing('maple', 'team')
        self.ingest();self.drain()
        delivered = self.search(self.bob)
        card = delivered['results'][0]
        self.store.set_project_sharing('maple', 'private')
        self.assertEqual(self.search(self.bob)['results'], [])
        self.assertEqual(self.lexical_hits(self.bob), [])
        self.assertEqual(self.store.validate_search_delivery(self.bob, {}, delivered)['results'], [])
        with self.assertRaises((Denied, PermissionError)):
            self.evidence(self.bob, card)
        with self.assertRaises((Denied, PermissionError)):
            self.context(self.bob, card)
        self.assertTrue(self.search()['results'])  # the owner keeps their own passages
        # Inheriting a team tenant default restores sources stored as team.
        self.store.set_project_sharing('maple', None)
        self.assertEqual(self.search(self.bob)['results'], [])
        self.store.set_tenant_sharing_default('team')
        self.assertEqual(self.search(self.bob)['results'], delivered['results'])
        status = self.store.sharing_policy_status()
        self.assertEqual(status['default_max_sharing'], 'team')
        maple = [p for p in status['projects'] if p['project'] == 'maple'][0]
        self.assertEqual((maple['max_sharing'], maple['effective_ceiling']), (None, 'team'))
        self.assertEqual(maple['active_sources']['team'], 1)

    def test_membership_revocation_and_delegation_limits(self):
        self.store.set_project_sharing('maple', 'team')
        self.ingest();self.drain()
        self.assertTrue(self.search(self.bob)['results'])
        # A delegated credential stays inside its delegated projects.
        self.store.create_principal('acme', 'helper')
        for projects, visible in ((['birch'], False), (['maple'], True)):
            self.store.set_delegation('acme', 'helper', 'bob', ['read'], projects, True)
            token = self.store.enroll('acme', 'helper', 'helper-' + projects[0], ['read'], acting_for='bob')
            delegated = self.store.authenticate(token)
            self.assertEqual(bool(self.store.search(delegated, {'version': 'enterprise-local-1',
                'query': self.fixture['query']})['results']), visible, projects)
        self.store.set_membership('acme', 'maple', 'bob', False)
        self.assertEqual(self.search(self.bob)['results'], [])
        self.assertEqual(self.lexical_hits(self.bob), [])

    # Existing protections -----------------------------------------------------

    def test_quarantine_withdrawal_and_legal_hold_behave_as_before(self):
        self.store.set_project_sharing('maple', 'team')
        source = self.ingest();self.drain()
        self.assertTrue(self.search(self.bob)['results'])
        self.store.quarantine_principal('alice', reason='poisoning review', since=0)
        self.assertEqual(self.search(self.bob)['results'], [])
        self.assertEqual(self.lexical_hits(self.bob), [])
        self.assertTrue(self.search()['results'])
        self.store.release_principal('alice', reason='review cleared')
        self.assertTrue(self.search(self.bob)['results'])
        # Owner deletion under a legal hold hides like a withdrawal and keeps the source.
        holder = self.store.authenticate(self.store.enroll('acme', 'admin', 'admin-hold', ['legal_hold', 'audit']))
        self.store.create_legal_hold(holder, 'principal', 'alice', 'synthetic matter')
        self.store.lifecycle(self.ctx, {'version': 'enterprise-local-1', 'target_id': source,
            'operation': 'delete', 'expected_revision': '1', 'idempotency_key': 'held-delete', 'reason': 'fixture'})
        self.assertEqual(self.search(self.bob)['results'], [])
        self.assertEqual(self.search()['results'], [])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM memories WHERE id=?', (source,)).fetchone()[0], 1)
        # Withdrawal of a second team-shared source.
        second = self.ingest(1);self.drain()
        self.assertTrue(self.search(self.bob)['results'])
        self.store.lifecycle(self.ctx, {'version': 'enterprise-local-1', 'target_id': second,
            'operation': 'withdraw', 'expected_revision': '1', 'idempotency_key': 'withdraw', 'reason': 'fixture'})
        self.assertEqual(self.search(self.bob)['results'], [])

    def test_connection_reader_list_still_restricts_team_passages(self):
        self.store.set_project_sharing('maple', 'team')
        self.ingest();self.drain()
        self.assertTrue(self.search(self.bob)['results'])
        self.store.connection_policy(self.ctx, 'share-test', reader_ids=['alice'])
        self.drain()
        self.assertEqual(self.search(self.bob)['results'], [])

    def test_tenant_isolation(self):
        self.store.set_project_sharing('maple', 'team')
        source = self.ingest();self.drain()
        with self.store.open() as state:
            row = state.db.execute('SELECT * FROM enterprise_sources WHERE id=?', (source,)).fetchone()
            forged = dict(self.bob, tenant='other')
            self.assertFalse(self.store._visible_source(state.db, forged, row, raw=True))
            self.assertTrue(self.store._visible_source(state.db, self.bob, row, raw=True))

    # Parity and cost ----------------------------------------------------------

    def test_indexed_predicate_row_security_and_python_oracle_agree(self):
        self.store.enroll_connection(self.bob, 'bob-share', 'bob', 'maple', ['agent'], visibility='team')
        self.store.set_project_sharing('maple', 'team')
        self.ingest(0);self.ingest(1, sharing='private');self.ingest(2, connection='bob-share', ctx=self.bob)
        self.ingest(0, connection='bob-share', ctx=self.bob, sharing='private')
        self.drain()
        reader = self.store.authenticate(self.store.enroll('acme', 'carol', 'carol-read', ['read']))
        contexts = (self.ctx, self.bob, self.carol, reader)

        def compare(label):
            with self.store.open() as state:
                docs = [r[0] for r in state.db.execute(
                    "SELECT id FROM enterprise_documents WHERE representation='raw'")]
                self.assertEqual(len(docs), 4)
                for ctx in contexts:
                    # The per-document Python oracle; CloudStore's own _document_allowed
                    # reuses the SQL predicate, so call the canonical implementation.
                    oracle = {d for d in docs if EnterpriseStore._document_allowed(self.store, state.db, ctx, d)}
                    indexed = set(self.store._authorized_documents(state, ctx)) & set(docs)
                    self.assertEqual(indexed, oracle, (label, ctx['actor']))
                    # The row-level security function (migration 045) used by ranking.
                    from agenthub.search_reader import restrict
                    with self.store.open() as reader_state, restrict(self.store, reader_state, ctx, reach='all'):
                        secured = {r[0] for r in reader_state.db.execute(
                            "SELECT id FROM enterprise_documents WHERE representation='raw'")}
                    self.assertEqual(secured, oracle, (label, ctx['actor'], 'row_level_security'))
                    yield ctx['actor'], len(oracle)
        self.assertEqual(dict(compare('team')), {'alice': 3, 'bob': 3, 'carol': 0})
        self.store.set_project_sharing('maple', 'private')
        self.assertEqual(dict(compare('private')), {'alice': 2, 'bob': 2, 'carol': 0})

    def test_search_statement_count_does_not_grow_with_team_sharing(self):
        def measure(ctx):
            sql = [];execute = PostgresConnection.execute
            def counted(db, query, params=None):
                sql.append(query);return execute(db, query, params)
            with patch.object(PostgresConnection, 'execute', counted):
                started = time.perf_counter();result = self.search(ctx)
                elapsed = time.perf_counter() - started
            return len(sql), elapsed, len(result['results'])
        self.store.set_project_sharing('maple', 'team')
        self.ingest(0);self.ingest(1, sharing='private');self.drain()
        owner = measure(self.ctx);member = measure(self.bob)
        self.assertEqual((owner[2], member[2]), (2, 1))
        # A teammate's search runs exactly the statements the owner's does.
        self.assertEqual(member[0], owner[0])
        self.assertLess(member[1], 5)
