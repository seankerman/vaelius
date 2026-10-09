"""Frozen provider-free maintenance cases; destructive tests use disposable DBs."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agenthub.cloud_maintenance import Maintenance,private_input,retire
from agenthub.cloud_ops import Meter
from agenthub.enterprise import Denied
from agenthub.source_objects import FileSourceObjects,object_key,ObjectMissing
from test_cloud_postgres import PostgresFixture,SERVICES


def config():
    return {'paused':False,'knowledge_backend':{'mode':'enterprise_local'},
        'observer':{'enabled':True,'min_interval_seconds':0,'max_calls_per_day':10000},
        'episode_curation':{'enabled':True,'policy':'durable_memory','generation_id':'maintenance-fixture','settle_seconds':0}}


class FrozenMaintenanceTests(unittest.TestCase):
    def test_frozen_fixture_and_private_inputs(self):
        root=Path(__file__).resolve().parent.parent/'tests/fixtures/service'
        raw=(root/'cloud_maintenance_v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'34da18bdecc30abdc0db44fecbeba1fca421ebf7566a073329bf3cdbc8d1af8c')
        self.assertTrue(json.loads((root/'cloud_maintenance_v1_manifest.json').read_text())['frozen_before_behavior'])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'input.json';path.write_text('{}');path.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'operator_input_permissions'):private_input(path)
            path.chmod(0o600);self.assertEqual(private_input(path),{})


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class MaintenanceTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store.curation_enabled=True
        self.maintenance=Maintenance(self.store,config())

    def tearDown(self):self.postgres_teardown()

    def _source(self,external='dataset'):
        return self.store.ingest(self.ctx,{'version':'enterprise-local-1','external_id':external,
            'session':'chat','turn':'1','project':'maple','kind':'Stop','body':'The synthetic dataset is saved at /tmp/maple.csv.',
            'occurred_at':12345,'visibility':'team'})['source_id']

    def _jobs(self):
        from agentclient.enterprise_capture import normalize_capture
        self.store.enroll_connection(self.ctx,'maintenance-agent','codex','maple',['agent'])
        for turn in ('selected','unselected'):
            for kind in ('UserPromptSubmit','Stop'):
                self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':kind,'event_id':kind+turn,
                    'session_id':'maintenance-chat','turn_id':turn,'prompt':'A useful reason because headers stay stable.',
                    'last_assistant_message':'Acknowledged.'},'maple','maintenance-agent'))
        self.maintenance.worker.prepare()
        with self.store.open() as state:return [r[0] for r in state.db.execute('SELECT id FROM backend_jobs ORDER BY id')]

    def test_hold_fences_running_claim_and_retry_is_explicit_and_model_free(self):
        jobs=self._jobs();self.assertEqual(len(jobs),2)
        claimed=self.maintenance.worker.claim()
        self.assertEqual(self.maintenance.hold([claimed['id']])['held'],1)
        with self.store.open() as state:
            with self.assertRaisesRegex(ValueError,'stale_worker_fence'):self.maintenance.worker.guard(state.db,claimed)
            untouched=state.db.execute('SELECT status FROM backend_jobs WHERE id<>?',(claimed['id'],)).fetchone()[0]
            self.assertEqual(untouched,'pending')
            self.assertEqual(state.db.execute('SELECT count(*) FROM model_call_details').fetchone()[0],0)
        self.assertEqual(self.maintenance.retry(claimed['id'])['provider_calls'],0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs WHERE id=?',(claimed['id'],)).fetchone()[0],'pending')
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_worker_receipts').fetchone()[0],0)
        with self.assertRaisesRegex(ValueError,'explicit_bounded_jobs_required'):self.maintenance.hold([])

    def test_unknown_usage_reconciles_one_attempt_and_admission_is_backend_owned(self):
        meter=Meter(self.store);meter.reserve('uncertain-attempt','job','curation');meter.dispatched('uncertain-attempt')
        meter.finish('uncertain-attempt','uncertain')
        value={'attempt_id':'uncertain-attempt','usage':{'input_tokens':32,'cached_input_tokens':16,'output_tokens':4},
            'result_digest':hashlib.sha256(b'synthetic provider result').hexdigest()}
        self.assertTrue(self.maintenance.reconcile(value)['reconciled'])
        self.assertTrue(self.maintenance.reconcile(value)['duplicate'])
        self.assertEqual(meter.status()['attempts'],1)
        self.assertEqual(meter.status()['reported_tokens']['cached_input_tokens'],16)
        self.maintenance.admission({'max_parallel':2,'max_attempts':10,'enabled':False})
        from agenthub.cloud_ops import AdmissionExhausted
        with self.assertRaises(AdmissionExhausted):meter.reserve('next','job','curation')

    def test_actual_metric_sum_is_an_integer_json_counter(self):
        path=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_usage_json_v1.json'
        raw=path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),"8c8dc8e28e9d4d8dd90fe1e1e9abb41717e01b0442d7b89fcc01192cf7c43f26")
        case=json.loads(raw);metric=case['metric'];meter=Meter(self.store)
        meter.metric(metric['id'],metric['kind'],metric['amount'])
        status=meter.status()
        self.assertEqual(json.loads(json.dumps(status))['metrics'],case['expected_integer_metrics'])
        self.assertIs(type(status['metrics'][metric['kind']]),int)

    def test_authenticated_correction_then_withdraw_and_delete_keep_canonical_boundaries(self):
        source=self._source();document=self.store.accept_reviewed_note(self.ctx,source,'Dataset location',
            'The synthetic dataset is saved at /tmp/maple.csv.')['document_id']
        payload={'version':'enterprise-local-1','target_id':source,'expected_revision':'1','idempotency_key':'correction',
            'reason':'synthetic owner correction','operation':'correct','replacement':{
                'source':{'version':'enterprise-local-1','external_id':'current-dataset','session':'chat','turn':'1','project':'maple',
                    'kind':'Stop','body':'The synthetic dataset is now at /tmp/current-maple.csv.','occurred_at':12346,'visibility':'team'},
                'title':'Dataset location','lesson':'The synthetic dataset is now at /tmp/current-maple.csv.'}}
        bob=self.store.authenticate(self.tokens['bob'])
        with self.assertRaises(Denied):self.maintenance.lifecycle(bob,payload)
        corrected=self.maintenance.lifecycle(self.ctx,payload)
        self.assertIn('current-maple.csv',self.store.detail(self.ctx,document)['claim']['lesson'])
        self.assertNotIn('current-maple.csv',json.dumps(corrected))
        new=corrected['replacement_source_id']
        self.maintenance.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':new,'expected_revision':'1',
            'idempotency_key':'withdraw','reason':'synthetic withdrawal','operation':'withdraw'})
        with self.assertRaises(Denied):self.store.detail(self.ctx,document)
        self.maintenance.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':new,'expected_revision':'2',
            'idempotency_key':'delete','reason':'synthetic retention','operation':'delete'})
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT body FROM memories WHERE id=?',(new,)).fetchone()[0],'')
        policy_source=self._source('policy-source')
        policy={'version':'enterprise-local-1','target_id':policy_source,'expected_revision':'1',
            'idempotency_key':'policy','reason':'synthetic narrower visibility','operation':'policy','visibility':'private'}
        self.assertEqual(self.maintenance.lifecycle(self.ctx,policy)['source_version'],2)
        with self.assertRaises(Denied):self.maintenance.lifecycle(self.ctx,{**policy,'expected_revision':'2',
            'idempotency_key':'widen','visibility':'team'})

    def test_connection_enroll_stale_refresh_narrow_policy_disable_and_status(self):
        self.maintenance.connection(self.ctx,'enroll','maintenance-agent',{'namespace':'codex','project':'maple','source_types':['agent'],
            'visibility':'team','reader_ids':['alice','bob']})
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE backend_connections SET permission_observed=0 WHERE id=?',('maintenance-agent',))
        self.assertTrue(self.maintenance.connection(self.ctx,'status','maintenance-agent')['stale'])
        self.assertFalse(self.maintenance.connection(self.ctx,'refresh','maintenance-agent')['invalidated'])
        self.maintenance.connection(self.ctx,'policy','maintenance-agent',{'reader_ids':['alice']})
        with self.assertRaises(Denied):self.maintenance.connection(self.ctx,'policy','maintenance-agent',{'reader_ids':['alice','bob']})
        self.maintenance.connection(self.ctx,'disable','maintenance-agent')
        self.assertFalse(self.maintenance.connection(self.ctx,'status','maintenance-agent')['active'])


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class DisposableRetirementTests(unittest.TestCase):
    def test_actual_export_and_retirement_require_bound_database_preserve_backups_and_disable_first(self):
        from agenthub.cloud_profile import setup_profile
        from agenthub.cloud_runtime import registry_from_settings
        from agenthub.postgres import connect
        from psycopg import sql
        services=json.loads(Path(SERVICES).read_text());namespace='test_maintenance_'+uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'profile';setup_profile(profile,SERVICES,namespace=namespace)
            operator=private_input(profile/'operator.json');registry=registry_from_settings(private_input(profile/'runtime.json'),profile/'runtime-state')
            objects=FileSourceObjects(Path(temp)/'objects');store=registry.resolve('acme')
            raw=b'A synthetic original document';sha=hashlib.sha256(raw).hexdigest();key=object_key('acme','source','1',sha)
            objects.put(key,io.BytesIO(raw))
            with store.open() as state,state.db:
                state.db.execute("INSERT INTO backend_object_uploads(id,tenant,connection,external_id,version,object_key,sha256,byte_length,status,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ('pending','acme','captured-agent','pending','1',key,sha,len(raw),'uploading',1))
            snapshot=profile/'retained-backup';maintenance=Maintenance(store,config())
            try:
                exported=maintenance.export(objects,snapshot)
                self.assertEqual(exported['tenant'],'acme');self.assertTrue((snapshot/'manifest.json').is_file())
                with self.assertRaisesRegex(ValueError,'expected_tenant_database_mismatch'):
                    retire(profile,'acme',expected_database=namespace+'_bravo',objects=objects,apply=True)
                preview=retire(profile,'acme',expected_database=namespace+'_acme',objects=objects)
                self.assertEqual(preview['status'],'preview');registry.require_active('acme')
                # An object outage leaves the route disabled and the database
                # intact; the operator can resume with the same explicit binding.
                with patch.object(objects,'delete',side_effect=ObjectMissing('synthetic outage')):
                    with self.assertRaises(ObjectMissing):
                        retire(profile,'acme',expected_database=namespace+'_acme',objects=objects,apply=True)
                with self.assertRaises(Denied):registry.require_active('acme')
                with connect(services['admin_dsn']) as db:
                    self.assertTrue(db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(namespace+'_acme',)).fetchone())
                deleted=[];original=objects.delete
                def checked_delete(value):
                    with self.assertRaises(Denied):registry.require_active('acme')
                    deleted.append(value);original(value)
                with patch.object(objects,'delete',side_effect=checked_delete):
                    result=retire(profile,'acme',expected_database=namespace+'_acme',objects=objects,apply=True)
                self.assertEqual(result['status'],'retired');self.assertEqual(deleted,[key])
                self.assertTrue((snapshot/'manifest.json').exists())
                with self.assertRaises(ObjectMissing):objects.head(key)
                with self.assertRaises(Denied):registry.store_for_token((profile/'credentials/acme-alice.token').read_text().strip())
                registry.require_active('bravo')
                with connect(services['admin_dsn']) as db:
                    self.assertFalse(db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(namespace+'_acme',)).fetchone())
                self.assertEqual(len(list((profile/'maintenance-receipts').glob('*.json'))),4)
            finally:
                with connect(services['admin_dsn'],autocommit=True) as db:
                    for name in (namespace+'_acme',namespace+'_bravo',namespace+'_control'):
                        db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
                    for role in (namespace+'_acme_app',namespace+'_bravo_app',namespace+'_router'):
                        db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


if __name__=='__main__':unittest.main()
