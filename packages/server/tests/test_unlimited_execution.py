import tempfile
import unittest
import os
from types import SimpleNamespace
from agenthub.retrieval_embedding_cache import CampaignEmbedder
from agenthub.backend_worker import Worker
from test_cloud_postgres import PostgresFixture


class Model:
    model_key = 'unlimited-fixture:512'
    dimension = 512
    def embed_queries(self, texts): return [[1.] + [0.] * 511 for _ in texts]
    embed_documents = embed_queries


class UnlimitedExecutionTests(unittest.TestCase):
    def test_remove_embedding_limit_retains_cache_and_all_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CampaignEmbedder(Model(), tmp, max_embedding_seconds=1)
            cache.embed_documents(['synthetic original'])
            with cache._locked() as db:
                db.execute('UPDATE campaign SET used_seconds=4000 WHERE singleton=1')
                db.commit()
            cache.remove_limit()
            reopened = CampaignEmbedder(Model(), tmp, max_embedding_seconds=3600)
            reopened.embed_queries(['synthetic new query'])
            result = reopened.stats()
            self.assertEqual(result['cache_entries'], 1)
            self.assertEqual(result['model_calls'], 2)
            self.assertGreaterEqual(result['embedding_seconds'], 4000)
            self.assertIsNone(result['max_embedding_seconds'])
            self.assertIsNone(result['remaining_seconds'])

    def test_operator_selected_run_size_is_not_an_authorization_ceiling(self):
        worker = Worker.__new__(Worker)
        worker.dry_run = True
        worker.store = SimpleNamespace(curation_enabled=True)
        worker.status = lambda: {'synthetic': True}
        result = worker.run(max_jobs=5000, max_calls=5000, max_retries=1001,
                            max_seconds=7200, max_input_tokens=4_000_000, refresh_queue=False)
        self.assertEqual(result['calls'], 0)


@unittest.skipUnless(os.environ.get('AGENTNETWORK_PG_SERVICES'), 'private PostgreSQL fixture required')
class UnlimitedAdmissionTests(PostgresFixture, unittest.TestCase):
    def setUp(self): self.postgres_setup(bootstrap=False)
    def tearDown(self): self.postgres_teardown()

    def test_cumulative_calls_do_not_block_but_current_concurrency_does(self):
        from agenthub.cloud_ops import Meter, AdmissionExhausted
        meter = Meter(SimpleNamespace(tenant_id='synthetic-unlimited', open=self.store.open))
        meter.configure(max_parallel=1, max_attempts=1)
        meter.reserve('first', 'synthetic-job', 'observer')
        meter.finish('first', 'failed', usage={'input_tokens': 37})
        meter.reserve('second', 'synthetic-job', 'observer')
        self.assertEqual(meter.status()['attempts'], 2)
        self.assertEqual(meter.status()['reported_tokens']['input_tokens'], 37)
        with self.assertRaises(AdmissionExhausted):
            meter.reserve('concurrent', 'synthetic-job', 'observer')
