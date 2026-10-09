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
        self.store.retrieval_selection_policy = 'facets_v2'
        self.ctx = self.store.authenticate(self.tokens['alice'])
        self.sources = []
        for i in range(12):
            body = ('Project Copper protects files during a datacenter outage by keeping copies '
                    f'in three separate zones. Its replication interval is {i+1} minutes.')
            source = self.store.ingest(self.ctx, {'version': VERSION, 'external_id': 'copper-'+str(i),
                'session': 'copper', 'turn': str(i), 'project': 'maple', 'kind': 'Stop',
                'body': body, 'occurred_at': 12345+i, 'visibility': 'private'})['source_id']
            self.sources.append(source)
            self.store.accept_reviewed_note(self.ctx, source, 'Project Copper replica '+str(i), body)

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
        self.assertTrue(result['answerable'])
        self.assertTrue(result['results'])
        self.assertLess(checks.call_count, count)

    def test_withdrawal_after_ranking_still_blocks_every_selected_card(self):
        from agenthub.cloud_retrieval import select_supported_cards
        def withdraw_then_select(*args, **kwargs):
            for source in self.sources:
                self.store.lifecycle(self.ctx, {'version': VERSION, 'target_id': source,
                    'expected_revision': '1', 'idempotency_key': 'withdraw-'+source,
                    'reason': 'Synthetic withdrawal after ranking', 'operation': 'withdraw'})
            return select_supported_cards(*args, **kwargs)
        with patch('agenthub.cloud_retrieval.select_supported_cards', side_effect=withdraw_then_select):
            result = self.store.search(self.ctx, self.request())
        self.assertFalse(result['answerable'])
        self.assertFalse(result['results'])


if __name__ == '__main__':
    unittest.main()
