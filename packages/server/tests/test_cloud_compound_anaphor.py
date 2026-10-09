"""Compound actor/option references with current source/ACL boundaries."""
import hashlib
import json
from pathlib import Path
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture,SERVICES
SHA='62b182b8866922a76a7d1c1b9150e5b385982e30c0160fb9f90f7eded071ad35'
def fixture():
    raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_compound_anaphor_v1.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SHA:raise ValueError('frozen_compound_anaphor_changed')
    return json.loads(raw)
@unittest.skipUnless(SERVICES,'explicit real PostgreSQL required')
class CompoundAnaphorTests(PostgresFixture,unittest.TestCase):
    def setUp(self):self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme');self.case=fixture()['cases'][0]
    def tearDown(self):self.postgres_teardown()
    def install(self,case):
        project='anaphor-'+case['id'];self.store.create_project('acme',project)
        for principal in ('alice','bob'):self.store.set_membership('acme',project,principal,True)
        ctx=self.store.authenticate(self.tokens['alice'])
        source=self.store.ingest(ctx,{'version':VERSION,'external_id':project,'session':project,'turn':'1',
            'project':project,'kind':'Stop','body':case['lesson'],'visibility':'private','occurred_at':100})['source_id']
        doc=self.store.accept_reviewed_note(ctx,source,'Project Atlas 7 original',case['lesson'])['document_id']
        return ctx,project,source,doc
    def search(self,ctx,project,case,**filters):
        return self.store.search(ctx,{'version':VERSION,'query':case['query'],'project':project,'mode':'explicit',**filters})
    def test_frozen_actor_option_location_pairs(self):
        for case in fixture()['cases']:
            with self.subTest(case=case['id']):
                ctx,project,source,doc=self.install(case);result=self.search(ctx,project,case)
                self.assertEqual(result['answerable'],case['expected'])
                if case['expected']:self.assertEqual({r['id'] for r in result['results']},{doc})
    def test_absent_choices_are_not_resolved_to_affirmative_options(self):
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_compound_absence_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'ca3ec4d423e5317f050f5039d2c23654163d3e3552190da2d71f61d38f83a219')
        for case in json.loads(raw)['cases']:
            with self.subTest(case=case['id']):
                ctx,project,source,doc=self.install(case)
                self.assertFalse(self.search(ctx,project,case)['answerable'])
    def test_exact_filters_and_historical_cutoff_do_not_donate_anaphor_evidence(self):
        ctx,project,source,doc=self.install(self.case)
        for filters in ({'domain':'UNKNOWN_EXACT_DOMAIN'}, {'knowledge_type':'UNKNOWN_EXACT_TYPE'}, {'subject':'UNKNOWN_EXACT_SUBJECT'}, {'as_of':'1999-01-01'}):
            with self.subTest(filters=filters):self.assertFalse(self.search(ctx,project,self.case,**filters)['answerable'])
    def test_private_source_remains_hidden_from_other_current_reader(self):
        ctx,project,source,doc=self.install(self.case);bob=self.store.authenticate(self.tokens['bob'])
        result=self.search(bob,project,self.case);self.assertFalse(result['answerable']);self.assertFalse(result['results'])
    def test_withdrawn_source_cannot_supply_references(self):
        ctx,project,source,doc=self.install(self.case)
        self.assertTrue(self.search(ctx,project,self.case)['answerable'])
        self.store.lifecycle(ctx,{'version':VERSION,'target_id':source,'expected_revision':'1','operation':'withdraw',
            'idempotency_key':'anaphor-withdraw','reason':'fixture'})
        result=self.search(ctx,project,self.case);self.assertFalse(result['answerable']);self.assertNotIn(doc,{r['id'] for r in result['results']})
