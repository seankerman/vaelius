"""Bounded source/query failure evidence, separate from cloud capacity claims."""
from concurrent.futures import ThreadPoolExecutor
import hashlib,io,json,os
from pathlib import Path
import threading,unittest,uuid
from vaelius_test_support.hub.cloud_load import provision,seed_volume,probe,_store
from agenthub.document_ingest import DocumentStore
from agenthub.enterprise import Denied
from agenthub.source_objects import FileSourceObjects
import test_cloud_objects


class FaultFixtureTests(unittest.TestCase):
    def test_frozen_supplementary_cases(self):
        path=Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_faults_v1.json'
        manifest=json.loads(path.with_name('cloud_faults_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),manifest['sha256'])


@unittest.skipUnless(os.environ.get('CLOUD_TEST_DSN'),'explicit PostgreSQL source fixture required')
class SlowOriginalPolicyTests(unittest.TestCase):
    def setUp(self):
        test_cloud_objects.DocumentPostgresTests.setUp(self)
    def tearDown(self):test_cloud_objects.DocumentPostgresTests.tearDown(self)
    def test_slow_original_read_rechecks_policy_before_return(self):
        receipt=self.documents.ingest(self.ctx[self.alice],self.connection,'slow','1','slow.md',
            io.BytesIO(b'# Slow original\nRetained original bytes are current-policy protected.\n'))
        began=threading.Event();released=threading.Event();open_original=self.objects.open
        def delayed(key):
            began.set()
            if not released.wait(5):raise RuntimeError('synthetic_slow_object_deadline')
            return open_original(key)
        self.objects.open=delayed
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.documents.fetch,self.ctx[self.bob],receipt['source_id'])
            try:
                self.assertTrue(began.wait(3),'object read did not reach bounded delay')
                self.store.connection_policy(self.ctx[self.alice],self.connection,reader_ids=[self.alice])
            finally:released.set()
            with self.assertRaises(Denied):future.result(timeout=5)


@unittest.skipUnless(os.environ.get('CLOUD_LOAD_PARENT_PROFILE'),'explicit isolated volume fixture parent required')
class TenantNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent=Path(os.environ['CLOUD_LOAD_PARENT_PROFILE']);cls.root=parent/'fault-test-profiles'/uuid.uuid4().hex
        cls.config=provision(cls.root,parent);seed_volume(cls.config,100)
    def test_two_query_processes_and_ingestion_make_bounded_progress(self):
        store=_store(self.config,'acme');ctx=store.authenticate(self.config['tenants']['acme']['token'])
        documents=DocumentStore(store,FileSourceObjects(self.root/'objects/acme'))
        with ThreadPoolExecutor(max_workers=3) as pool:
            queries=[pool.submit(probe,self.config,queries=10,max_seconds=20) for _ in range(2)]
            accepted=pool.submit(documents.ingest,ctx,'volume-original','parallel-original','1','parallel.md',
                io.BytesIO(b'# Parallel source\nSaved at /synthetic/parallel.tsv because the bounded query loop remains active.\n'))
            self.assertEqual(accepted.result(timeout=20)['model_calls'],0)
            for future in queries:
                result=future.result(timeout=22)
                self.assertEqual(result['status'],'PASS',result);self.assertEqual(result['completed_queries'],10)
    def test_one_unreachable_tenant_endpoint_does_not_change_other_authority(self):
        from psycopg import OperationalError
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        from agenthub.cloud_runtime import CloudStore
        values=conninfo_to_dict(self.config['tenants']['acme']['dsn'])
        with self.assertRaises(OperationalError):
            unavailable=CloudStore(self.root/'unreachable-state',make_conninfo(**(values|{'host':'127.0.0.1','port':'1'})),'acme')
            unavailable.authenticate(self.config['tenants']['acme']['token'])
        live=_store(self.config,'bravo');ctx=live.authenticate(self.config['tenants']['bravo']['token'])
        result=live.search(ctx,{'version':'enterprise-local-1','query':'Volume record 000001','project':'synthetic-volume','mode':'explicit'})
        self.assertTrue(result['answerable'])
