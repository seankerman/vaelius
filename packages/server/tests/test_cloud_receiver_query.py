"""Genuine receiver-query regression; authored/tuned, never held-out evidence."""
import hashlib
import json
from pathlib import Path
import unittest

from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture,SERVICES

FIXTURE_SHA='380226db93511b7ded320990d296d8ba0f2a859917ce563b201d223de08ee5e0'

@unittest.skipUnless(SERVICES,'explicit real PostgreSQL required')
class ReceiverQueryTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_receiver_generated_query_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),FIXTURE_SHA);self.case=json.loads(raw)
        self.docs={};self.sources={}
        for suffix,text in [('location','The Maple dataset is currently at /Users/demo/My Data/current-maple.csv.'),
                            ('reason','Maple uses CSV because the importer requires a stable header.')]:
            source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'receiver-'+suffix,'session':'receiver',
                'turn':suffix,'project':'maple','kind':'Stop','body':text,'visibility':'private','occurred_at':100})['source_id']
            self.sources[suffix]=source
            self.docs[suffix]=self.store.accept_reviewed_note(self.ctx,source,'Maple current '+suffix,text)['document_id']
    def tearDown(self):self.postgres_teardown()
    def search(self,query,**filters):
        return self.store.search(self.ctx,{'version':VERSION,'query':query,'project':'maple','mode':'explicit',**filters})
    def test_actual_generated_compound_current_query_returns_both_supported_facets(self):
        query=self.case['actual_generated_args']['query']
        result=self.search(query)
        self.assertTrue(result['answerable']);self.assertEqual({r['id'] for r in result['results']},set(self.docs.values()))
    def test_location_synonyms_use_existing_location_intent_without_dropping_reason(self):
        for query in ('Where is the Maple dataset and why choose CSV?',
                      'Maple dataset location and why choose CSV',
                      'Where the Maple dataset was saved and why choose CSV',
                      'Maple dataset stored and why choose CSV'):
            with self.subTest(query=query):
                result=self.search(query);self.assertTrue(result['answerable'])
                self.assertEqual({r['id'] for r in result['results']},set(self.docs.values()))
    def test_unknown_reason_subject_and_history_remain_unsupported(self):
        for query,filters in [('Maple dataset location and why choose JSON',{}),
                              ('Willow dataset location and why choose CSV',{}),
                              ('Maple dataset location and why choose CSV',{'subject':'UNKNOWN_EXACT_SUBJECT'}),
                              ('Maple dataset location and why choose CSV',{'domain':'UNKNOWN_EXACT_DOMAIN'}),
                              ('Maple dataset location and why choose CSV',{'knowledge_type':'UNKNOWN_EXACT_TYPE'}),
                              ('Maple dataset location and why choose CSV',{'event_day':'1999-01-01'}),
                              ('Maple dataset location and why choose CSV',{'as_of':'1999-01-01'})]:
            with self.subTest(query=query,filters=filters):self.assertFalse(self.search(query,**filters)['answerable'])
    def test_withdrawing_reason_keeps_current_location_but_denies_complete_answer(self):
        self.store.lifecycle(self.ctx,{'version':VERSION,'target_id':self.sources['reason'],'expected_revision':'1',
            'operation':'withdraw','idempotency_key':'receiver-reason-withdraw','reason':'fixture'})
        result=self.search(self.case['actual_generated_args']['query'])
        self.assertFalse(result['answerable']);self.assertNotIn(self.docs['reason'],{r['id'] for r in result['results']})
