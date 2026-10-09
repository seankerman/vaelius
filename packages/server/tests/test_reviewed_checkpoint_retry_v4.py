"""Only a current explicit review may discard an invalid saved stage."""
import hashlib
import json
import unittest
from test_backend_worker import WorkerTests
from agenthub.backend_worker import Worker
from agenthub.processing.episode_pipeline import retry_held_job

class ReviewedCheckpointRetryTests(unittest.TestCase):
    setUp=WorkerTests.setUp
    tearDown=WorkerTests.tearDown
    turn=WorkerTests.turn

    def rejected_checkpoint(self):
        self.requests=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            self.requests.append(payload)
            if len(self.requests)==1:
                return {'records':[dict(title='Synthetic invalid report',text='Synthetic invalid report.',subject='CSV',facets=['fact'],actors=['agent'],artifact=None,rationale=None,state='reported',occurred_date='',event_id='e999',evidence_span_ids=['s999'])]}, {}, 'first-session'
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, 'reviewed-session'
        self.worker=Worker(self.store,self.config,runner=runner,live=False)
        self.worker.run(max_seconds=30)
        with self.store.open() as st,st.db:
            st.db.execute("UPDATE curation_episode_jobs SET error='LockNotAvailable'")
            st.db.execute("UPDATE backend_jobs SET last_error='LockNotAvailable'")
            st.db.execute('UPDATE backend_worker_receipts SET is_live=1')
            self.row=dict(st.db.execute('SELECT * FROM curation_episode_jobs').fetchone())
            self.job=st.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
            self.old_returns=[dict(r) for r in st.db.execute('SELECT * FROM backend_provider_returns')]

    def test_current_review_discards_invalid_stage_without_replaying_or_losing_history(self):
        self.rejected_checkpoint()
        with self.store.open() as st:
            retry_held_job(st.db,self.row['id'],discard_invalid_stages=True,
                expected_progress_sha256=hashlib.sha256(self.row['progress'].encode()).hexdigest(),
                reviewed_errors=('reference_unknown',))
        self.worker.recover(self.job,retain_validated=True)
        denied=self.worker.run(max_seconds=30,max_retries=0,refresh_queue=False)
        self.assertEqual((denied['calls'],len(self.requests)),(0,1))
        self.worker.recover(self.job,retain_validated=True)
        result=self.worker.run(max_seconds=30,max_retries=1,refresh_queue=False)
        self.assertEqual((result['completed'],result['calls'],result['retries']),(1,1,1))
        self.assertIn('reference_unknown',self.requests[1]['reviewed_retry']['rejection_reasons'])
        with self.store.open() as st:
            progress=json.loads(st.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertEqual(progress['failed_stage_outputs'][-1]['outputs'],json.loads(self.row['progress'])['stage_outputs'])
            self.assertEqual(progress['failed_stage_outputs'][-1]['reviewed_errors'],['reference_unknown'])
            returns=[dict(r) for r in st.db.execute('SELECT * FROM backend_provider_returns')]
            self.assertTrue(all(r in returns for r in self.old_returns))
            self.assertEqual(st.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],0)

    def test_stale_review_or_unbounded_feedback_cannot_clear_saved_output(self):
        self.rejected_checkpoint()
        for digest,errors in [('0'*64,('reference_unknown',)),(hashlib.sha256(self.row['progress'].encode()).hexdigest(),('private source text',))]:
            with self.subTest(errors=errors),self.store.open() as st:
                with self.assertRaisesRegex(ValueError,'reviewed_stage'):
                    retry_held_job(st.db,self.row['id'],discard_invalid_stages=True,expected_progress_sha256=digest,reviewed_errors=errors)
                current=st.db.execute('SELECT progress,status FROM curation_episode_jobs').fetchone()
                self.assertEqual((current['progress'],current['status']),(self.row['progress'],'held'))
        self.assertEqual(len(self.requests),1)
