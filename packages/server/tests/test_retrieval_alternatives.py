"""Frozen causal regressions, written before bounded retrieval alternatives.

These synthetic PostgreSQL cases are development fixtures, never confirmation.
"""
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit local PostgreSQL required')
class Alternatives(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')

    def tearDown(self):
        self.postgres_teardown()

    def note(self, external, title, body):
        source = self.store.ingest(self.ctx, {'version': VERSION, 'external_id': external,
            'session': 'synthetic', 'turn': external, 'project': 'maple', 'kind': 'Stop',
            'body': body, 'visibility': 'private', 'occurred_at': 100})['source_id']
        return self.store.accept_reviewed_note(self.ctx, source, title, body)['document_id']

    def test_amount_is_not_mandatory_identifier_but_typed_record_remains_one(self):
        evidence = self.note('amount', 'Allowance arithmetic',
                             'The approved allowance is 300000 USD for the regional project.')
        self.store.retrieval_numeric_mode = 'typed'
        candidates = self.store.candidates(self.ctx, 'Does the approved allowance exceed 250000 USD?', vector=False)
        self.assertIn(evidence, {r['document_id'] for r in candidates})
        record = self.note('record', 'Volume record 000123',
                           'Volume Record 000123 keeps the dataset at /synthetic/000123.csv.')
        result = self.store.candidates(self.ctx, 'Where is Volume Record 000123 saved?', vector=False)
        self.assertEqual([r['document_id'] for r in result], [record])
        self.assertEqual(self.store.candidates(self.ctx, 'Where is Volume Record 000999 saved?', vector=False), [])

    def test_larger_candidate_budget_remains_delivery_bounded_and_scope_safe(self):
        for index in range(27):
            self.note(str(index), 'Atlas operation '+str(index),
                      'The Atlas operation requires a checksum check and approval before recovery. Step '+str(index)+'.')
        self.store.retrieval_candidate_limit = 50
        result = self.store.search(self.ctx, {'version': VERSION, 'query': 'Atlas operation', 'mode': 'explicit'})
        self.assertLessEqual(len(result['results']), 8)
        import json
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True)), 4000)
        bob = self.store.authenticate(self.tokens['bob'])
        self.assertEqual(self.store.search(bob, {'version': VERSION, 'query': 'Atlas operation'})['results'], [])

    def test_heading_only_passage_is_not_a_fact_when_prose_guard_enabled(self):
        self.note('heading', 'Atlas checklist', '# Atlas checklist with extended provenance heading\n\n')
        self.store.retrieval_require_prose = True
        result = self.store.search(self.ctx, {'version': VERSION, 'query': 'Atlas checklist'})
        self.assertFalse(result['answerable'])
        self.assertEqual(result['results'], [])


if __name__ == '__main__':
    unittest.main()
