import base64
import json
from pathlib import Path
import tempfile
import unittest

from agentclient.capture_outbox import Outbox
from agentclient.enterprise_capture import normalize_capture
from agentclient.general_contract import split_event, validate_event
from vaelius_test_support.fixtures.enterprise import EnterpriseStore
from agenthub.enterprise import Conflict, Denied


class GeneralSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=EnterpriseStore(Path(self.tmp.name)/'hub')
        self.store.create_organization('orchard');self.store.create_principal('orchard','alice')
        self.store.create_project('orchard','maple');self.store.set_membership('orchard','maple','alice',True)
        self.token=self.store.enroll('orchard','alice','device',['ingest','read','source_read','policy','withdraw','correct'])
        self.ctx=self.store.authenticate(self.token)
        self.store.enroll_connection(self.ctx,'agent-one','host-codex','maple',['agent'])
        self.fixture=json.loads((Path(__file__).resolve().parent/'fixtures/processing/local_backend_v2.json').read_text())

    def tearDown(self): self.tmp.cleanup()

    def event(self,index=2): return normalize_capture(self.fixture['events'][index],'maple','agent-one')

    def test_frozen_round_trip_and_no_raw_index(self):
        for original in self.fixture['events']:
            event=normalize_capture(original,'maple','agent-one')
            receipt=None
            for part in reversed(split_event(event)): receipt=self.store.ingest_part(self.ctx,part)
            self.assertTrue(receipt['complete'])
            payload=self.store.general_source(self.ctx,receipt['source_id'])['payload']
            self.assertEqual(payload,event)
            self.assertEqual(self.store.ingest_part(self.ctx,split_event(event)[0])['disposition'],'duplicate')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM memory_fts').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_fts').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_capture_gaps').fetchone()[0],2)

    def test_two_megabytes_duplicate_reordering_and_restart(self):
        event=self.event();event['blocks'][1]['value']={'output':'α😀\\"\n'*230000}
        parts=split_event(event)
        for part in parts[1:]:
            self.assertFalse(self.store.ingest_part(self.ctx,part)['complete'])
        self.store=EnterpriseStore(self.store.home)
        self.assertFalse(self.store.ingest_part(self.ctx,parts[1])['complete'])
        receipt=self.store.ingest_part(self.ctx,parts[0]);self.assertTrue(receipt['complete'])
        self.assertEqual(self.store.general_source(self.ctx,receipt['source_id'])['payload'],event)
        broken=dict(parts[0],data=base64.b64encode(b'changed').decode())
        with self.assertRaises(Conflict):self.store.ingest_part(self.ctx,broken)
        other=dict(parts[0],digest='f'*64)
        with self.assertRaises(Conflict): self.store.ingest_part(self.ctx,other)

    def test_revision_conflict_and_policy_freshness(self):
        event=self.event();first=self.store.ingest_general(self.ctx,event)
        changed=dict(event,blocks=[{'type':'text','value':'changed'}])
        with self.assertRaises(Conflict):self.store.ingest_general(self.ctx,changed)
        changed['revision']='2';self.store.ingest_general(self.ctx,changed)
        with self.assertRaises(Denied):self.store.general_source(self.ctx,first['source_id'])
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE backend_connections SET permission_observed=0')
        with self.assertRaises(Denied):self.store.ingest_general(self.ctx,self.event(3))

    def test_forged_authority_and_unredacted_data_rejected(self):
        for key in ('tenant','visibility','reader_ids','provider_model'):
            with self.assertRaises(ValueError):validate_event(dict(self.event(),**{key:'forged'}))
        event=self.event();event['blocks'][0]['value']['password']='example-not-redacted'
        with self.assertRaises(ValueError):self.store.ingest_general(self.ctx,event)

    def test_stale_permissions_refresh_only_by_enrolled_owner(self):
        with self.store.open() as state,state.db:state.db.execute('UPDATE backend_connections SET permission_observed=0')
        self.assertFalse(self.store.search(self.ctx,{'version':'enterprise-local-1','query':'Maple'})['answerable'])
        self.store.connection_policy(self.ctx,'agent-one')
        self.assertEqual(self.store.ingest_general(self.ctx,self.event())['disposition'],'accepted')

    def test_same_external_ids_remain_tenant_and_namespace_isolated(self):
        from agenthub.cloud_runtime import CloudStore
        from vaelius_test_support.fixtures.postgres import database
        home=Path(self.tmp.name)/'second'
        other=CloudStore(home,database(home),'second')
        other.create_organization('second');other.create_principal('second','alice')
        other.create_project('second','maple');other.set_membership('second','maple','alice',True)
        token=other.enroll('second','alice','device',['ingest','read','source_read'])
        ctx=other.authenticate(token);other.enroll_connection(ctx,'agent-two','host-codex','maple',['agent'])
        first=self.store.ingest_general(self.ctx,self.event())
        event=self.event();event['connection']='agent-two';second=other.ingest_general(ctx,event)
        self.assertNotEqual(first['source_id'],second['source_id'])
        with self.assertRaises(Denied):other.general_source(ctx,first['source_id'])
        with self.assertRaises(Denied):self.store.authenticate(token)

    def test_redacted_outbox_outage_lost_response_and_full(self):
        box=Outbox(Path(self.tmp.name)/'client');event=self.event()
        box.put(event)
        class Transport:
            def __init__(inner):inner.lost=True
            def request(inner,path,value):
                result=self.store.ingest_part(self.ctx,value)
                if inner.lost:inner.lost=False;raise ConnectionError('lost response')
                return result
        transport=Transport()
        self.assertTrue(box.drain(transport)['blocked'])
        box.close();box=Outbox(Path(self.tmp.name)/'client')
        with box.db:box.db.execute('UPDATE pending SET next_attempt=0')
        self.assertEqual(box.drain(transport)['pending_events'],0)
        self.assertEqual(box.put(event),split_event(event)[0]['digest'])
        self.assertEqual(box.status()['pending_events'],0)
        box.close()
        small=Outbox(Path(self.tmp.name)/'full',max_bytes=1)
        with self.assertRaises(ValueError):small.put(event)
        self.assertIn('outbox_full_backpressure',small.status()['gaps']);small.close()

    def test_hook_metadata_gap_is_replayed_to_backend_without_source_content(self):
        from agentclient.hooks import _enterprise_gap
        home=Path(self.tmp.name)/'gap-client';home.mkdir()
        _enterprise_gap(home,{'session':'chat','turn':'turn','project':'maple','kind':'PostToolUse'},'capture_outbox_full')
        box=Outbox(home)
        self.assertEqual(box.queue_diagnostics({'knowledge_backend':{'mode':'enterprise_local',
            'api_version':'cloud-local-1','connection_id':'agent-one','url':'http://127.0.0.1:55486'}}),1)
        class Transport:
            def request(inner,path,value):return self.store.ingest_part(self.ctx,value)
        self.assertEqual(box.drain(Transport())['pending_events'],0);box.close()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_capture_gaps').fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_fts').fetchone()[0],0)

    def test_v2_restore_purges_saved_outputs_and_retains_current_denials(self):
        from agenthub.backend_worker import initialize
        self.store.create_principal('orchard','bob');self.store.set_membership('orchard','maple','bob',True)
        bob_token=self.store.enroll('orchard','bob','bob-device',['read'])
        self.store.enroll_connection(self.ctx,'lifecycle','codex-life','maple',['agent'],visibility='team',reader_ids=['alice','bob'])
        event=self.event(2);event['connection']='lifecycle'
        old=self.store.ingest_general(self.ctx,event)['source_id']
        doc=self.store.accept_reviewed_note(self.ctx,old,'Maple location','Dataset is at /Users/demo/My Data/maple.csv.')
        with self.store.open() as state,state.db:
            db=state.db;initialize(db)
            db.execute('''INSERT INTO curation_episode_jobs(id,generation_id,episode_id,project,session,turn,source_ids,
                source_hash,status,progress,created,updated,version) VALUES('purge-job','g','e','p','s','t',?,'h','done',?,1,1,'fixture')''',
                (json.dumps([old]),json.dumps({'private':'DELETE_CANDIDATE_CANARY'})))
            db.execute("INSERT INTO episode_candidates VALUES('c','purge-job','g','k',?,'{}','applied',?,1)",
                (json.dumps({'private':'DELETE_CANDIDATE_CANARY'}),doc['document_id']))
            db.execute('INSERT INTO episode_candidate_history SELECT *,2 FROM episode_candidates')
            db.execute('''INSERT INTO backend_observers(id,tenant,connection,conversation,internal_project,generation,
                permission_hash,model,prompt_version,provider_session,checkpoint,updated)
                VALUES('o','orchard','lifecycle','s','p','g','h','fixture','fixture','synthetic-session',?,1)''',
                (json.dumps({'private':'DELETE_CANDIDATE_CANARY'}),))
            db.execute("INSERT INTO backend_observer_dependencies VALUES('o',?,1)",(old,))
            db.execute("INSERT INTO backend_processing_dependencies VALUES('purge-job',?,1)",(old,))
            db.execute("INSERT INTO backend_jobs(id,episode_job,observer_id,source_hash,status,pending_result,created,updated) VALUES('bj','purge-job','o','h','done',?,1,1)",
                (json.dumps({'private':'DELETE_CANDIDATE_CANARY'}),))
        from agenthub.cloud_recovery import backup,restore_snapshot,reconcile_restore
        from agenthub.source_objects import FileSourceObjects
        from vaelius_test_support.fixtures.postgres import database
        objects=FileSourceObjects(Path(self.tmp.name)/'objects')
        snapshot=Path(self.tmp.name)/'before';backup(self.store,objects,snapshot)
        # Native revision correction and a current policy denial happen after backup.
        changed=dict(event,revision='2',blocks=[{'type':'text','value':'The Maple dataset is now /tmp/current.csv.'}])
        new=self.store.ingest_general(self.ctx,changed)['source_id']
        self.store.connection_policy(self.ctx,'lifecycle',visibility='private',reader_ids=['alice'])
        self.store.set_membership('orchard','maple','bob',False)
        self.store.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':old,'operation':'delete',
            'expected_revision':'1','idempotency_key':'purge','reason':'owner deletes superseded source'})
        target=Path(self.tmp.name)/'restored';restored=EnterpriseStore(target)
        restored_objects=FileSourceObjects(Path(self.tmp.name)/'restored-objects')
        admin=database(target,admin=True)
        restore_snapshot(snapshot,restored,restored_objects,admin_dsn=admin)
        with self.assertRaisesRegex(ValueError,'restore_requires_reconciliation'):restored.require_ready()
        result=reconcile_restore(snapshot,restored,self.store,restored_objects,objects,
            admin_dsn=admin,destination=Path(self.tmp.name)/'latest')
        self.assertTrue(result['ready']);self.assertFalse(result['cutover_performed'])
        bob=restored.authenticate(bob_token)
        with self.assertRaises(Denied):restored.general_source(bob,new)
        with restored.open() as state:
            db=state.db
            for table,column in (('episode_candidates','candidate_json'),('episode_candidate_history','candidate_json'),
                                 ('curation_episode_jobs','progress'),('backend_observers','checkpoint'),
                                 ('backend_jobs','pending_result')):
                self.assertNotIn('DELETE_CANDIDATE_CANARY',json.dumps([r[0] for r in db.execute(f'SELECT {column} FROM {table}')]))
            self.assertEqual(db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?',(old,)).fetchone()[0],'{}')
            self.assertEqual(db.execute('SELECT visibility FROM backend_connections WHERE id=\'lifecycle\'').fetchone()[0],'private')
            self.assertIsNone(db.execute('SELECT provider_session FROM backend_observers').fetchone()[0])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT active FROM enterprise_sources WHERE id=?',(new,)).fetchone()[0],1)
