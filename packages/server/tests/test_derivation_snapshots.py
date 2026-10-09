"""Exact pre-dispatch input versions; conservative legacy graphs stay untouched."""
import json,unittest
from unittest.mock import patch
from agentclient.enterprise_contract import VERSION
from agentclient.enterprise_capture import normalize_capture
from agenthub.backend_worker import Worker
from agenthub.postgres import PostgresConnection
from agenthub.processing.episode_pipeline import snapshot_model_inputs
from test_cloud_postgres import PostgresFixture,SERVICES

@unittest.skipUnless(SERVICES,'explicit disposable PostgreSQL required')
class DerivationSnapshots(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.addCleanup(self.postgres_teardown)
        self.store.curation_enabled=True  # Pre-dispatch snapshot tests opt into enrichment.
        self.store.enroll_connection(self.ctx,'agent','codex','maple',['agent'])
        for kind in ('UserPromptSubmit','Stop'):
            self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':kind,'event_id':kind,
                'session_id':'fixture','turn_id':'1','prompt':'Keep a stable header.',
                'last_assistant_message':'Retain a stable header.'},'maple','agent'))
        cfg={'paused':False,'knowledge_backend':{'mode':'enterprise_local'},
            'observer':{'enabled':True,'model':'fixture','min_interval_seconds':0},
            'episode_curation':{'enabled':True,'policy':'durable_memory','settle_seconds':0,'generation_id':'snapshots'}}
        Worker(self.store,cfg,live=False).prepare()
        with self.store.open() as state:self.job=state.db.execute('SELECT episode_job FROM backend_jobs').fetchone()[0]

    def test_batch_reads_are_constant_and_inputs_do_not_expand_after_dispatch(self):
        artifacts=[]
        for i in range(40):
            source=self.store.ingest(self.ctx,dict(version=VERSION,external_id=str(i),session='notes',turn=str(i),
                project='maple',kind='Stop',body=f'The artifact {i} is located in /synthetic/report-{i}.csv.',
                visibility='private',occurred_at=100+i))['source_id']
            doc=self.store.accept_reviewed_note(self.ctx,source,f'Artifact {i}',f'The artifact {i} is located in /synthetic/report-{i}.csv.')['document_id']
            with self.store.open() as state:revision=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(doc,)).fetchone()[0]
            artifacts.append({'artifact_id':doc,'revision':revision})
        counts=[];original=PostgresConnection.execute
        for subset in (artifacts[:1],artifacts):
            calls=[]
            def counted(db,sql,params=None):calls.append(sql);return original(db,sql,params)
            with self.store.open() as state,state.db,patch.object(PostgresConnection,'execute',counted):
                snapshot_model_inputs(state.db,self.job,subset)
            counts.append(len(calls))
        self.assertEqual(counts,[2,2])
        with self.store.open() as state,state.db:
            before=state.db.execute('SELECT source_ids FROM backend_model_input_snapshots WHERE episode_job=? AND document_id=?',(self.job,artifacts[0]['artifact_id'])).fetchone()[0]
            extra=state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(artifacts[1]['artifact_id'],)).fetchone()[0]
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)',(artifacts[0]['artifact_id'],extra))
            snapshot_model_inputs(state.db,self.job,artifacts[:1])
            after=state.db.execute('SELECT source_ids FROM backend_model_input_snapshots WHERE episode_job=? AND document_id=?',(self.job,artifacts[0]['artifact_id'])).fetchone()[0]
            self.assertEqual(before,after);self.assertNotIn(extra,json.loads(after))
            with self.assertRaisesRegex(ValueError,'context_changed'):
                snapshot_model_inputs(state.db,self.job,[dict(artifacts[0],revision='wrong-revision')])
