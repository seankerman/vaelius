import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path

from agenthub.processing.evaluation_budget import reserve, snapshot, finish
PHASE_CAPS={"live_baseline":6000,"live_curation":6000,"consolidation":2000,"task_retrieval":40}
TOTAL_ATTEMPT_CAP=6000
RETRY_ATTEMPT_CAP=1000


class EvaluationBudgetTests(unittest.TestCase):
    def setUp(self):
        # Historical explicit diagnostic limits, not current owner authorization.
        caps = {"live_baseline":6000,"live_curation":6000,"consolidation":2000,"task_retrieval":40}
        patcher = patch.multiple('agenthub.processing.evaluation_budget', TOTAL_ATTEMPT_CAP=6000,
                                 RETRY_ATTEMPT_CAP=1000, PHASE_CAPS=caps)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_cache_telemetry_preserves_missing_values_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evaluation.sqlite"
            reserve(path, "without-cache", "live_curation")
            finish(path, "without-cache", "complete", {"input_tokens": 100})
            reserve(path, "with-cache", "live_curation")
            finish(path, "with-cache", "complete", {"input_tokens": 200,
                "cached_input_tokens": 128, "cache_write_input_tokens": 0})
            with sqlite3.connect(path) as db:
                rows = db.execute("SELECT cached_input_tokens,cache_write_input_tokens FROM evaluation_attempts ORDER BY created").fetchall()
            self.assertEqual(rows, [(None, None), (128, 0)])
    def test_authorized_ceiling_is_shared_not_added_per_arm(self):
        self.assertEqual(TOTAL_ATTEMPT_CAP, 6000)
        self.assertEqual(PHASE_CAPS["live_curation"], 6000)
        self.assertEqual(PHASE_CAPS["live_baseline"], 6000)
        self.assertEqual(PHASE_CAPS["consolidation"], 2000)
        self.assertEqual(RETRY_ATTEMPT_CAP, 1000)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evaluation.sqlite"
            reserve(path, "existing", "live_curation")
            with sqlite3.connect(path) as db:
                db.executemany("INSERT INTO evaluation_attempts(attempt_id,phase,status,created,updated) VALUES(?,?,'failed',0,0)",
                    [(str(i), "live_baseline" if i % 2 else "live_curation") for i in range(5999)])
            with self.assertRaisesRegex(ValueError, "budget_exhausted"):
                reserve(path, "extra", "live_curation")
            self.assertEqual(snapshot(path)["attempts"], 6000)

    def test_raised_ceiling_preserves_all_previously_recorded_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evaluation.sqlite"
            reserve(path, "existing", "live_curation")
            finish(path, "existing", "failed", {"input_tokens": 123})
            with sqlite3.connect(path) as db:
                db.executemany("INSERT INTO evaluation_attempts(attempt_id,phase,status,created,updated) VALUES(?,?,'complete',0,0)",
                    [(str(i), "live_curation") for i in range(2709)])
            current = snapshot(path)
            self.assertEqual(current["attempts"], 2710)
            self.assertEqual(current["remaining"], 3290)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT status,input_tokens FROM evaluation_attempts WHERE attempt_id='existing'").fetchone(), ("failed", 123))

    def test_attempts_are_reserved_before_calls_and_failed_calls_still_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"private"/"evaluation.sqlite"
            reserved=reserve(db,"campaign:case-1","live_curation")
            self.assertEqual(reserved["attempts"],1)
            finish(db,"campaign:case-1","failed")
            self.assertEqual(snapshot(db)["phases"]["live_curation"]["attempts"],1)
            with self.assertRaisesRegex(ValueError,"already_reserved"):
                reserve(db,"campaign:case-1","live_curation")

    def test_phase_cap_fails_closed_without_spending_another_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"evaluation.sqlite"
            for index in range(PHASE_CAPS["task_retrieval"]):
                reserve(db,f"campaign:{index}","task_retrieval")
            with self.assertRaisesRegex(ValueError,"budget_exhausted"):
                reserve(db,"campaign:one-too-many","task_retrieval")
            self.assertEqual(snapshot(db)["phases"]["task_retrieval"]["attempts"],
                PHASE_CAPS["task_retrieval"])

    def test_bounded_retry_pool_is_separate_from_planned_phase_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"evaluation.sqlite"
            for index in range(RETRY_ATTEMPT_CAP):
                reserve(db,f"retry:{index}","live_curation",retry=True)
            current=snapshot(db)
            self.assertEqual(current["attempts"],RETRY_ATTEMPT_CAP)
            self.assertEqual(current["retry_remaining"],0)
            self.assertEqual(current["phases"]["live_curation"]["attempts"],0)
            with self.assertRaisesRegex(ValueError,"budget_exhausted"):
                reserve(db,"retry:too-many","live_curation",retry=True)

    def test_receiver_retries_count_toward_the_receiving_attempt_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'evaluation.sqlite'
            for index in range(39):reserve(db,str(index),'task_retrieval')
            reserve(db,'receiver-retry','task_retrieval',retry=True)
            for retry in (False,True):
                with self.assertRaisesRegex(ValueError,'budget_exhausted'):
                    reserve(db,'extra-'+str(retry),'task_retrieval',retry=retry)
            phase=snapshot(db)['phases']['task_retrieval']
            self.assertEqual(phase['attempts'],40);self.assertEqual(phase['fresh_attempts'],39)
            self.assertEqual(phase['retry_attempts'],1);self.assertEqual(phase['remaining'],0)


if __name__=="__main__":unittest.main()
