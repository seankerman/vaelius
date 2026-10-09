import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from agenthub.processing.evaluation_budget import reserve, snapshot, finish

class RetryAuthorizationTests(unittest.TestCase):
    def setUp(self):
        # Historical explicit diagnostic limits, not current owner authorization.
        caps = {"live_baseline":6000,"live_curation":6000,"consolidation":2000,"task_retrieval":40}
        patcher = patch.multiple('agenthub.processing.evaluation_budget', TOTAL_ATTEMPT_CAP=6000,
                                 RETRY_ATTEMPT_CAP=1000, PHASE_CAPS=caps)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_authorized_retry_increase_retains_120_old_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.sqlite'
            reserve(path,'original','live_curation',retry=True)
            finish(path,'original','failed',{'input_tokens':37})
            with sqlite3.connect(path) as db:
                db.executemany("INSERT INTO evaluation_attempts(attempt_id,phase,status,created,updated,is_retry) VALUES(?,'live_curation','failed',0,0,1)",[(f'old-{i}',) for i in range(119)])
            reserve(path,'reviewed-121','live_curation',retry=True)
            status=snapshot(path)
            self.assertEqual((status['attempts'],status['retry_attempts'],status['retry_cap']),(121,121,1000))
            self.assertEqual(status['total_cap'],6000)
            self.assertEqual(status['retry_remaining'],879)
            self.assertEqual(status['phases']['consolidation']['cap'],2000)
            self.assertEqual(status['phases']['task_retrieval']['cap'],40)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT status,input_tokens FROM evaluation_attempts WHERE attempt_id='original'").fetchone(),('failed',37))

    def test_retry_ceiling_and_shared_ceiling_both_fail_closed(self):
        for retry_count,fresh_count in ((1000,0),(999,5001)):
            with self.subTest(retry_count=retry_count),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'ledger.sqlite'
                reserve(path,'seed','live_curation',retry=True)
                with sqlite3.connect(path) as db:
                    db.executemany("INSERT INTO evaluation_attempts(attempt_id,phase,status,created,updated,is_retry) VALUES(?,'live_curation','failed',0,0,?)",[(f'old-{i}',int(i<retry_count-1)) for i in range(retry_count+fresh_count-1)])
                before=snapshot(path)
                with self.assertRaisesRegex(ValueError,'budget_exhausted'):
                    reserve(path,'denied','live_curation',retry=True)
                self.assertEqual(snapshot(path),before)
