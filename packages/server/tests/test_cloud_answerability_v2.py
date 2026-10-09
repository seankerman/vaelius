"""Paired answerability regressions; no model or untouched-case claim."""
import hashlib
import json
from pathlib import Path
import unittest
from agenthub.cloud_retrieval import supported_answer

class AnswerabilityPairs(unittest.TestCase):
    def test_temporal_comparison_and_requested_quantity_pairs(self):
        for name,sha in [('cloud_temporal_actor_v1','18cd7e15033f96d764227114b41679bd7821e589d59c213613343ec59507fff2'),
                         ('cloud_requested_quantity_v1','62e2b67966e23235fac3b5f65c2f608555a32d9886aef85285432330f2b9237f'),
                         ('cloud_verification_facets_v1','90ea7c70333b02731651c88810ce26cccf240f46c9d95705028e6b9ada2ca793')]:
            raw=(Path(__file__).resolve().parent.parent/('tests/fixtures/service/'+name+'.json')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),sha)
            for case in json.loads(raw)['cases']:
                with self.subTest(case=case['id']):self.assertEqual(supported_answer(case['query'],case['claim']),case['expected'])
    def test_grammatical_actor_subject_pairs(self):
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_actor_subject_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'1a51012f92606b0b125e9244336685d440110dbcc128fb0302bfcda41cdd596d')
        for case in json.loads(raw)['cases']:
            with self.subTest(case=case['id']):self.assertEqual(supported_answer(case['query'],case['claim']),case['expected'])
    def test_same_sentence_attribution_pairs(self):
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_answerability_attribution_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'282fbd1600bb008c4c7d8d34030eb0151223bfcc239e7952faf1c410fd3f0850')
        for case in json.loads(raw)['cases']:
            with self.subTest(case=case['id']):self.assertEqual(supported_answer(case['query'],case['claim']),case['expected'])
    def test_supplemental_pairs(self):
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_answerability_v2.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'997b5740960ad69d4e3b9faaa7a59e90a8035a1b04946b538a2fc77b7392add9')
        self.assertEqual(hashlib.sha256(raw).hexdigest(),json.loads((Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_answerability_v2_manifest.json').read_text())['sha256'])
        for case in json.loads(raw)['cases']:
            with self.subTest(case=case['id']):self.assertEqual(supported_answer(case['query'],case['claim']),case['expected'])

from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture,SERVICES

@unittest.skipUnless(SERVICES,'explicit real PostgreSQL required')
class AnswerabilityPostgresPairs(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
    def tearDown(self):self.postgres_teardown()
    def test_frozen_pairs_use_canonical_scoping_and_delivery(self):
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_answerability_v2.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'997b5740960ad69d4e3b9faaa7a59e90a8035a1b04946b538a2fc77b7392add9')
        extra=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_answerability_attribution_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(extra).hexdigest(),'282fbd1600bb008c4c7d8d34030eb0151223bfcc239e7952faf1c410fd3f0850')
        actors=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_actor_subject_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(actors).hexdigest(),'1a51012f92606b0b125e9244336685d440110dbcc128fb0302bfcda41cdd596d')
        cases=json.loads(raw)['cases']+json.loads(extra)['cases']+json.loads(actors)['cases']
        for name,sha in [('cloud_temporal_actor_v1','18cd7e15033f96d764227114b41679bd7821e589d59c213613343ec59507fff2'),
                         ('cloud_requested_quantity_v1','62e2b67966e23235fac3b5f65c2f608555a32d9886aef85285432330f2b9237f'),
                         ('cloud_verification_facets_v1','90ea7c70333b02731651c88810ce26cccf240f46c9d95705028e6b9ada2ca793')]:
            raw=(Path(__file__).resolve().parent.parent/('tests/fixtures/service/'+name+'.json')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),sha);cases+=json.loads(raw)['cases']
        for case in cases:
            with self.subTest(case=case['id']):
                project='pairs-'+case['id']
                self.store.create_project('acme',project);self.store.set_membership('acme',project,'alice',True)
                ctx=self.store.authenticate(self.tokens['alice'])
                source=self.store.ingest(ctx,{'version':VERSION,'external_id':project,'session':project,
                    'turn':'1','project':project,'kind':'Stop','body':case['claim']['lesson'],
                    'visibility':'private','occurred_at':100})['source_id']
                doc=self.store.accept_reviewed_note(ctx,source,case['claim']['title'],case['claim']['lesson'])['document_id']
                result=self.store.search(ctx,{'version':VERSION,'query':case['query'],'project':project,'mode':'explicit'})
                self.assertEqual(result['answerable'],case['expected'])
                if case['expected']:self.assertEqual({c['id'] for c in result['results']},{doc})
                else:self.assertFalse(result['results'])
