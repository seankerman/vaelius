import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from vaelius_test_support.fixtures.enterprise import EnterpriseStore
from agenthub.local_connectors import Connector


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=EnterpriseStore(self.root/'hub')
        self.store.create_organization('orchard')
        for name in ('alice','bob'):self.store.create_principal('orchard',name)
        self.store.create_project('orchard','maple')
        for name in ('alice','bob'):self.store.set_membership('orchard','maple',name,True)
        self.tokens={name:self.store.enroll('orchard',name,name,['ingest','read','source_read','policy','withdraw','correct']) for name in ('alice','bob')}
        self.ctx=self.store.authenticate(self.tokens['alice']);self.connector=Connector(self.store)

    def tearDown(self):self.tmp.cleanup()

    def test_project_names_do_not_replace_authorization(self):
        from agenthub.enterprise import Denied
        import hashlib
        path = Path(__file__).parent / 'fixtures/service/generic_connector_paths.json'
        raw = path.read_bytes()
        manifest = json.loads(path.with_name('generic_connector_paths_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['sha256'])
        for index, case in enumerate(json.loads(raw)['cases']):
            directory = self.root / ''.join(case['name_parts'])
            directory.mkdir()
            (directory / 'guide.md').write_text(case['content'])
            self.connector.enroll(self.ctx, 'generic-' + str(index), 'directory', directory, 'maple')
            self.assertEqual(self.connector.sync(self.ctx, 'generic-' + str(index))['upserts'], 1)
        self.store.set_membership('orchard', 'maple', 'bob', False)
        with self.assertRaises(Denied):
            self.connector.enroll(self.store.authenticate(self.tokens['bob']), 'denied-generic',
                                  'directory', directory, 'maple')

    def test_documents_incremental_permissions_delete_restore(self):
        docs=self.root/'docs';docs.mkdir();file=docs/'guide.md';file.write_text('# Maple importing\nCSV retains a stable header. Dataset: /Users/demo/My Data/maple.csv.\n')
        policy=self.root/'policy.json';policy.write_text(json.dumps({'visibility':'team','reader_ids':['alice','bob']}))
        self.connector.enroll(self.ctx,'docs','directory',docs,'maple',visibility='team',reader_ids=['alice','bob'],policy_file=policy)
        self.assertEqual(self.connector.sync(self.ctx,'docs',dry_run=True)['upserts'],1)
        self.assertEqual(self.connector.sync(self.ctx,'docs')['upserts'],1)
        self.assertEqual(self.connector.sync(self.ctx,'docs')['upserts'],0)
        query={'version':'enterprise-local-1','query':'Maple importing CSV header'}
        self.assertTrue(self.store.search(self.store.authenticate(self.tokens['bob']),query)['answerable'])
        policy.write_text(json.dumps({'visibility':'private','reader_ids':['alice']}))
        self.assertTrue(self.connector.sync(self.ctx,'docs')['policy_changed'])
        self.assertFalse(self.store.search(self.store.authenticate(self.tokens['bob']),query)['answerable'])
        file.unlink();self.assertEqual(self.connector.sync(self.ctx,'docs')['deletions'],1)
        file.write_text('# Maple importing\nCSV retains a stable header. Dataset restored.\n')
        self.assertEqual(self.connector.sync(self.ctx,'docs')['upserts'],1)
        self.assertTrue(self.store.search(self.ctx,query)['answerable'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM model_calls').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM memory_fts').fetchone()[0],0)

    def test_failed_snapshot_does_not_delete_and_symlinks_do_not_escape(self):
        docs=self.root/'docs';docs.mkdir();(docs/'guide.md').write_text('Maple CSV requires a stable header.')
        self.connector.enroll(self.ctx,'docs','directory',docs,'maple')
        self.connector.sync(self.ctx,'docs')
        outside=self.root/'outside.md';outside.write_text('OUTSIDE_CANARY')
        (docs/'escape.md').symlink_to(outside)
        with self.assertRaises(ValueError):self.connector.sync(self.ctx,'docs')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_sources WHERE active=1').fetchone()[0],1)
            self.assertNotIn('OUTSIDE_CANARY',json.dumps([r[0] for r in state.db.execute('SELECT payload FROM backend_source_revisions')]))

    def test_native_registration_failure_cannot_commit_unregistered_document(self):
        docs=self.root/'crash-docs';docs.mkdir()
        (docs/'guide.md').write_text('# Synthetic Maple guide\nThe CSV header stays stable.\n')
        self.connector.enroll(self.ctx,'crash-docs','directory',docs,'maple')
        def fail_registration(*,db=None,document_id=None):
            if db is not None:raise RuntimeError('native_registration_fault')
            return 0
        with patch.object(self.store,'refresh_documents',side_effect=fail_registration):
            try:self.connector.sync(self.ctx,'crash-docs')
            except RuntimeError as error:self.assertEqual(str(error),'native_registration_fault')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],0)

    def test_imported_copy_has_original_lineage_and_is_not_corroboration(self):
        from agentclient.enterprise_capture import normalize_capture
        docs=self.root/'docs';docs.mkdir();text='Maple importer requires a CSV file that retains the stable header.'
        (docs/'guide.md').write_text(text)
        self.connector.enroll(self.ctx,'docs','directory',docs,'maple');self.connector.sync(self.ctx,'docs')
        self.store.enroll_connection(self.ctx,'agent','codex','maple',['agent'])
        copied=self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostToolUse','event_id':'read',
            'session_id':'chat','turn_id':'turn','tool_name':'read_file','tool_response':{'output':text}},'maple','agent'))
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT source_role FROM source_event_metadata WHERE source_id=?',(copied['source_id'],)).fetchone()[0],'context_transfer')
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_source_links WHERE source_id=?',(copied['source_id'],)).fetchone()[0],1)

    def test_records_and_threads_preserve_fields_and_edits(self):
        records=self.root/'records.json';records.write_text(json.dumps({'records':[{'id':'MAP-1','revision':'1','fields':{'status':'Resolved','owner':'alice'},'comments':['Stable CSV header verified.'],'history':[{'status':'Open'},{'status':'Resolved'}]}]}))
        self.connector.enroll(self.ctx,'records','records',records,'maple');self.connector.sync(self.ctx,'records')
        self.assertTrue(self.store.search(self.ctx,{'version':'enterprise-local-1','query':'MAP-1 Resolved'})['answerable'])
        threads=self.root/'threads.json';threads.write_text(json.dumps({'threads':[{'id':'thread','revision':'1','participants':['alice','bob'],'messages':[{'id':'message','revision':'1','role':'user','actor':'alice','text':'Use CSV because it retains a stable header.'}]}]}))
        self.connector.enroll(self.ctx,'threads','conversations',threads,'maple');result=self.connector.sync(self.ctx,'threads')
        self.assertEqual(result['upserts'],2)
        self.assertFalse(self.store.search(self.ctx,{'version':'enterprise-local-1','query':'thread'})['answerable'])
        data=json.loads(threads.read_text());data['threads'][0]['revision']='2';data['threads'][0]['messages'][0].update(revision='2',text='Correction: TSV is required.')
        threads.write_text(json.dumps(data));self.assertEqual(self.connector.sync(self.ctx,'threads')['upserts'],2)

    def test_heading_sections_exact_spans_and_native_conversation_order(self):
        fixture=json.loads((Path(__file__).resolve().parent/'fixtures/processing/backend_semantics_v2.json').read_text())
        docs=self.root/'sections';docs.mkdir();(docs/'guide.md').write_text(fixture['document'])
        self.connector.enroll(self.ctx,'sections','directory',docs,'maple');self.connector.sync(self.ctx,'sections')
        with self.store.open() as state:
            spans=[dict(r) for r in state.db.execute('SELECT start,"end",heading FROM backend_native_spans ORDER BY start')]
            self.assertEqual(spans,fixture['expected_sections'])
            for row in state.db.execute('SELECT s.start,s.end,m.body FROM backend_native_spans s JOIN knowledge_documents d USING(document_id) JOIN memories m ON m.id=d.origin_memory_id'):
                self.assertEqual(row['body'],fixture['document'][row['start']:row['end']])
        threads=self.root/'ordered.json';threads.write_text(json.dumps({'threads':[{
            'id':'ordered','messages':[{'id':'z-first','role':'user','actor':'alice','text':'First choice.'},
                                      {'id':'a-second','role':'assistant','actor':'bob','text':'Second commentary.'}]}]}))
        self.connector.enroll(self.ctx,'ordered','conversations',threads,'maple');self.connector.sync(self.ctx,'ordered')
        with self.store.open() as state:
            events=[json.loads(r[0]) for r in state.db.execute("SELECT payload FROM backend_source_revisions WHERE connection='connector-ordered'")]
        ordered=sorted(events,key=lambda e:e['event']['order'])
        self.assertEqual([e['external_id'] for e in ordered],['ordered:z-first','ordered:a-second','ordered:end'])
        self.assertEqual([e['actor'] for e in ordered[:2]],['alice','bob'])
