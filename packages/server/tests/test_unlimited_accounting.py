"""Owner-authorized accounting without pilot authorization ceilings."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agenthub.processing.evaluation_budget import reserve, snapshot, finish
from agenthub.processing.usage import Ledger


class UnlimitedAccountingTests(unittest.TestCase):
    def test_existing_total_retry_phase_and_failed_usage_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'evaluation.sqlite'
            reserve(path, 'original', 'live_curation')
            finish(path, 'original', 'failed', {'input_tokens': 123})
            with sqlite3.connect(path) as db:
                db.executemany("INSERT INTO evaluation_attempts(attempt_id,phase,status,created,updated,is_retry) VALUES(?,?,'failed',0,0,?)",
                    [(str(i), 'task_retrieval' if i < 50 else 'consolidation', int(i < 1100)) for i in range(6000)])
            for phase, retry in [('live_curation', False), ('consolidation', False), ('task_retrieval', True)]:
                reserve(path, 'new-' + phase, phase, retry=retry)
            status = snapshot(path)
            self.assertEqual(status['attempts'], 6004)
            self.assertIsNone(status['total_cap'])
            self.assertIsNone(status['remaining'])
            self.assertIsNone(status['retry_cap'])
            self.assertTrue(all(v['cap'] is None for v in status['phases'].values()))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT status,input_tokens FROM evaluation_attempts WHERE attempt_id='original'").fetchone(), ('failed', 123))
            with self.assertRaisesRegex(ValueError, 'already_reserved'):
                reserve(path, 'original', 'live_curation')

    def test_old_founder_and_installation_limits_do_not_gate_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger({'accounting_home': tmp, 'installation_call_limit': 1,
                             'observer': {'max_calls_per_day': 1}})
            try:
                first = ledger.reserve('observe', 'fixture')
                ledger.finish(first, 'failed', {'input_tokens': 37})
                second = ledger.reserve('evaluation', 'fixture')
                ledger.finish(second, 'done')
                status = ledger.summary()
                self.assertEqual(status['rolling_calls'], 2)
                self.assertEqual(status['tokens']['input_tokens'], 37)
                self.assertIsNone(status['limit'])
                self.assertIsNone(status['remaining'])
            finally:
                ledger.close()
