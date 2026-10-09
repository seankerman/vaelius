"""Reviewed rejection retries must not replay rejected provider returns."""
import json
from test_backend_worker import WorkerTests
from agenthub.backend_worker import Worker
from agenthub.processing.episode_pipeline import retry_held_job


class ReviewedMemoryRetryTests(WorkerTests):
    def test_reviewed_rejection_uses_feedback_and_requires_retry_allowance(self):
        payloads=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            payloads.append(payload)
            if len(payloads)==1:
                return {'records':[dict(title='Synthetic report',text='Synthetic report.',
                    subject='CSV',facets=['fact'],actors=['agent'],artifact=None,rationale=None,
                    state='reported',occurred_date='',event_id='e999',evidence_span_ids=['s999'])]}, {}, 'fixture-session'
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, 'new-fixture-session'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_seconds=30)['completed'],0)
        with self.store.open() as state,state.db:
            row=state.db.execute('SELECT id,progress,error FROM curation_episode_jobs').fetchone()
            job=state.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
            self.assertEqual(row['error'],'memory_all_candidates_rejected')
            original_returns=[dict(r) for r in state.db.execute('SELECT * FROM backend_provider_returns')]
            # Synthetic live receipt exercises retry classification without a provider call.
            state.db.execute('UPDATE backend_worker_receipts SET is_live=1')
            retry_held_job(state.db,row['id'])
        worker.recover(job,retain_validated=True)
        blocked=worker.run(max_seconds=30,max_retries=0,refresh_queue=False)
        self.assertEqual((blocked['completed'],blocked['calls']),(0,0))
        self.assertEqual(len(payloads),1)
        worker.recover(job,retain_validated=True)
        resumed=worker.run(max_seconds=30,max_retries=1,refresh_queue=False)
        self.assertEqual((resumed['completed'],resumed['calls'],resumed['retries']),(1,1,1))
        self.assertEqual(payloads[1]['reviewed_retry']['cycle'],1)
        self.assertIn('reference_unknown',payloads[1]['reviewed_retry']['rejection_reasons'])
        with self.store.open() as state:
            progress=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertTrue(progress['failed_stage_outputs'])
            returns=[dict(r) for r in state.db.execute('SELECT * FROM backend_provider_returns')]
            self.assertTrue(all(r in returns for r in original_returns))
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],0)
