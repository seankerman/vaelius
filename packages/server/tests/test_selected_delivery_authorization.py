"""Candidate ranking and current delivery checks have separate responsibilities."""
import unittest
from unittest.mock import patch
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit isolated PostgreSQL fixture required')
class SelectedDeliveryAuthorization(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_authorization_shape = 'authorized_cte'
        self.ctx = self.store.authenticate(self.tokens['alice'])
        self.sources = []
        # Twelve owner-private captured chat messages, each one passage.
        for i in range(12):
            body = ('Project Copper protects files during a datacenter outage by keeping copies '
                    f'in three separate zones. Its replication interval is {i+1} minutes.')
            self.sources.append(self.capture(body, key='copper-'+str(i), conversation='copper',
                                             sharing='private', index=False))
        self.index_captures()

    def tearDown(self):
        self.postgres_teardown()

    def request(self):
        return {'version': VERSION, 'query': 'How does Project Copper protect files during a datacenter outage?',
                'project': 'maple', 'mode': 'explicit', 'limit': 3}

    def test_unselected_candidates_do_not_repeat_delivery_authorization_roundtrips(self):
        count = len(self.store.candidates(self.ctx, self.request()['query'], project='maple', limit=20))
        self.assertGreaterEqual(count, 10)
        with patch.object(self.store, '_document_allowed', wraps=self.store._document_allowed) as checks:
            result = self.store.search(self.ctx, self.request())
        # Passages are discovery: delivered, never claimed as a verified answer.
        self.assertFalse(result['answerable'])
        self.assertTrue(result['results'])
        self.assertLess(checks.call_count, count)

    def test_withdrawal_after_ranking_still_blocks_every_selected_card(self):
        ranked = self.store.candidates
        def rank_then_withdraw(*args, **kwargs):
            rows = ranked(*args, **kwargs)
            self.assertGreaterEqual(len(rows), 10)
            for source in self.sources:
                self.store.lifecycle(self.ctx, {'version': VERSION, 'target_id': source,
                    'expected_revision': '1', 'idempotency_key': 'withdraw-'+source,
                    'reason': 'Synthetic withdrawal after ranking', 'operation': 'withdraw'})
            return rows
        with patch.object(self.store, 'candidates', side_effect=rank_then_withdraw):
            result = self.store.search(self.ctx, self.request())
        self.assertFalse(result['answerable'])
        self.assertFalse(result['results'])
        self.assertIn('authorization_changed_before_delivery', result['coverage_gaps'])


if __name__ == '__main__':
    unittest.main()
