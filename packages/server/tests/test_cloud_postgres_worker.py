"""Existing canonical worker scenarios plus independent PostgreSQL processes."""
import multiprocessing
import unittest
from unittest.mock import patch

import test_backend_worker as canonical_worker_tests
from test_cloud_postgres import PostgresFixture, SERVICES
from agenthub.postgres import PostgresEnterpriseStore


def _claim(home, dsn, config, output):
    from agenthub.backend_worker import Worker
    try:
        store = PostgresEnterpriseStore(home, dsn, "orchard")
        store.curation_enabled = True  # This process exercises explicitly enabled enrichment.
        worker = Worker(store, config, live=False)
        claim = worker.claim(lease_seconds=30)
        output.put({"job": claim["id"] if claim else None, "fence": claim["fence"] if claim else None})
    except Exception as exc:
        output.put({"error": type(exc).__name__})


@unittest.skipUnless(SERVICES, "explicit real PostgreSQL services manifest required")
class PostgresWorkerTests(PostgresFixture, canonical_worker_tests.WorkerTests):
    def setUp(self):
        self.postgres_setup(tenant_id="orchard", bootstrap=False)
        def enriched_store(home):
            store = PostgresEnterpriseStore(home, self.dsn, "orchard")
            store.curation_enabled = True
            return store
        self.factory = patch.object(canonical_worker_tests, "EnterpriseStore", side_effect=enriched_store)
        self.factory.start()
        canonical_worker_tests.WorkerTests.setUp(self)

    def tearDown(self):
        canonical_worker_tests.WorkerTests.tearDown(self)
        self.factory.stop()
        self.postgres_teardown()

    def test_four_processes_claim_one_conversation_at_most_once(self):
        from agenthub.backend_worker import Worker
        Worker(self.store, self.config, live=False).prepare()
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        processes = [context.Process(target=_claim,
            args=(str(self.store.home), self.dsn, self.config, output)) for _ in range(4)]
        for process in processes:
            process.start()
        results = [output.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all("error" not in result for result in results), results)
        self.assertEqual(len([result for result in results if result["job"]]), 1, results)

    def test_partial_capture_fact_delivery_warns_and_source_withdrawal_denies(self):
        from agenthub.cloud_runtime import CloudStore
        from agentclient.enterprise_contract import VERSION
        # Reuse the canonical opt-in, real claim, history and activation checks.
        self.test_partial_capture_requires_opt_in_and_retains_coverage_gap()
        store=CloudStore(self.store.home,self.dsn,'orchard')
        store.curation_enabled=True  # Explicit optional-enrichment scenario.
        store.hybrid_enabled=True
        store.retrieval_selection_policy='facets_v2'
        store.retrieval_authorization_shape='authorized_cte'
        result=store.search(self.ctx,{'version':VERSION,'project':'maple',
            'query':'CSV','mode':'explicit'})
        self.assertTrue(result['results'])
        self.assertIn('incomplete_source_capture',result['coverage_gaps'])
        self.assertLessEqual(len(__import__('json').dumps(result,ensure_ascii=True)),4000)
        explanation=store.search(self.ctx,{'version':VERSION,'project':'maple',
            'query':'Why did the user choose CSV?','mode':'explicit'})
        self.assertTrue(explanation['answerable'])
        self.assertIn('incomplete_source_capture',explanation['coverage_gaps'])
        with store.open() as state:
            source=state.db.execute("SELECT s.id FROM enterprise_sources s JOIN memories m ON m.id=s.id WHERE m.kind='UserPromptSubmit'").fetchone()[0]
        store.lifecycle(self.ctx,{'version':VERSION,'target_id':source,'expected_revision':'1',
            'idempotency_key':'partial-source-withdraw','operation':'withdraw','reason':'synthetic withdrawal'})
        denied=store.search(self.ctx,{'version':VERSION,'project':'maple',
            'query':'CSV','mode':'explicit'})
        self.assertFalse(denied['results'])

    def test_completed_worker_does_not_wait_for_global_index_repair_lock(self):
        import hashlib
        import time
        from concurrent.futures import ThreadPoolExecutor
        from agenthub.backend_worker import Worker
        from agenthub.cloud_runtime import CloudStore
        from agentclient.enterprise_capture import normalize_capture
        store=CloudStore(self.store.home,self.dsn,'orchard')
        store.curation_enabled=True  # Explicit optional-enrichment scenario.
        for kind in ('UserPromptSubmit','Stop'):
            store.ingest_general(self.ctx,normalize_capture({'hook_event_name':kind,
                'event_id':'second-'+kind,'session_id':'second-chat','turn_id':'1',
                'prompt':'No new durable work.','last_assistant_message':'Acknowledged.'},'maple','agent'))
        def runner(*args,**kwargs):
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},{},None
        worker=Worker(store,self.config,runner=runner,live=False);worker.prepare()
        with store.open() as state,state.db:
            state.db.execute("INSERT INTO cloud_vector_generations(id,model_key,dimension,status,created) VALUES('lock-fixture','fixture',512,'active',?)",(time.time(),))
            state.db.execute("INSERT INTO cloud_vector_state VALUES(1,'lock-fixture','fixture',?)",(time.time(),))
            jobs=[r[0] for r in state.db.execute('SELECT id FROM curation_episode_jobs')]
        policy_key=int.from_bytes(hashlib.sha256(b'orchard').digest()[:8],'big',signed=True)
        # A concurrent observer may legitimately retain the shared policy guard.
        # Completion must not escalate to an unrelated exclusive maintenance lock.
        with store.open() as guarded,guarded.db:
            guarded.db.execute('SELECT pg_advisory_xact_lock_shared(?)',(policy_key,))
            with ThreadPoolExecutor(max_workers=1) as pool:
                result=pool.submit(worker.run,max_jobs=1,max_calls=2,max_seconds=20,
                    refresh_queue=False,episode_job_ids=jobs).result(timeout=15)
            self.assertEqual(result['completed'],1)

    def test_saved_private_return_and_progress_are_purged_on_policy_change(self):
        from agenthub.backend_worker import Worker
        calls=[]
        def runner(*args,**kwargs):
            calls.append(1)
            return {'records':[],'episode_summary':{'intent':'private fixture intent','open_work':[]}}, {}, '00000000-0000-4000-8000-000000000001'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        with patch('agenthub.processing.episode_pipeline._install',side_effect=ValueError('install_fault')):
            self.assertEqual(worker.run(max_seconds=10)['completed'],0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0],1)
            job=state.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
        self.store.connection_policy(self.ctx,'agent',active=False)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0],'{}')
            self.assertIsNone(state.db.execute('SELECT pending_result FROM backend_jobs').fetchone()[0])
        with self.assertRaisesRegex(ValueError,'inactive_worker_connection'):
            worker.recover(job,retain_validated=True)
        self.assertEqual(worker.run(max_seconds=10)['completed'],0)
        self.assertEqual(len(calls),1)

    def test_delete_during_provider_return_keeps_no_private_recovery_result(self):
        from agenthub.backend_worker import Worker
        from agentclient.enterprise_contract import VERSION
        def runner(*args,**kwargs):
            with self.store.open() as state:
                source=state.db.execute('SELECT id FROM enterprise_sources ORDER BY created LIMIT 1').fetchone()[0]
            self.store.lifecycle(self.ctx,{'version':VERSION,'target_id':source,'expected_revision':'1',
                'idempotency_key':'delete-during-dispatch','reason':'synthetic retention','operation':'delete'})
            return {'records':[],'episode_summary':{'intent':'must not remain cached','open_work':[]}}, {}, None
        self.assertEqual(Worker(self.store,self.config,runner=runner,live=False).run(max_seconds=10)['completed'],0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0],0)
            self.assertIsNone(state.db.execute('SELECT pending_result FROM backend_jobs').fetchone()[0])


if __name__ == "__main__":
    unittest.main()
