"""Real PG worker guards read checksummed original event segments before dispatch."""
import hashlib
import io
import json
from pathlib import Path
import unittest

from agentclient.general_contract import canonical
from agenthub.backend_worker import Worker
from agenthub.cloud_runtime import CloudStore
from agenthub.conversation_segments import ConversationSegments
from agenthub.enterprise import Denied
from agenthub.source_objects import FileSourceObjects,ObjectMissing,ObjectCorrupt
from test_cloud_postgres import PostgresFixture,SERVICES


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class SegmentWorkerTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.store.curation_enabled=True  # This fixture explicitly exercises optional enrichment.
        self.objects=FileSourceObjects(Path(self.temp.name)/'objects')
        self.segments=ConversationSegments(self.store,self.objects)
        self.store.conversation_segments=self.segments
        raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_segment_worker_v1.json').read_bytes()
        self.case=json.loads(raw)
        manifest=json.loads((Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_segment_worker_v1_manifest.json').read_text())
        expected='41dde49c6d4f72e567d36521712ea8d542696df7c210214939eb345cdc30f357'
        self.assertEqual(hashlib.sha256(raw).hexdigest(),expected)
        self.assertEqual(manifest['sha256'],expected)
        self.assertTrue(manifest['frozen_before_tests'])
        self.store.enroll_connection(self.ctx,'segment-agent','codex','maple',['agent'])
        self.source_events={}
        for event in self.case['events']:
            result=self.store.ingest_general(self.ctx,event)
            self.source_events[result['source_id']]=event
        self.config={'paused':False,'knowledge_backend':{'mode':'enterprise_local'},
            'observer':{'enabled':True,'min_interval_seconds':0,'max_calls_per_day':10000},
            'episode_curation':{'enabled':True,'policy':'durable_memory','generation_id':'segment-worker-fixture','settle_seconds':0},
            'backend_worker':{'allowed_connections':['segment-agent']}}
        self.calls=[]
        def runner(*args,**kwargs):
            self.calls.append(1)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, 'synthetic-segment-observer'
        self.worker=Worker(self.store,self.config,runner=runner,live=False)
        self.worker.prepare()
        with self.store.open() as state:
            self.job=state.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
            self.original=dict(state.db.execute('SELECT * FROM cloud_conversation_segments ORDER BY created LIMIT 1').fetchone())

    def tearDown(self):self.postgres_teardown()

    def assert_blocked(self):
        result=self.worker.run(max_jobs=1,max_calls=4,max_retries=1,max_seconds=20)
        self.assertEqual(result['completed'],0)
        self.assertEqual(result['calls'],0)
        self.assertEqual(self.calls,[])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_usage_attempts').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs WHERE id=?',(self.job,)).fetchone()[0],'held')

    def test_missing_original_blocks_dispatch_then_exact_operator_restore_and_retry(self):
        self.objects.delete(self.original['object_key'])
        self.assert_blocked()
        # No implicit replay from PostgreSQL's cached projection repairs an
        # accepted original. Restoration is explicit and byte-identical.
        with self.assertRaises(ObjectMissing):
            self.segments.retain_existing(self.ctx,[self.original['source_id']])
        event=self.source_events[self.original['source_id']]
        self.objects.put(self.original['object_key'],io.BytesIO(canonical(event)),expected_sha256=self.original['sha256'])
        self.worker.recover(self.job)
        result=self.worker.run(max_jobs=1,max_calls=4,max_retries=1,max_seconds=20)
        self.assertEqual(result['completed'],1);self.assertEqual(len(self.calls),1)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT policy_version FROM enterprise_sources WHERE id=?',(self.original['source_id'],)).fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT status FROM cloud_conversation_segments WHERE source_id=?',(self.original['source_id'],)).fetchone()[0],'active')

    def test_corrupt_original_blocks_dispatch_without_installed_or_cached_output(self):
        self.objects._path(self.original['object_key']).write_bytes(b'corrupt synthetic segment')
        with self.assertRaises(ObjectCorrupt):self.segments.fetch(self.ctx,self.original['source_id'])
        self.assert_blocked()

    def test_permission_revocation_during_response_denies_install_and_purges_private_return(self):
        def revoked(*args,**kwargs):
            self.calls.append(1)
            self.store.connection_policy(self.ctx,'segment-agent',active=False)
            return {'records':[],'episode_summary':{'intent':'private return must not be cached','open_work':[]}}, {}, None
        self.worker.runner=revoked
        self.assertEqual(self.worker.run(max_seconds=20)['completed'],0)
        self.assertEqual(len(self.calls),1)
        with self.assertRaises(Denied):self.segments.fetch(self.ctx,self.original['source_id'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],0)
            self.assertIsNone(state.db.execute('SELECT pending_result FROM backend_jobs WHERE id=?',(self.job,)).fetchone()[0])
        with self.assertRaisesRegex(ValueError,'inactive_worker_connection'):self.worker.recover(self.job)

    def test_delete_during_response_physically_purges_source_object_and_cached_payload(self):
        def deleted(*args,**kwargs):
            self.calls.append(1)
            self.store.lifecycle(self.ctx,{'version':'enterprise-local-1','operation':'delete',
                'target_id':self.original['source_id'],'expected_revision':'1','idempotency_key':'segment-delete-during-return',
                'reason':'synthetic retention'})
            return {'records':[],'episode_summary':{'intent':'private deleted-source return must disappear','open_work':[]}}, {}, None
        self.worker.runner=deleted
        self.assertEqual(self.worker.run(max_seconds=20)['completed'],0)
        with self.assertRaises(ObjectMissing):self.objects.head(self.original['object_key'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?',(self.original['source_id'],)).fetchone()[0],'{}')
            self.assertEqual(state.db.execute('SELECT body FROM memories WHERE id=?',(self.original['source_id'],)).fetchone()[0],'')
            self.assertIsNone(state.db.execute('SELECT pending_result FROM backend_jobs WHERE id=?',(self.job,)).fetchone()[0])
            self.assertEqual(state.db.execute('SELECT status FROM cloud_conversation_segments WHERE source_id=?',(self.original['source_id'],)).fetchone()[0],'removed')


if __name__=='__main__':unittest.main()
