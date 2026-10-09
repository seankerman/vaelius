import unittest
from agentclient.enterprise_capture import normalize_capture
from agentclient.general_contract import split_event
from test_cloud_postgres import PostgresFixture,SERVICES

@unittest.skipUnless(SERVICES,'explicit local PostgreSQL required')
class CloudTransportTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store.enroll_connection(self.ctx,'agent','synthetic','maple',['agent'])
    def tearDown(self):self.postgres_teardown()
    def test_complete_multipart_round_trip_and_identical_transport_retry(self):
        event=normalize_capture({'hook_event_name':'PostToolUse','event_id':'save','session_id':'chat',
            'turn_id':'first','tool_name':'save_file','tool_use_id':'one','tool_input':{'path':'/synthetic/maple.csv',
            'password':'synthetic-canary'},'tool_response':{'status':'saved'},'exit_code':0},'maple','agent')
        parts=split_event(event);result=None
        for part in reversed(parts):result=self.store.ingest_part(self.ctx,part)
        self.assertTrue(result['complete'])
        duplicate=self.store.ingest_part(self.ctx,parts[0]);self.assertTrue(duplicate['complete']);self.assertEqual(duplicate['disposition'],'duplicate')
        retained=self.store.general_source(self.ctx,result['source_id'])['payload']
        self.assertEqual(retained,event)
        self.assertFalse(self.store.search(self.ctx,{'version':'enterprise-local-1','query':'maple.csv'})['answerable'])
