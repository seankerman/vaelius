from datetime import datetime,timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import unittest
import uuid

from agenthub.cloud_recovery import backup,restore_snapshot,reconcile_restore,_encode,_decode
from vaelius_test_support.hub.recovery_rehearsal import rehearse
from vaelius_test_support.hub.cloud_load import provision
from agenthub.source_objects import FileSourceObjects,ObjectCorrupt,ObjectMissing
from agenthub.document_ingest import DocumentStore
from agenthub.enterprise import Denied


class LogicalRecoveryEncodingTests(unittest.TestCase):
    def test_nested_untrusted_marker_and_binary_are_not_confused(self):
        original={'kind':'binary','value':'malicious user content','nested':[b'\x00\xff',Decimal('1.25'),datetime(2026,9,26,tzinfo=timezone.utc)]}
        self.assertEqual(_decode(_encode(original)),original)


@unittest.skipUnless(os.environ.get('CLOUD_LOAD_PARENT_PROFILE'),'explicit isolated logical fixture parent profile required')
class LogicalRecoveryPostgresTests(unittest.TestCase):
    def setUp(self):
        from agenthub.cloud_runtime import CloudStore
        parent=Path(os.environ['CLOUD_LOAD_PARENT_PROFILE'])
        self.root=parent/'recovery-test-profiles'/uuid.uuid4().hex
        self.config=provision(self.root,parent)
        self.source=CloudStore(self.root/'source-state',self.config['tenants']['acme']['dsn'],'acme')
        # Offline restore fixture uses a fresh separately isolated database and
        # same logical tenant, never registered as a second live route/listener.
        self.target=CloudStore(self.root/'target-state',self.config['tenants']['bravo']['dsn'],'acme')
        self.source.create_organization('acme');self.source.create_project('acme','recovery-fixture')
        self.tokens={}
        for actor in ('alice','bob'):
            self.source.create_principal('acme',actor);self.source.set_membership('acme','recovery-fixture',actor,True)
            self.tokens[actor]=self.source.enroll('acme',actor,actor,['ingest','read','source_read','policy','withdraw','correct'])
        self.ctx=self.source.authenticate(self.tokens['alice'])
        self.source.enroll_connection(self.ctx,'recovery-docs','recovery-fixture','recovery-fixture',['document'],visibility='team',reader_ids=['alice','bob'])
        self.source_objects=FileSourceObjects(self.root/'source-objects');self.target_objects=FileSourceObjects(self.root/'target-objects')
        self.documents=DocumentStore(self.source,self.source_objects)
        self.original=self.ingest('guide',b'# Guide\nDataset saved at /synthetic/old.csv because it retains headers.\n')
        self.obsolete=self.ingest('obsolete',b'# Obsolete checklist\nThis old procedure is withdrawn later.\n')
        self.admin_dsn=self.config['tenants']['bravo']['admin_dsn']

    def ingest(self,external,raw,version=1):
        return self.documents.ingest(self.ctx,'recovery-docs',external,str(version),external+'.md',io.BytesIO(raw),title=external)

    def test_current_deltas_objects_and_denials_reconcile_before_readiness(self):
        snapshot=self.root/'before';manifest=backup(self.source,self.source_objects,snapshot)
        self.assertEqual(len(manifest['objects']),2);self.assertEqual(manifest['provider_calls'],0)
        corrected=self.ingest('guide',b'# Guide\nCorrection: dataset saved at /synthetic/current.tsv because TSV preserves headers.\n',2)
        added=self.ingest('new',b'# New document\nThis write happened after backup.\n')
        self.source.lifecycle(self.ctx,{'version':'enterprise-local-1','operation':'delete','target_id':self.obsolete['source_id'],
            'expected_revision':'1','idempotency_key':'delete-obsolete','reason':'synthetic post-backup deletion'})
        self.source.connection_policy(self.ctx,'recovery-docs',reader_ids=['alice'])
        receipt=restore_snapshot(snapshot,self.target,self.target_objects,admin_dsn=self.admin_dsn)
        self.assertFalse(receipt['ready'])
        with self.assertRaisesRegex(ValueError,'restore_requires_reconciliation'):self.target.require_ready()
        reconciled=reconcile_restore(snapshot,self.target,self.source,self.target_objects,self.source_objects,
            admin_dsn=self.admin_dsn,destination=self.root/'latest')
        self.assertTrue(reconciled['ready']);self.assertEqual(reconciled['source_policy_delta_journal_rows'],1)
        self.assertFalse(reconciled['cutover_performed']);self.assertFalse(reconciled['managed_backup_evidence'])
        self.target.require_ready();documents=DocumentStore(self.target,self.target_objects)
        alice=self.target.authenticate(self.tokens['alice']);bob=self.target.authenticate(self.tokens['bob'])
        stream,_=documents.fetch(alice,corrected['source_id'])
        with stream:self.assertIn(b'/synthetic/current.tsv',stream.read())
        stream,_=documents.fetch(alice,added['source_id'])
        with stream:self.assertIn(b'after backup',stream.read())
        with self.assertRaises(Denied):documents.fetch(alice,self.obsolete['source_id'])
        with self.assertRaises(Denied):documents.fetch(bob,corrected['source_id'])

    def test_populated_claim_temporal_and_episode_history_survive_offline_reconcile(self):
        from agentclient.enterprise_contract import VERSION
        from agenthub.processing.episode_pipeline import _record_episode_revision
        from agenthub.processing.temporal import record_assertion
        from agenthub.project_history import project_overview
        event=self.source.ingest(self.ctx,{'version':VERSION,'external_id':'recovery-decision',
            'session':'recovery-chat','turn':'1','project':'recovery-fixture','kind':'Stop',
            'body':'On 2026-09-25 the synthetic dataset moved to /synthetic/decision.tsv because TSV keeps headers. It is still there.',
            'occurred_at':'2026-09-25T12:00:00Z','visibility':'team'})['source_id']
        note=self.source.accept_reviewed_note(self.ctx,event,'Synthetic dataset decision',
            'On 2026-09-25 the synthetic dataset moved to /synthetic/decision.tsv because TSV keeps headers. It is still there.')
        with self.source.open() as state,state.db:
            db=state.db
            scope=db.execute('SELECT internal_project FROM enterprise_sources WHERE id=?',(event,)).fetchone()[0]
            session=db.execute('SELECT session FROM memories WHERE id=?',(event,)).fetchone()[0]
            db.execute('''INSERT INTO knowledge_generations
                (generation_id,curator_version,status,source_policy,config_hash,created)
                VALUES('recovery-populated-fixture','synthetic','active','synthetic','fixed',1)''')
            db.execute('''INSERT INTO curation_episode_jobs
                (id,generation_id,episode_id,project,session,turn,source_ids,source_hash,
                 created,updated,version,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'done')''',
                ('recovery-decision-job','recovery-populated-fixture','recovery-decision-episode',
                 scope,session,'1',json.dumps([event]),'recovery-decision-hash',1,1,'fixture'))
            row={'id':'recovery-decision-job','generation_id':'recovery-populated-fixture',
                'project':scope,'session':session,'turn':'1','source_hash':'recovery-decision-hash',
                'source_ids':json.dumps([event])}
            text='On 2026-09-25 the synthetic dataset moved to /synthetic/decision.tsv because TSV keeps headers. It is still there.'
            _record_episode_revision(db,row,{'episode_disposition':'learning'},
                [{'atom_key':'recovery-decision','record':{'title':'Dataset moved','text':text,
                    'subject':'synthetic dataset','facets':['location','reason'],
                    'actors':['alice'],'state':'adopted','attribution':'source',
                    'occurred_date':'2026-09-25'},
                  'evidence':[{'source_id':event,'version':'1','start':0,'end':len(text),
                               'quote':text,'segment_id':'recovery-decision'}],
                  'operation':'AUTHORED_FIXED'}],[],{},1)
            assertion=record_assertion(db,revision_id=note['revision_id'],
                subject='synthetic dataset',predicate='location',value='/synthetic/decision.tsv',
                actor='alice',evidence_source_ids=[event],
                validity={'from':'2026-09-25','to_status':'ongoing','precision':'day',
                          'timezone':'UTC','basis':'explicit_source'})
        baseline=project_overview(self.source,self.ctx,'recovery-fixture',page_limit=10)
        baseline_occurrences=[item['episode_id'] for item in baseline['episodes']]
        self.assertEqual(len(baseline_occurrences),1)
        snapshot=self.root/'populated-before';backup(self.source,self.source_objects,snapshot)
        self.ingest('post-backup',b'# Later original\nThis version was added after the populated snapshot.\n')
        self.assertFalse(restore_snapshot(snapshot,self.target,self.target_objects,
            admin_dsn=self.admin_dsn)['ready'])
        with self.assertRaises(ValueError):self.target.require_ready()
        reconciled=reconcile_restore(snapshot,self.target,self.source,self.target_objects,
            self.source_objects,admin_dsn=self.admin_dsn,destination=self.root/'populated-latest')
        self.assertTrue(reconciled['ready']);self.target.require_ready()
        restored_ctx=self.target.authenticate(self.tokens['alice'])
        with self.target.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM episode_occurrences').fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_temporal_assertions WHERE assertion_id=?',
                (assertion['assertion_id'],)).fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_dependencies WHERE document_id=?',
                (note['document_id'],)).fetchone()[0],1)
        self.assertEqual(self.target.detail(restored_ctx,note['document_id'])['id'],note['document_id'])
        restored=project_overview(self.target,restored_ctx,'recovery-fixture',page_limit=10)
        self.assertEqual([item['episode_id'] for item in restored['episodes']],baseline_occurrences)

    def test_corrupt_object_restore_stays_held(self):
        snapshot=self.root/'before';manifest=backup(self.source,self.source_objects,snapshot)
        (snapshot/manifest['objects'][0]['file']).write_bytes(b'corrupt original')
        with self.assertRaises(ObjectCorrupt):restore_snapshot(snapshot,self.target,self.target_objects,admin_dsn=self.admin_dsn)
        with self.assertRaises(ValueError):self.target.require_ready()

    def test_missing_post_backup_original_and_target_writes_cannot_be_hidden(self):
        snapshot=self.root/'before';backup(self.source,self.source_objects,snapshot)
        restore_snapshot(snapshot,self.target,self.target_objects,admin_dsn=self.admin_dsn)
        with self.target.open() as state,state.db:
            state.db.execute("INSERT INTO cloud_metrics VALUES('post-restore-write','acme','input_bytes',1,0,'{}')")
        with self.assertRaisesRegex(ValueError,'unpreserved_writes'):
            reconcile_restore(snapshot,self.target,self.source,self.target_objects,self.source_objects,
                admin_dsn=self.admin_dsn,destination=self.root/'latest')
        with self.target.open() as state:
            self.assertEqual(state.db.execute("SELECT amount FROM cloud_metrics WHERE id='post-restore-write'").fetchone()[0],1)
        with self.assertRaises(ValueError):self.target.require_ready()

    def test_missing_latest_source_bytes_prevents_reconciliation(self):
        snapshot=self.root/'before';backup(self.source,self.source_objects,snapshot)
        restore_snapshot(snapshot,self.target,self.target_objects,admin_dsn=self.admin_dsn)
        latest=self.ingest('after-backup',b'# Missing later original\nMust not be acknowledged by recovery.\n')
        with self.source.open() as state:
            key=state.db.execute('SELECT object_key FROM backend_document_versions WHERE source_id=%s',(latest['source_id'],)).fetchone()[0]
        self.source_objects.delete(key)
        with self.assertRaises(ObjectMissing):
            reconcile_restore(snapshot,self.target,self.source,self.target_objects,self.source_objects,
                admin_dsn=self.admin_dsn,destination=self.root/'latest')
        with self.assertRaises(ValueError):self.target.require_ready()

    def test_operator_rehearsal_uses_isolated_fixtures_and_preserves_installed_authority(self):
        profile=self.root/'operator-profile';profile.mkdir(mode=0o700)
        operator=profile/'operator.json'
        operator.write_text(json.dumps({'control_admin_dsn':self.config['tenants']['acme']['admin_dsn']}))
        operator.chmod(0o600)
        receipt=rehearse(profile,self.source,{'provider_mode':'off'})
        for key in ('fixture_only','installed_authority_sources_unchanged','missing_deltas_denied',
                'missing_object_denied','correction_exact','post_backup_source_preserved',
                'deletion_denied','policy_narrowing_denied'):
            self.assertTrue(receipt[key],key)
        self.assertEqual(receipt['provider_calls'],0);self.assertFalse(receipt['managed_backup_evidence'])

    def test_explicit_cli_restore_requires_unregistered_target_and_private_config(self):
        from contextlib import contextmanager
        from agenthub.cloud_recovery import offline_target
        from agenthub.postgres import connect
        from psycopg import sql
        # This synthetic routing table lives in the source fixture solely for
        # CLI admission tests; it contains DSNs, never another knowledge corpus.
        with connect(self.config['tenants']['acme']['admin_dsn']) as db:
            db.execute('CREATE TABLE cloud_tenants(id TEXT PRIMARY KEY,dsn TEXT NOT NULL,active INTEGER NOT NULL)')
            db.execute('INSERT INTO cloud_tenants VALUES(%s,%s,1)',('acme',self.config['tenants']['acme']['dsn']))
            db.execute(sql.SQL('GRANT SELECT ON cloud_tenants TO {}').format(sql.Identifier(self.config['tenants']['acme']['role'])))
        @contextmanager
        def control():
            with connect(self.config['tenants']['acme']['dsn']) as db:yield db
        registry=type('FixtureRegistry',(),{'open_control':staticmethod(control)})()
        path=self.root/'offline-target.json'
        settings={'kind':'offline_restore_target_v1','tenant':'acme','dsn':self.config['tenants']['bravo']['dsn'],
            'admin_dsn':self.admin_dsn,'home':str(self.root/'cli-target'),
            'objects':{'kind':'file','root':str(self.root/'cli-objects')},'serve':False,'dispatch':False}
        path.write_text(json.dumps(settings));path.chmod(0o600)
        target,objects,admin=offline_target(path,registry)
        self.assertEqual(target.tenant_id,'acme');self.assertEqual(admin,self.admin_dsn)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'permissions'):offline_target(path,registry)
        path.chmod(0o600)
        settings['dsn']=self.admin_dsn;path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError,'application_role'):offline_target(path,registry)
        settings['dsn']=self.config['tenants']['bravo']['dsn'];settings['admin_dsn']=self.config['tenants']['acme']['admin_dsn'];path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError,'admin_database_mismatch'):offline_target(path,registry)
        settings['dsn']=self.config['tenants']['acme']['dsn'];settings['admin_dsn']=self.config['tenants']['acme']['admin_dsn']
        path.write_text(json.dumps(settings))
        with self.assertRaisesRegex(ValueError,'registered'):offline_target(path,registry)

    def test_actual_cli_offline_restore_and_current_authority_reconcile(self):
        import subprocess,sys
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from agenthub.postgres import connect
        # Control routing is an isolated metadata schema, outside the canonical
        # public-table snapshot. No knowledge corpus is duplicated there.
        control_schema='cli_control'
        with connect(self.config['tenants']['acme']['admin_dsn']) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(control_schema)))
            db.execute(sql.SQL('CREATE TABLE {}.cloud_tenants(id TEXT PRIMARY KEY,dsn TEXT NOT NULL,active INTEGER NOT NULL)').format(sql.Identifier(control_schema)))
            db.execute(sql.SQL('INSERT INTO {}.cloud_tenants VALUES(%s,%s,1)').format(sql.Identifier(control_schema)),('acme',self.config['tenants']['acme']['dsn']))
            db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(control_schema),sql.Identifier(self.config['tenants']['acme']['role'])))
            db.execute(sql.SQL('GRANT SELECT ON {}.cloud_tenants TO {}').format(sql.Identifier(control_schema),sql.Identifier(self.config['tenants']['acme']['role'])))
        profile=self.root/'cli-profile';profile.mkdir(mode=0o700)
        runtime=profile/'runtime.json'
        runtime.write_text(json.dumps({'provider_mode':'off','control_dsn':make_conninfo(self.config['tenants']['acme']['dsn'],options='-c search_path=cli_control,public'),
            'objects':{'kind':'file','root':str(self.source_objects.root)}}));runtime.chmod(0o600)
        target=profile/'offline.json'
        target.write_text(json.dumps({'kind':'offline_restore_target_v1','tenant':'acme','dsn':self.config['tenants']['bravo']['dsn'],
            'admin_dsn':self.admin_dsn,'home':str(self.target.home),'objects':{'kind':'file','root':str(self.target_objects.root)},'serve':False,'dispatch':False}));target.chmod(0o600)
        snapshot=self.root/'cli-before';backup(self.source,self.source_objects,snapshot)
        arguments=[sys.executable,'-m','agenthub.cloud_recovery','restore','--current-profile',str(profile),
            '--target-config',str(target),'--snapshot',str(snapshot)]
        first=subprocess.run(arguments,capture_output=True,text=True,timeout=30)
        self.assertEqual(first.returncode,0,first.stderr[-1000:]);self.assertFalse(json.loads(first.stdout)['ready'])
        with self.assertRaises(ValueError):self.target.require_ready()
        current=self.ingest('guide',b'# Current CLI original\nDataset saved at /synthetic/cli-current.tsv because headers remain explicit.\n',2)
        arguments[3]='reconcile';arguments+=['--output',str(self.root/'cli-latest')]
        second=subprocess.run(arguments,capture_output=True,text=True,timeout=30)
        self.assertEqual(second.returncode,0,second.stderr[-1000:]);receipt=json.loads(second.stdout)
        self.assertTrue(receipt['ready']);self.assertFalse(receipt['cutover_performed']);self.assertEqual(receipt['provider_calls'],0)
        ctx=self.target.authenticate(self.tokens['alice']);stream,_=DocumentStore(self.target,self.target_objects).fetch(ctx,current['source_id'])
        with stream:self.assertIn(b'/synthetic/cli-current.tsv',stream.read())


if __name__=='__main__':unittest.main()
