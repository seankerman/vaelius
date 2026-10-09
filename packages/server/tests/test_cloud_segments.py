"""Frozen synthetic conversation segments on the canonical PostgreSQL authority."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.general_contract import canonical,split_event,validate_event
from agenthub.conversation_segments import ConversationSegments
from agenthub.enterprise import Denied
from agenthub.source_objects import FileSourceObjects,ObjectMissing,ObjectCorrupt
from test_cloud_postgres import PostgresFixture,SERVICES

FIXTURE_PATH=Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_conversation_segments_v1.json'
FIXTURE=json.loads(FIXTURE_PATH.read_text())


class SegmentFixtureTests(unittest.TestCase):
    def test_fixture_frozen_before_objectization(self):
        manifest=json.loads(FIXTURE_PATH.with_name('cloud_conversation_segments_v1_manifest.json').read_text())
        self.assertTrue(manifest['frozen_before_implementation'])
        self.assertEqual(hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),manifest['sha256'])
        for event in FIXTURE['events']:validate_event(event)


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class SegmentPostgresTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        from agenthub.cloud_runtime import CloudStore
        self.store=CloudStore(Path(self.temp.name)/'hub',self.dsn,'acme')
        self.objects=FileSourceObjects(Path(self.temp.name)/'segments')
        self.segments=ConversationSegments(self.store,self.objects)
        self.store.conversation_segments=self.segments
        self.store.enroll_connection(self.ctx,'segment-agent','codex','maple',['agent','conversation'],
            visibility='team',reader_ids=['alice','bob'])

    def tearDown(self):self.postgres_teardown()

    def event(self,index=0):return copy.deepcopy(FIXTURE['events'][index])

    def source(self,index=0):return self.store.ingest_general(self.ctx,self.event(index))['source_id']

    def locator(self,ident):
        with self.store.open() as state:
            return dict(state.db.execute('SELECT * FROM cloud_conversation_segments WHERE source_id=?',(ident,)).fetchone())

    def test_exact_structured_multi_segment_turn_and_multipart_retry(self):
        ids=[]
        for value in FIXTURE['events']:
            for part in reversed(split_event(value)):receipt=self.store.ingest_part(self.ctx,part)
            self.assertTrue(receipt['complete']);ids.append(receipt['source_id'])
            self.assertEqual(self.segments.fetch(self.ctx,receipt['source_id'])['payload'],value)
            self.assertEqual(self.store.general_source(self.ctx,receipt['source_id'])['payload'],value)
            locator=self.locator(receipt['source_id'])
            with self.objects.open(locator['object_key']) as source:self.assertEqual(source.read(),canonical(value))
            self.assertEqual(self.store.ingest_part(self.ctx,split_event(value)[0])['disposition'],'duplicate')
        self.assertEqual(len(list(self.objects.root.iterdir())),2)
        with self.store.open() as state:
            self.segments.verify_sources(state.db,ids)
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_segment_uploads WHERE status=?',('accepted',)).fetchone()[0],2)
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_fts').fetchone()[0],0)

    def test_segments_use_actual_sdk_loopback_moto(self):
        from moto_test_fixture import sdk_objects
        with sdk_objects('synthetic-segments') as objects:
            self.store.conversation_segments=ConversationSegments(self.store,objects)
            for value in FIXTURE['events']:
                result=self.store.ingest_general(self.ctx,value)
                self.assertEqual(self.store.general_source(self.ctx,result['source_id'])['payload'],value)
                self.assertEqual(self.store.ingest_general(self.ctx,value)['disposition'],'duplicate')
            self.assertEqual(len(objects.client.list_objects_v2(Bucket=objects.bucket)['Contents']),2)

    def test_object_failure_rolls_back_source_and_not_searchable(self):
        with patch.object(self.objects,'put',side_effect=OSError('synthetic unavailable object endpoint')):
            with self.assertRaises(OSError):self.source()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_source_revisions').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_conversation_segments').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT status FROM cloud_segment_uploads').fetchone()[0],'failed')
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_metrics').fetchone()[0],0)
        self.assertFalse(self.store.search(self.ctx,{'version':'enterprise-local-1','query':'maple.csv'})['answerable'])

    def test_db_failure_after_upload_keeps_reservation_then_retry_reuses_object(self):
        with patch.object(self.store,'_audit',side_effect=RuntimeError('synthetic commit failure after upload')):
            with self.assertRaises(RuntimeError):self.source()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0],0)
            row=dict(state.db.execute('SELECT * FROM cloud_segment_uploads').fetchone())
            self.assertEqual(row['status'],'uploaded')
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_metrics').fetchone()[0],0)
        self.assertEqual(len(list(self.objects.root.iterdir())),1)
        with patch.object(self.objects,'put',wraps=self.objects.put) as put:
            ident=self.source();self.assertEqual(put.call_count,0)
            self.assertEqual(self.store.ingest_general(self.ctx,self.event())['disposition'],'duplicate')
        self.assertEqual(self.locator(ident)['object_key'],row['object_key'])

    def test_corrupt_missing_objects_deny_cache_read_duplicate_and_processing(self):
        ident=self.source();locator=self.locator(ident)
        self.objects._path(locator['object_key']).write_bytes(b'corrupt')
        for operation in (lambda:self.segments.fetch(self.ctx,ident),
                          lambda:self.store.general_source(self.ctx,ident),
                          lambda:self.store.ingest_general(self.ctx,self.event())):
            with self.assertRaises(ObjectCorrupt):operation()
        self.objects.delete(locator['object_key'])
        with self.assertRaises(ObjectMissing):self.store.general_source(self.ctx,ident)
        with self.store.open() as state:
            with self.assertRaises(ObjectMissing):self.segments.verify_sources(state.db,[ident])
        # A completed transport retry must verify the retained original too.
        self.store.ingest_general(self.ctx,self.event(1))
        part=split_event(self.event(1))[0];self.store.ingest_part(self.ctx,part)
        second=self.store.ingest_general(self.ctx,self.event(1))['source_id']
        self.objects.delete(self.locator(second)['object_key'])
        with self.assertRaises(ObjectMissing):self.store.ingest_part(self.ctx,part)

    def test_current_auth_cross_tenant_supersession_and_withdrawal(self):
        ident=self.source();bob=self.store.authenticate(self.tokens['bob'])
        self.assertEqual(self.segments.fetch(self.ctx,ident)['payload'],self.event())
        # Shared derived knowledge does not grant access to private raw traces.
        for ctx in (bob,bob|{'tenant':'bravo'},self.store.authenticate(self.tokens['admin'])):
            with self.assertRaises(Denied):self.segments.fetch(ctx,ident)
        self.store.connection_policy(self.ctx,'segment-agent',reader_ids=['alice'])
        with self.assertRaises(Denied):self.segments.fetch(bob,ident)
        changed=self.event();changed['revision']='2';changed['blocks'][1]['value']['path']='/synthetic/current.csv'
        current=self.store.ingest_general(self.ctx,changed)['source_id']
        with self.assertRaises(Denied):self.segments.fetch(self.ctx,ident)
        self.store.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':current,'expected_revision':'1',
            'operation':'withdraw','idempotency_key':'segment-withdraw','reason':'synthetic withdrawal'})
        with self.assertRaises(Denied):self.segments.fetch(self.ctx,current)

    def test_existing_eight_mib_event_ceiling_before_object_write(self):
        from agentclient.general_contract import EVENT_BYTES
        value=self.event();value['blocks']=[{'type':'text','value':'x'*EVENT_BYTES}]
        with patch.object(self.objects,'put',wraps=self.objects.put) as put:
            with self.assertRaisesRegex(ValueError,'logical_event_too_large'):
                self.store.ingest_general(self.ctx,value)
            self.assertEqual(put.call_count,0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_segment_uploads').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0],0)

    def test_explicit_existing_backfill_and_grace_cleanup_boundaries(self):
        del self.store.conversation_segments
        ident=self.source();self.store.conversation_segments=self.segments
        with self.store.open() as state:
            with self.assertRaisesRegex(ValueError,'segment_original_required'):self.segments.verify_sources(state.db,[ident])
        result=self.segments.retain_existing(self.ctx,[ident]);self.assertEqual(result['retained'],1)
        self.assertEqual(self.segments.retain_existing(self.ctx,[ident])['retained'],1)
        bob=self.store.authenticate(self.tokens['bob'])
        with self.assertRaises(Denied):self.segments.retain_existing(bob,[ident])
        with self.store.open() as state:upload=state.db.execute('SELECT id FROM cloud_segment_uploads').fetchone()[0]
        with self.assertRaisesRegex(ValueError,'segment_upload_referenced'):self.segments.cleanup(upload)
        failed=self.event(1)
        with patch.object(self.store,'_audit',side_effect=RuntimeError('synthetic transaction abort')):
            with self.assertRaises(RuntimeError):self.store.ingest_general(self.ctx,failed)
        with self.store.open() as state:
            orphan=dict(state.db.execute("SELECT * FROM cloud_segment_uploads WHERE status='uploaded'").fetchone())
        with self.assertRaisesRegex(ValueError,'segment_upload_recent'):self.segments.cleanup(orphan['id'])
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE cloud_segment_uploads SET updated=0 WHERE id=?',(orphan['id'],))
        self.assertEqual(self.segments.cleanup(orphan['id'])['status'],'removed')
        with self.assertRaises(ObjectMissing):self.objects.open(orphan['object_key'])

    def test_missing_accepted_original_requires_explicit_owner_repair(self):
        manifest=FIXTURE_PATH.with_name('cloud_segment_repair_v1.json')
        pin=json.loads(manifest.with_name('cloud_segment_repair_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(),pin['sha256'])
        ident=self.source();key=self.locator(ident)['object_key'];self.objects.delete(key)
        with self.assertRaises(ObjectMissing):self.segments.retain_existing(self.ctx,[ident])
        with self.assertRaises(Denied):
            self.segments.retain_existing(self.store.authenticate(self.tokens['bob']),[ident],repair_missing=True)
        self.assertEqual(self.segments.retain_existing(self.ctx,[ident],repair_missing=True)['retained'],1)
        self.assertEqual(self.segments.fetch(self.ctx,ident)['payload'],self.event())
        self.objects._path(key).write_bytes(b'corrupt')
        with self.assertRaises(ObjectCorrupt):self.segments.retain_existing(self.ctx,[ident],repair_missing=True)

    def test_explicit_old_projection_backfill_checks_original_digest_and_redaction(self):
        del self.store.conversation_segments
        ident=self.source();self.store.conversation_segments=self.segments
        altered=self.event();altered['blocks'][1]['value']['path']='/synthetic/tampered.csv'
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE backend_source_revisions SET payload=? WHERE source_id=?',(canonical(altered).decode(),ident))
        with self.assertRaises(ObjectCorrupt):self.segments.retain_existing(self.ctx,[ident],repair_missing=True)
        self.assertEqual(list(self.objects.root.iterdir()),[])

    def test_atomic_accepted_segment_metrics_are_stable_across_retry_revision_backfill(self):
        p=FIXTURE_PATH.with_name('cloud_segment_metrics_v1.json')
        pin=json.loads(p.with_name('cloud_segment_metrics_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),pin['sha256'])
        first=self.event();ident=self.source()
        self.store.ingest_general(self.ctx,first)
        self.segments.retain_existing(self.ctx,[ident])
        changed=self.event();changed['revision']='2';changed['blocks'][1]['value']['path']='/synthetic/current.csv'
        second=self.store.ingest_general(self.ctx,changed)['source_id']
        del self.store.conversation_segments
        old=self.source(1);self.store.conversation_segments=self.segments
        self.segments.retain_existing(self.ctx,[old]);self.segments.retain_existing(self.ctx,[old])
        with self.store.open() as state:
            rows=[dict(row) for row in state.db.execute('SELECT * FROM cloud_metrics ORDER BY id')]
        self.assertEqual(len(rows),6)
        self.assertEqual(sum(r['amount'] for r in rows if r['kind']=='source_revisions'),3)
        self.assertEqual(sum(r['amount'] for r in rows if r['kind']=='input_bytes'),
            len(canonical(first))+len(canonical(changed))+len(canonical(self.event(1))))
        self.assertTrue(all(json.loads(r['details'])=={'stage':'capture','status':'accepted'} for r in rows))
        from agenthub.cloud_ops import Meter
        totals=Meter(self.store).status()['metrics']
        self.assertEqual(totals['source_revisions'],3)

    def test_explicit_deletion_purges_current_segment_object(self):
        ident=self.source();key=self.locator(ident)['object_key']
        self.store.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':ident,'expected_revision':'1',
            'operation':'delete','idempotency_key':'segment-delete','reason':'synthetic deletion'})
        with self.assertRaises(Denied):self.segments.fetch(self.ctx,ident)
        with self.assertRaises(ObjectMissing):self.objects.open(key)
        self.assertEqual(self.locator(ident)['status'],'removed')


@unittest.skipUnless(os.environ.get('CLOUD_LOAD_PARENT_PROFILE'),'explicit local isolated recovery profile required')
class SegmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        import uuid
        from vaelius_test_support.hub.cloud_load import provision
        from agenthub.cloud_runtime import CloudStore
        parent=Path(os.environ['CLOUD_LOAD_PARENT_PROFILE'])
        self.root=parent/'segment-recovery-tests'/uuid.uuid4().hex
        self.config=provision(self.root,parent)
        self.source=CloudStore(self.root/'source-state',self.config['tenants']['acme']['dsn'],'acme')
        self.target=CloudStore(self.root/'target-state',self.config['tenants']['bravo']['dsn'],'acme')
        self.objects=FileSourceObjects(self.root/'source-objects');self.restored_objects=FileSourceObjects(self.root/'target-objects')
        self.source.conversation_segments=ConversationSegments(self.source,self.objects)
        self.target.conversation_segments=ConversationSegments(self.target,self.restored_objects)
        self.source.create_organization('acme');self.source.create_principal('acme','alice')
        self.source.create_project('acme','maple');self.source.set_membership('acme','maple','alice',True)
        self.token=self.source.enroll('acme','alice','device',['ingest','read','source_read','policy','withdraw','correct'])
        self.ctx=self.source.authenticate(self.token)
        self.source.enroll_connection(self.ctx,'segment-agent','codex','maple',['agent'],visibility='private')
        self.ids=[self.source.ingest_general(self.ctx,value)['source_id'] for value in FIXTURE['events']]

    def test_matching_segment_manifest_offline_restore_and_current_deletion_reconcile(self):
        from agenthub.cloud_recovery import backup,restore_snapshot,reconcile_restore
        snapshot=self.root/'snapshot';manifest=backup(self.source,self.objects,snapshot)
        self.assertEqual(len(manifest['objects']),2)
        self.assertEqual({x['source_id'] for x in manifest['objects']},set(self.ids))
        self.assertTrue(all(x['kind']=='cloud_conversation_segments' for x in manifest['objects']))
        restore_snapshot(snapshot,self.target,self.restored_objects,admin_dsn=self.config['tenants']['bravo']['admin_dsn'])
        with self.assertRaisesRegex(ValueError,'restore_requires_reconciliation'):self.target.require_ready()
        self.source.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':self.ids[0],'expected_revision':'1',
            'operation':'delete','idempotency_key':'delete-post-backup-segment','reason':'synthetic deletion'})
        reconciled=reconcile_restore(snapshot,self.target,self.source,self.restored_objects,self.objects,
            admin_dsn=self.config['tenants']['bravo']['admin_dsn'],destination=self.root/'current')
        self.assertTrue(reconciled['ready']);self.target.require_ready()
        current=json.loads((self.root/'current/manifest.json').read_text())
        self.assertEqual(len(current['objects']),1)
        ctx=self.target.authenticate(self.token)
        with self.assertRaises(Denied):self.target.general_source(ctx,self.ids[0])
        self.assertEqual(self.target.general_source(ctx,self.ids[1])['payload'],FIXTURE['events'][1])
        with self.target.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM cloud_conversation_segments WHERE source_id=?',(self.ids[0],)).fetchone()[0],'removed')

    def test_missing_segment_refuses_backup_or_usable_restore(self):
        from agenthub.cloud_recovery import backup,restore_snapshot
        snapshot=self.root/'snapshot';manifest=backup(self.source,self.objects,snapshot)
        first=manifest['objects'][0]
        (snapshot/first['file']).unlink()
        with self.assertRaises(FileNotFoundError):
            restore_snapshot(snapshot,self.target,self.restored_objects,admin_dsn=self.config['tenants']['bravo']['admin_dsn'])
        with self.assertRaises(ValueError):self.target.require_ready()
        self.objects.delete(first['key'])
        with self.assertRaises(ObjectMissing):backup(self.source,self.objects,self.root/'missing-backup')
