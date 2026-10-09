"""A recovered pre-dispatch hold must not consume the scarce model retry pool."""
import sqlite3
import unittest

from agenthub.backend_worker import _is_model_retry


class HistoricalWorkerRetryTests(unittest.TestCase):
    def test_next_stage_after_a_saved_return_is_fresh_but_uncertain_attempt_is_retry(self):
        db = sqlite3.connect(':memory:'); self.addCleanup(db.close)
        db.execute('CREATE TABLE backend_worker_receipts(job_id TEXT,attempt_id TEXT,purpose TEXT,is_live INTEGER,status TEXT)')
        db.execute('CREATE TABLE backend_provider_returns(job_id TEXT,purpose TEXT,request_hash TEXT)')
        job={'id':'job','attempts':2,'dispatch_attempt':'saved'}
        db.execute("INSERT INTO backend_worker_receipts VALUES('job','saved','durable_memory_curate',1,'returned')")
        db.execute("INSERT INTO backend_provider_returns VALUES('job','durable_memory_curate','stage-0')")
        self.assertFalse(_is_model_retry(db,job,'durable_memory_curate',request_hash='stage-1'))
        self.assertTrue(_is_model_retry(db,job,'durable_memory_curate',request_hash='stage-0'))
        db.execute("INSERT INTO backend_worker_receipts VALUES('job','timeout','durable_memory_curate',1,'failed_or_uncertain')")
        self.assertTrue(_is_model_retry(db,job,'durable_memory_curate',request_hash='stage-1'))

    def test_only_a_prior_dispatch_for_the_same_purpose_counts_as_retry(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE backend_worker_receipts(job_id TEXT,attempt_id TEXT,purpose TEXT,is_live INTEGER)")
        job = {"id": "job", "attempts": 2, "dispatch_attempt": None}
        self.assertFalse(_is_model_retry(db, job, "durable_memory_curate"))
        db.execute("INSERT INTO backend_worker_receipts VALUES('job','fixture','durable_memory_curate',0)")
        self.assertFalse(_is_model_retry(db, job, "durable_memory_curate"))
        db.execute("INSERT INTO backend_worker_receipts VALUES('job','attempt-1','durable_memory_curate',1)")
        self.assertTrue(_is_model_retry(db, job, "durable_memory_curate"))
        self.assertFalse(_is_model_retry(db, job, "episode_resolve"))
        job["dispatch_attempt"] = "attempt-1"
        self.assertFalse(_is_model_retry(db, job, "episode_resolve"))
        job["dispatch_attempt"] = "missing-receipt"
        self.assertTrue(_is_model_retry(db, job, "episode_resolve"))


if __name__ == "__main__":
    unittest.main()
