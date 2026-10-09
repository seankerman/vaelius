"""PostgreSQL epoch rows contain values, not dictionary column names."""
import unittest
from agenthub.processing.context_delivery import current_epoch,record_postcompact_boundary
from test_cloud_postgres import PostgresFixture,SERVICES

@unittest.skipUnless(SERVICES,'explicit disposable PostgreSQL required')
class ContextEpochValues(PostgresFixture,unittest.TestCase):
    def setUp(self):self.postgres_setup()
    def tearDown(self):self.postgres_teardown()
    def test_compaction_advances_once_and_preserves_named_state(self):
        event={'hook_event_name':'PostCompact','trigger':'auto','session_id':'receiver','turn_id':'one'}
        with self.store.open() as state,state.db:
            self.assertEqual(current_epoch(state.db,'receiver')['epoch'],0)
            self.assertTrue(record_postcompact_boundary(state.db,event))
            self.assertFalse(record_postcompact_boundary(state.db,event))
            first=current_epoch(state.db,'receiver')
            self.assertEqual(first['epoch'],1)
            self.assertEqual(first['session'],'receiver')
            self.assertEqual(first['continuity_status'],'known')
            self.assertTrue(record_postcompact_boundary(state.db,dict(event,turn_id='two')))
            self.assertEqual(current_epoch(state.db,'receiver')['epoch'],2)
