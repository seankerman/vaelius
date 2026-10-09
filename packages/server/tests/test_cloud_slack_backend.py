"""Actual bounded connector CLI against disposable tenant DBs and local Slack HTTP.

Provider/model/vendor calls are zero. The manifest was frozen before behavior edits.
Set AGENTNETWORK_PG_SERVICES to an explicit local private services file.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from agenthub.backend_worker import Worker
from agenthub.cloud_local import private_json, runtime
from agenthub.cloud_profile import setup_profile
from agenthub.postgres import connect
from agenthub.slack_connector import SlackConnector, SlackHTTP, SlackHeld
from agenthub.source_objects import ObjectMissing
from test_cloud_slack import SlackServer, FIXTURE

SERVICES=os.environ.get('AGENTNETWORK_PG_SERVICES')
MANIFEST_SHA='e104de9a0f621872344ebab30319f5d78ed6e26081661f141043cde01a65e737'


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class SlackBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture=Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_slack_backend_v1.json'
        assert hashlib.sha256(fixture.read_bytes()).hexdigest()==MANIFEST_SHA
        manifest=json.loads(fixture.with_name('cloud_slack_backend_v1_manifest.json').read_text())
        assert manifest['sha256']==MANIFEST_SHA and manifest['frozen_before_implementation']
        deletion=fixture.with_name('cloud_slack_backend_deletion_v1.json')
        frozen=json.loads(deletion.with_name('cloud_slack_backend_deletion_v1_manifest.json').read_text())
        assert hashlib.sha256(deletion.read_bytes()).hexdigest()==frozen['sha256']
        assert frozen['sha256']=='8ce4de2fa889b678e61ce42fec53823fa9f721ec11fc3f6b69db91b90ad6dd23'
        assert frozen['frozen_before_implementation']
        cls.deletion_case=json.loads(deletion.read_text())
        cls.temp=tempfile.TemporaryDirectory(prefix='cloud-slack-backend-')
        cls.profile=Path(cls.temp.name)/'profile'
        private_json(cls.profile/'runtime.json',{'objects':{'kind':'file','root':str(cls.profile/'objects')},
            'processing':{'curation_enabled':True}})  # Explicit enrichment fixture, not a runtime default.
        cls.provision=setup_profile(cls.profile,SERVICES,namespace='test_slack_'+uuid.uuid4().hex[:10])
        cls.operator=json.loads((cls.profile/'operator.json').read_text())

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql
        bootstrap=json.loads(Path(SERVICES).read_text())['admin_dsn']
        with connect(bootstrap,autocommit=True) as db:
            for name in cls.provision['databases']:
                db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in [cls.operator['control_role']]+[t['role'] for t in cls.operator['tenants'].values()]:
                db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        cls.temp.cleanup()

    def setUp(self):
        self.server=SlackServer()
        self.settings,self.registry=runtime(self.profile)
        self.store=self.registry.resolve('acme')
        self.token=(self.profile/'credentials/acme-alice.token').read_text().strip()
        self.ctx=self.store.authenticate(self.token)
        self.ident='cli-'+uuid.uuid4().hex[:10]
        self.config={'backend_profile':str(self.profile),'tenant':'acme','enterprise_token':self.token,
            'slack_token':'synthetic-token','endpoint':self.server.endpoint,'allow_vendor':False,
            'connector':self.ident,'team':FIXTURE['team'],'channel':FIXTURE['channel'],
            'project':'enrolled-test','bindings':{'U_ALICE':'alice','U_BOB':'bob'},
            # Matching legacy fields expose the pre-fix silent profile omission.
            'home':str(self.store.home),'dsn':self.store.dsn}
        self.config_path=self.profile/(self.ident+'.json')
        private_json(self.config_path,self.config)
        self.connector=SlackConnector(self.store,SlackHTTP('synthetic-token',endpoint=self.server.endpoint))
        self.connection='slack-'+self.ident

    def tearDown(self):
        self.server.close()

    def cli(self,command,*flags,config=None,success=True):
        if config is not None:private_json(self.config_path,config)
        env=os.environ.copy()
        if env.get('PYTHONPATH'):
            env['PYTHONPATH']=os.pathsep.join(str(Path(p).resolve()) for p in env['PYTHONPATH'].split(os.pathsep))
        result=subprocess.run([sys.executable,'-m','agenthub.slack_connector','--profile',str(self.config_path),command,*flags],
            cwd=self.profile,env=env,capture_output=True,text=True,timeout=40)
        if success:
            self.assertEqual(result.returncode,0,'connector CLI failed; private error retained in subprocess result')
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode,0)
        return result

    def worker(self):
        calls=[]
        def runner(*args,**kwargs):
            calls.append(1)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, 'synthetic-slack-observer'
        config={'paused':False,'knowledge_backend':{'mode':'enterprise_local'},
            'observer':{'enabled':True,'model':'fixture','min_interval_seconds':0,'max_calls_per_day':10000},
            'episode_curation':{'enabled':True,'policy':'durable_memory','generation_id':self.ident,'settle_seconds':0},
            'backend_worker':{'allowed_connections':[self.connection]}}
        worker=Worker(self.store,config,runner=runner,live=False)
        return worker,calls

    def sources(self):
        with self.store.open() as state:
            return [dict(r) for r in state.db.execute('SELECT * FROM backend_source_revisions WHERE connection=?',(self.connection,))]

    def event(self,event,event_id):
        value={'type':'event_callback','team_id':FIXTURE['team'],'event_id':event_id,'event':event}
        raw=json.dumps(value,sort_keys=True).encode();when=str(int(time.time()));secret='synthetic-secret'
        headers={'X-Slack-Request-Timestamp':when,'X-Slack-Signature':'v0='+hmac.new(secret.encode(),b'v0:'+when.encode()+b':'+raw,hashlib.sha256).hexdigest()}
        return self.connector.event(self.ctx,self.ident,raw,headers,secret)

    def counts(self):
        with self.store.open() as state:
            return {table:state.db.execute('SELECT count(*) FROM '+table+' WHERE source_id IN (SELECT source_id FROM backend_source_revisions WHERE connection=?)',
                (self.connection,)).fetchone()[0] for table in ('cloud_conversation_segments','cloud_segment_uploads')}

    def test_actual_cli_cloud_profile_retains_verified_segments_worker_and_retry(self):
        # Cloud config does not require duplicated routing/DSN secrets.
        cloud=self.config.copy();cloud.pop('dsn');cloud.pop('home')
        self.cli('enroll',config=cloud)
        first=self.cli('sync','--max-messages','20','--max-pages','5')
        self.assertEqual(first['upserts'],4)
        rows=self.sources();self.assertGreaterEqual(len(rows),6)
        for row in rows:
            with self.store.open() as state:
                self.store.conversation_segments.verify_source(state.db,row['source_id'])
        before=self.counts();self.assertEqual(before['cloud_conversation_segments'],len(rows))
        with self.store.open() as state:
            metric_before=state.db.execute("SELECT count(*) FROM cloud_metrics WHERE id LIKE 'segment:%' AND tenant='acme'").fetchone()[0]
        again=self.cli('sync','--max-messages','20','--max-pages','5')
        self.assertEqual(again['duplicates'],4);self.assertEqual(again['upserts'],0)
        self.assertEqual(self.counts(),before);self.assertEqual(len(self.sources()),len(rows))
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT count(*) FROM cloud_metrics WHERE id LIKE 'segment:%' AND tenant='acme'").fetchone()[0],metric_before)
        worker,calls=self.worker();result=worker.run(max_jobs=10,max_calls=12,max_retries=0,max_seconds=30)
        self.assertGreater(result['completed'],0);self.assertGreater(len(calls),0)
        self.assertFalse(list(self.profile.rglob('*.sqlite')))

    def test_pre_fix_profile_omission_is_missing_original_hold_baseline(self):
        # Set SLACK_BACKEND_BASELINE=1 only before behavior edits to retain failure evidence.
        if not os.environ.get('SLACK_BACKEND_BASELINE'):self.skipTest('historical pre-fix reproduction only')
        self.cli('enroll');self.cli('sync')
        self.assertGreater(len(self.sources()),0)
        self.assertEqual(self.counts()['cloud_conversation_segments'],0)
        worker,calls=self.worker();result=worker.run(max_jobs=10,max_calls=12,max_retries=0,max_seconds=30)
        self.assertEqual(result['completed'],0);self.assertEqual(calls,[])
        with self.store.open() as state:
            errors=[r[0] for r in state.db.execute('SELECT last_error FROM backend_jobs WHERE id IN (SELECT j.id FROM backend_jobs j JOIN backend_observers o ON o.id=j.observer_id WHERE o.connection=?)',(self.connection,))]
            self.assertTrue(any('segment_original_required' in str(e) for e in errors))

    def test_object_failure_holds_checkpoint_and_no_dispatch_then_cli_recovery(self):
        self.cli('enroll')
        with patch.object(self.store.conversation_segments.objects,'put',side_effect=OSError('synthetic object failure')):
            with self.assertRaises(SlackHeld):self.connector.sync(self.ctx,self.ident)
        status=self.connector.status(self.ctx,self.ident)
        self.assertEqual(status['status'],'held');self.assertEqual(status['cursor'],'0')
        self.assertFalse(status['policy_fresh']);self.assertEqual(self.sources(),[])
        worker,calls=self.worker();self.assertEqual(worker.run(max_seconds=5)['completed'],0);self.assertEqual(calls,[])
        self.assertEqual(self.cli('sync')['upserts'],4)
        for row in self.sources():
            with self.store.open() as state:self.store.conversation_segments.verify_source(state.db,row['source_id'])
        self.assertGreater(worker.run(max_jobs=10,max_calls=12,max_retries=0,max_seconds=30)['completed'],0)

    def test_signed_delete_purges_object_and_pg_content_no_poll_resurrection(self):
        self.cli('enroll');self.cli('sync')
        external=FIXTURE['channel']+':1000.000001'
        independent='independent-'+self.ident
        self.connector.enroll(self.ctx,independent,FIXTURE['team'],FIXTURE['channel'],
            'enrolled-test',{'U_ALICE':'alice','U_BOB':'bob'})
        self.connector.sync(self.ctx,independent)
        with self.store.open() as state:
            unrelated=dict(state.db.execute('SELECT c.source_id,c.object_key FROM cloud_conversation_segments c JOIN backend_source_revisions r USING(source_id) WHERE r.connection=? AND r.external_id=?',('slack-'+independent,external)).fetchone())
        edit={'type':'message','subtype':'message_changed','channel':FIXTURE['channel'],
            'event_ts':self.deletion_case['edit']['event_ts'],
            'message':{'user':'U_ALICE','text':self.deletion_case['edit']['text'],'ts':self.deletion_case['stamp']}}
        self.assertEqual(self.event(edit,'edit-'+self.ident)['disposition'],'upserted')
        with self.store.open() as state:
            originals=[dict(r) for r in state.db.execute('SELECT c.source_id,c.object_key FROM cloud_conversation_segments c JOIN backend_source_revisions r USING(source_id) WHERE r.connection=? AND r.external_id=?',(self.connection,external))]
            other=[dict(r) for r in state.db.execute('SELECT c.source_id,c.object_key FROM cloud_conversation_segments c JOIN backend_source_revisions r USING(source_id) WHERE r.connection=? AND r.external_id<>? AND c.status=\'active\'',(self.connection,external))]
        self.assertEqual(len(originals),2);self.assertTrue(other)
        deletion={'type':'message','subtype':'message_deleted','channel':FIXTURE['channel'],
            'deleted_ts':self.deletion_case['stamp'],'event_ts':self.deletion_case['delete']['event_ts']}
        self.assertEqual(self.event(deletion,'delete-'+self.ident)['disposition'],'deleted')
        self.assertEqual(self.event(deletion,'delete-'+self.ident)['disposition'],'duplicate')
        for row in originals:
            with self.assertRaises(ObjectMissing):self.store.conversation_segments.objects.head(row['object_key'])
        for row in other:self.store.conversation_segments.objects.head(row['object_key'])
        self.store.conversation_segments.objects.head(unrelated['object_key'])
        self.cli('sync')
        with self.store.open() as state:
            self.assertNotEqual(state.db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?',(unrelated['source_id'],)).fetchone()[0],'{}')
            self.assertEqual(state.db.execute('SELECT active FROM backend_slack_items WHERE connector=? AND external_id=?',(self.ident,external)).fetchone()[0],0)
            for row in originals:
                self.assertEqual(state.db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?',(row['source_id'],)).fetchone()[0],'{}')
                self.assertEqual(state.db.execute('SELECT body FROM memories WHERE id=?',(row['source_id'],)).fetchone()[0],'')

    def test_conflicting_routes_and_wrong_tenant_refuse_before_http(self):
        bad=[]
        bad.append(self.config|{'dsn':self.operator['tenants']['bravo']['dsn']})
        bad.append(self.config|{'home':str(self.profile/'wrong-home')})
        bad.append(self.config|{'tenant':'absent'})
        bad.append(self.config|{'enterprise_token':(self.profile/'credentials/bravo-alice.token').read_text().strip()})
        bad.append(self.config|{'backend_profile':str(self.profile/'absent')})
        for case in bad:
            with self.subTest(case_number=bad.index(case)):
                self.cli('discover',config=case,success=False)
                self.assertEqual(self.server.calls,[])

    def test_partial_delete_failure_holds_then_explicit_sync_event_retry_purges_all(self):
        self.cli('enroll');self.cli('sync')
        edit={'type':'message','subtype':'message_changed','channel':FIXTURE['channel'],
            'event_ts':'1100.000001','message':{'user':'U_ALICE','text':'synthetic edited memory','ts':'1000.000001'}}
        self.event(edit,'edit-'+self.ident)
        external=FIXTURE['channel']+':1000.000001'
        with self.store.open() as state:
            keys=[r[0] for r in state.db.execute('SELECT c.object_key FROM cloud_conversation_segments c JOIN backend_source_revisions r USING(source_id) WHERE r.connection=? AND r.external_id=? ORDER BY r.received,r.source_id',(self.connection,external))]
        objects=self.store.conversation_segments.objects;original=objects.delete;deleted=[]
        def fail_second(key):
            deleted.append(key)
            if len(deleted)==2:raise OSError('synthetic delete outage')
            return original(key)
        deletion={'type':'message','subtype':'message_deleted','channel':FIXTURE['channel'],
            'deleted_ts':'1000.000001','event_ts':'1200.000001'}
        cursor=self.connector.status(self.ctx,self.ident)['cursor']
        with patch.object(objects,'delete',side_effect=fail_second):
            with self.assertRaises(SlackHeld):self.event(deletion,'delete-'+self.ident)
        self.assertEqual(self.connector.status(self.ctx,self.ident)['status'],'held')
        self.assertEqual(self.connector.status(self.ctx,self.ident)['cursor'],cursor)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM backend_slack_events WHERE connector=? AND event_id=?',(self.ident,'delete-'+self.ident)).fetchone()[0],'processing')
        worker,calls=self.worker();self.assertEqual(worker.run(max_seconds=5)['completed'],0);self.assertEqual(calls,[])
        self.cli('sync')
        self.assertEqual(self.event(deletion,'delete-'+self.ident)['disposition'],'deleted')
        for key in keys:
            with self.assertRaises(ObjectMissing):objects.head(key)

    def test_message_lock_serializes_concurrent_edit_with_all_revision_deletion(self):
        self.cli('enroll');self.cli('sync')
        edit={'type':'message','subtype':'message_changed','channel':FIXTURE['channel'],
            'event_ts':'1300.000001','message':{'user':'U_ALICE','text':'concurrent synthetic edit','ts':'1000.000001'}}
        deletion={'type':'message','subtype':'message_deleted','channel':FIXTURE['channel'],
            'deleted_ts':'1000.000001','event_ts':'1200.000001'}
        entered=threading.Event();release=threading.Event();objects=self.store.conversation_segments.objects;original=objects.delete
        def blocked_delete(key):
            entered.set()
            if not release.wait(5):raise RuntimeError('fixture deletion release deadline')
            return original(key)
        with patch.object(objects,'delete',side_effect=blocked_delete),ThreadPoolExecutor(max_workers=2) as pool:
            deleting=pool.submit(self.event,deletion,'delete-'+self.ident)
            self.assertTrue(entered.wait(5))
            editing=pool.submit(self.event,edit,'edit-'+self.ident)
            try:
                time.sleep(.1);self.assertFalse(editing.done())
            finally:release.set()
            self.assertEqual(deleting.result(10)['disposition'],'deleted')
            self.assertEqual(editing.result(10)['disposition'],'deleted')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_source_revisions WHERE connection=? AND external_id=?',(self.connection,FIXTURE['channel']+':1000.000001')).fetchone()[0],1)

    def test_revision_delete_bound_holds_before_any_object_purge(self):
        self.cli('enroll');self.cli('sync')
        # Actual canonical source revisions, without provider calls or mocked SQL.
        from agentclient.enterprise_capture import normalize_capture
        external=FIXTURE['channel']+':1000.000001'
        for revision in range(2,102):
            value=normalize_capture({'hook_event_name':'UserPromptSubmit','event_id':external,
                'session_id':external,'turn_id':external,'revision':str(revision),
                'speaker':'alice','timestamp':'unknown','source_order':revision},
                'enrolled-test',self.connection,source_type='conversation',origin='slack_connector')
            value['blocks']=[{'type':'text','value':'synthetic revision '+str(revision)}]
            self.store.ingest_general(self.ctx,value)
        objects=self.store.conversation_segments.objects
        deletion={'type':'message','subtype':'message_deleted','channel':FIXTURE['channel'],
            'deleted_ts':'1000.000001','event_ts':'1200.000001'}
        with patch.object(objects,'delete',wraps=objects.delete) as deleting:
            with self.assertRaises(SlackHeld):self.event(deletion,'delete-'+self.ident)
            deleting.assert_not_called()
        self.assertEqual(self.connector.status(self.ctx,self.ident)['status'],'held')

    def test_direct_dsn_legacy_mode_is_preserved_and_pg_only(self):
        legacy=self.config.copy();legacy.pop('backend_profile')
        self.cli('enroll',config=legacy);self.assertEqual(self.cli('sync')['upserts'],4)
        self.assertGreater(len(self.sources()),0)
        self.assertEqual(self.counts()['cloud_conversation_segments'],0)


if __name__=='__main__':unittest.main()
