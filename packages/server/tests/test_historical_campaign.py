"""A long historical run stops cleanly and can be resumed without a new corpus."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agenthub.historical_campaign import campaign


class HistoricalCampaignTests(unittest.TestCase):
    def test_bounded_partial_then_complete_preserves_progress_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def runner(*args, **kwargs):
                calls.append(kwargs['max_total_seconds'])
                return ({'status': 'partial', 'lines_advanced': 10, 'events_queued': 3,
                         'events_acknowledged': 3, 'remaining_sessions': 1}
                        if len(calls) == 1 else
                        {'status': 'complete', 'lines_advanced': 4, 'events_queued': 1,
                         'events_acknowledged': 1, 'remaining_sessions': 0})
            result = campaign('profile', 'token', 'manifest', directory,
                              max_hours=1, runner=runner)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['rounds'], 2)
            self.assertEqual(result['events_acknowledged'], 4)
            self.assertEqual(json.loads((Path(directory) / 'campaign-status.json').read_text()), result)

    def test_transient_postgres_lock_retries_within_one_finite_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes=iter([
                {'status':'transport_blocked','last_error_type':'LockNotAvailable'},
                {'status':'transport_blocked','last_error_type':None},
                {'status':'complete','lines_advanced':1,'events_acknowledged':1,
                 'events_queued':1,'remaining_sessions':0}])
            with patch('agenthub.historical_campaign.time.sleep') as sleep:
                result=campaign('profile','token','manifest',directory,
                                max_hours=1,runner=lambda *a,**k:next(outcomes))
            self.assertEqual((result['status'],result['rounds'],result['transient_retries']),
                             ('complete',3,2))
            self.assertEqual(sleep.call_count,2)

    def test_nontransient_transport_block_stops_for_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            result=campaign('profile','token','manifest',directory,max_hours=1,
                            runner=lambda *a,**k:{'status':'transport_blocked',
                                'last_error_type':'Denied'})
            self.assertEqual((result['status'],result['rounds']),('transport_blocked',1))

    def test_transient_retries_have_a_fixed_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('agenthub.historical_campaign.time.sleep') as sleep:
                result=campaign('profile','token','manifest',directory,max_hours=1,
                    runner=lambda *a,**k:{'status':'transport_blocked',
                                           'last_error_type':'LockNotAvailable'})
            self.assertEqual((result['status'],result['rounds'],result['transient_retries']),
                             ('transport_blocked',9,8))
            self.assertEqual(sleep.call_count,8)


if __name__ == '__main__':
    unittest.main()
