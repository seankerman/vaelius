import hashlib
import json
import os
from pathlib import Path
import unittest
import uuid

from vaelius_test_support.hub.cloud_load import (SYNTHETIC_MODEL_KEY,SyntheticVolumeEmbedder,
    fixture_text,_records,provision,seed_volume,probe,compare_authorization)


class VolumeFixtureTests(unittest.TestCase):
    def test_separately_frozen_non_exact_hybrid_workload(self):
        path=Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_volume_hybrid_v1.json'
        manifest=json.loads(path.with_name('cloud_volume_hybrid_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),manifest['sha256'])
        self.assertEqual(json.loads(path.read_text())['requested_queries'],200)
    def test_exact_passages_and_fixed_offsets_across_full_capacity(self):
        width=len(fixture_text(1))
        self.assertEqual(len(fixture_text(50000)),width)
        self.assertIn('/synthetic/volume/050000.tsv',fixture_text(50000))
        config={'namespace':'synthetic','tenants':{'acme':{'scope':'scope','session':'session','source_id':'source'}}}
        row=next(_records(config,'acme',2,2))
        self.assertEqual(row['span'][2:4],(width,2*width))
        self.assertEqual(row['memory'][3],fixture_text(2));self.assertEqual(row['support'][1],'source')
        self.assertEqual(row['vector'][3],hashlib.sha256(fixture_text(2).encode()).hexdigest())

    def test_synthetic_vectors_deterministic_and_distinct_from_real_model(self):
        model=SyntheticVolumeEmbedder()
        self.assertEqual(model.model_key,SYNTHETIC_MODEL_KEY)
        vector=model.embed_queries(['Volume record 000123'])[0]
        self.assertEqual(len(vector),512);self.assertEqual(sum(x*x for x in vector),1)
        self.assertEqual(vector[123],1);self.assertEqual(model.embed_documents(['Volume record 000123'])[0],vector)
        self.assertEqual(model.embed_queries(['unmatched synthetic query']),model.embed_queries(['unmatched synthetic query']))

    def test_finite_probe_and_seed_bounds_fail_before_database_calls(self):
        for count in (0,3,100002,True):
            with self.assertRaises(ValueError):seed_volume({},count)
        for count,seconds in ((0,60),(201,60),(20,61),(20,0)):
            with self.assertRaises(ValueError):probe({},queries=count,max_seconds=seconds)


@unittest.skipUnless(os.environ.get('CLOUD_LOAD_PARENT_PROFILE'),'explicit private volume parent profile required')
class VolumePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile=Path(os.environ['CLOUD_LOAD_PARENT_PROFILE'])/'volume-test-profiles'/uuid.uuid4().hex
        cls.config=provision(cls.profile,os.environ['CLOUD_LOAD_PARENT_PROFILE'])
        cls.seed=seed_volume(cls.config,20)

    def test_small_full_production_path_and_oracle(self):
        self.assertEqual(self.seed['original_objects'],2);self.assertEqual(self.seed['total_records'],20)
        self.assertEqual(self.seed['provider_calls'],0)
        for tenant in self.config['tenants']:
            self.assertTrue(compare_authorization(self.config,tenant)['oracle_matches'])
        result=probe(self.config,queries=10,max_seconds=30)
        self.assertEqual(result['status'],'PASS',result['failures']);self.assertEqual(result['completed_queries'],10)
        self.assertFalse(result['semantic_quality_evidence'])

    def test_resume_expands_same_two_originals_without_duplicate_records(self):
        result=seed_volume(self.config,40);self.assertEqual(result['total_records'],40)
        repeat=seed_volume(self.config,40);self.assertEqual(repeat['total_records'],40)
        self.assertEqual(len(list((self.profile/'objects').glob('*/*'))),2)

    def test_separate_application_role_cannot_open_other_tenant_database(self):
        from psycopg import OperationalError
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        from agenthub.postgres import connect
        values=conninfo_to_dict(self.config['tenants']['acme']['dsn'])
        wrong=make_conninfo(**(values|{'dbname':self.config['tenants']['bravo']['database']}))
        with self.assertRaises(OperationalError):connect(wrong)

    def test_non_exact_hybrid_specific_answer_and_vector_path(self):
        result=probe(self.config,queries=10,max_seconds=30,workload='hybrid')
        self.assertEqual(result['status'],'PASS',result)
        self.assertTrue(result['vector_volume_evidence']);self.assertFalse(result['semantic_quality_evidence'])


if __name__=='__main__':unittest.main()
