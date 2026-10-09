"""Frozen full-set policy equivalence and current-delivery regressions.

These are synthetic development fixtures, not semantic quality or throughput.
All PostgreSQL mutation uses an explicitly configured disposable UUID schema.
"""
import hashlib
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from agenthub.document_ingest import DocumentStore
from agenthub.enterprise import Denied
from agenthub.source_objects import FileSourceObjects
from test_cloud_postgres import PostgresFixture, SERVICES

FIXTURE = Path(__file__).resolve().parent.parent / 'tools/fixtures/local_staging_readiness_v1/retrieval_lane.json'


class FrozenFixtureTests(unittest.TestCase):
    def test_typed_required_facts_are_grounded_in_synthetic_sources(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(fixture['version'], 'staging-retrieval-lane-v1')
        self.assertGreaterEqual(len(fixture['authorization_contract']['cases']), 12)
        for case in fixture['selection_cases']:
            for question in case['questions']:
                self.assertEqual(bool(question['facets']), question['answerable'])
                for facet in question['facets']:
                    self.assertIn(facet['value'], case['body'])
                    self.assertIn(facet['kind'], {'rationale', 'choice', 'procedure', 'quantity', 'reported'})
        for facet in fixture['compound']['required_facets']:
            self.assertIn(facet['value'], fixture['compound']['sources'][facet['source']]['body'])
        self.assertNotEqual(fixture['originals']['versions'][0]['bytes'], fixture['originals']['versions'][1]['bytes'])
        self.assertNotEqual(fixture['history']['versions'][0]['effective_at'], fixture['history']['versions'][0]['captured_at'])


@unittest.skipUnless(SERVICES, 'explicit isolated PostgreSQL services required')
class CompletePolicyEquivalence(PostgresFixture, unittest.TestCase):
    def setUp(self):
        from agenthub.pipeline_pin import verify
        # Verify the real installed/source identity before creating any schema.
        # A dirty source checkout needs a deliberately separate source harness;
        # this fixture never bypasses canonical verification itself.
        self.canonical_pin = verify()
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.docs = set()
        self.contexts = {who: self.store.authenticate(token) for who, token in self.tokens.items()}

    def tearDown(self):
        self.postgres_teardown()

    def note(self, key, *, owner='alice', visibility='private', project='maple'):
        ctx = self.contexts[owner]
        body = 'The ' + key + ' gauge reference is /synthetic/' + key + '.csv.'
        source = self.store.ingest(ctx, {'version': VERSION, 'external_id': key, 'session': key,
            'turn': '1', 'project': project, 'kind': 'Stop', 'body': body,
            'visibility': visibility, 'occurred_at': 100})['source_id']
        result = self.store.accept_reviewed_note(ctx, source, key + ' gauge reference', body)
        self.docs.add(result['document_id'])
        return source, result['document_id']

    def compare_full(self, *, expected=None, contexts=None):
        """Compare the complete ID/revision set, never merely ranked top-k."""
        for who, ctx in (contexts or self.contexts).items():
            with self.subTest(principal=who), self.store.open() as state:
                rows = state.db.execute('SELECT id FROM enterprise_documents WHERE tenant=?', ('acme',)).fetchall()
                oracle = {}
                for row in rows:
                    # Independent source-level oracle, including preference rules.
                    from agenthub.cloud_preferences import CloudPreferencesMixin
                    allowed = CloudPreferencesMixin._document_allowed(self.store,state.db,ctx,row[0])
                    if allowed:
                        oracle[row[0]] = allowed['active_revision_id']
                if expected and who in expected:
                    self.assertEqual(set(oracle), expected[who])
                for shape in ('compiled',):
                    self.store.retrieval_authorization_shape = shape
                    ids = self.store._authorized_documents(state, ctx)
                    actual = {ident: state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?', (ident,)).fetchone()[0] for ident in ids}
                    self.assertEqual(actual, oracle, (who, shape))

    def test_all_ids_private_team_organization_and_missing_dependency(self):
        _, alice = self.note('owner-a')
        _, bob = self.note('owner-b', owner='bob')
        _, team = self.note('member', visibility='team')
        _, organization = self.note('organization', visibility='organization')
        _, missing = self.note('missing-links', visibility='team')
        with self.store.open() as state, state.db:
            state.db.execute('DELETE FROM enterprise_dependencies WHERE document_id=?', (missing,))
        self.compare_full(expected={'alice': {alice, team, organization}, 'bob': {bob, team, organization}, 'admin': {organization}})
        self.store.set_membership('acme', 'maple', 'bob', False)
        self.compare_full(expected={'alice': {alice, team, organization}, 'bob': {bob, organization}, 'admin': {organization}})

    def test_all_dependencies_required_and_current_acl_fails_closed(self):
        source, mixed = self.note('mixed-support', visibility='team')
        private, private_doc = self.note('owner-evidence')
        with self.store.open() as state, state.db:
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)', (mixed, private))
        self.compare_full(expected={'alice': {mixed, private_doc}, 'bob': set(), 'admin': set()})
        self.store.set_source_acl_freshness(self.ctx, private, state='unknown')
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})
        self.store.set_source_acl_freshness(self.ctx, private, valid_seconds=0)
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})

    def test_connection_readers_freshness_and_inactive_policy(self):
        self.store.enroll_connection(self.ctx, 'native-policy', 'fixture', 'maple', ['document'], visibility='team', reader_ids=['alice', 'bob'], freshness_seconds=60)
        service = DocumentStore(self.store, FileSourceObjects(Path(self.temp.name) / 'objects'))
        original = service.ingest(self.ctx, 'native-policy', 'gauge', '1', 'gauge.md', io.BytesIO(b'# Gauge reference\nThe gauge uses a forty millimetre tube.\n'))
        source = original['source_id']
        with self.store.open() as state:
            docs = {r[0] for r in state.db.execute('SELECT document_id FROM backend_native_artifacts WHERE source_id=?', (source,))}
        self.assertTrue(docs)
        self.compare_full(expected={'alice': docs, 'bob': docs, 'admin': set()})
        # Explicit synthetic clock fault: current reader permission expires.
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE backend_connections SET permission_observed=? WHERE id=?', (time.time()-61, 'native-policy'))
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})
        self.store.connection_policy(self.ctx, 'native-policy', reader_ids=['alice'], active=False)
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})

    def test_empty_reader_set_and_pending_or_inactive_generation(self):
        self.store.enroll_connection(self.ctx, 'native-empty-readers', 'fixture', 'maple', ['document'], visibility='team', reader_ids=[])
        service = DocumentStore(self.store, FileSourceObjects(Path(self.temp.name) / 'objects'))
        original = service.ingest(self.ctx, 'native-empty-readers', 'empty-readers', '1', 'empty.md', io.BytesIO(b'# Empty reader permission\nThe gauge uses a thirty millimetre tube.\n'))
        with self.store.open() as state:
            docs = {r[0] for r in state.db.execute('SELECT document_id FROM backend_native_artifacts WHERE source_id=?', (original['source_id'],))}
        self.compare_full(expected={'alice': docs, 'bob': docs, 'admin': set()})
        _, held = self.note('held-preference', visibility='team')
        with self.store.open() as state, state.db:
            dependency = state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?', (held,)).fetchone()[0]
            state.db.execute('INSERT INTO cloud_preference_candidates VALUES(?,?,?,?,?,?)', (held, dependency, 'alice', '{}', 'held', time.time()))
            state.db.execute('INSERT INTO knowledge_generations VALUES(?,?,?,?,?,?,?,?)', ('held-generation', 'fixture', 'retired', '{}', 'fixture', time.time(), None, time.time()))
            for ident in docs:
                state.db.execute('INSERT INTO knowledge_generation_documents VALUES(?,?,?)', ('held-generation', ident, time.time()))
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})

    def test_current_private_preference_and_candidate_hold(self):
        text = 'Please remember: I prefer pytest for my projects.'
        source = self.store.ingest(self.ctx, {'version': VERSION, 'external_id': 'private-preference', 'session': 'preference', 'turn': '1', 'project': 'maple', 'kind': 'UserPromptSubmit', 'body': text, 'visibility': 'private', 'occurred_at': 100})['source_id']
        doc = self.store.curate_preference(self.ctx, source, {'key': 'test_runner', 'value': 'pytest', 'scope': 'user', 'quote': text})['document_id']
        self.compare_full(expected={'alice': {doc}, 'bob': set(), 'admin': set()})
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE cloud_preferences SET valid_until=? WHERE document_id=?', (time.time(), doc))
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})

    def test_reviewed_release_exact_policy_binding_and_delegation(self):
        source, private = self.note('reviewed-private')
        with self.store.open() as state:
            revision = state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?', (private,)).fetchone()[0]
        release = self.store.reviewed_release(self.ctx, private, revision, 'maple', ['bob'], 'release-once')['document_id']
        self.compare_full(expected={'alice': {private, release}, 'bob': {release}, 'admin': set()})
        self.store.set_delegation('acme', 'bob', 'alice', ['read'], ['maple'], True)
        delegated = self.store.authenticate(self.store.enroll('acme', 'bob', 'delegate', ['read'], acting_for='alice'))
        self.compare_full(contexts={'represented-alice': delegated}, expected={'represented-alice': {private, release}})
        self.store.set_source_acl_freshness(self.ctx, source, state='unknown')
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})

    def test_correction_withdrawal_and_revocation_at_delivery(self):
        source, document = self.note('mutable-gauge', visibility='team')
        bob = self.contexts['bob']
        self.compare_full(expected={'alice': {document}, 'bob': {document}, 'admin': set()})
        original_candidates = self.store.candidates
        def revoke_after_candidates(*args, **kwargs):
            rows = original_candidates(*args, **kwargs)
            self.store.set_membership('acme', 'maple', 'bob', False)
            return rows
        with patch.object(self.store, 'candidates', side_effect=revoke_after_candidates):
            result = self.store.search(bob, {'version': VERSION, 'query': 'Where is the mutable-gauge reference?', 'project': 'maple'})
        self.assertFalse(result['answerable'])
        self.assertEqual(result['results'], [])
        self.store.lifecycle(self.ctx, {'version': VERSION, 'target_id': source, 'expected_revision': '1', 'operation': 'withdraw', 'idempotency_key': 'withdraw-mutable', 'reason': 'synthetic fixture'})
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})

    def test_correction_changes_the_compared_active_revision_set(self):
        source, document = self.note('corrected-gauge', visibility='team')
        with self.store.open() as state:
            old = state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?', (document,)).fetchone()[0]
        replacement = {'version': VERSION, 'external_id': 'gauge-correction', 'session': 'corrected-gauge', 'turn': '2', 'project': 'maple', 'kind': 'Stop', 'body': 'The corrected gauge reference is /synthetic/updated-gauge.csv.', 'visibility': 'team', 'occurred_at': 200}
        self.store.lifecycle(self.ctx, {'version': VERSION, 'target_id': source, 'expected_revision': '1', 'operation': 'correct', 'idempotency_key': 'correct-gauge', 'reason': 'synthetic fixture', 'replacement': {'source': replacement, 'title': 'corrected-gauge gauge reference', 'lesson': replacement['body']}})
        with self.store.open() as state:
            current = state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?', (document,)).fetchone()[0]
        self.assertNotEqual(current, old)
        self.compare_full(expected={'alice': {document}, 'bob': {document}, 'admin': set()})


if __name__ == '__main__':
    unittest.main()
