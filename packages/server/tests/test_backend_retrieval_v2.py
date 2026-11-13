import tempfile
import unittest
from pathlib import Path
from vaelius_test_support.fixtures.enterprise import EnterpriseStore
from agenthub.enterprise import Denied


class BackendRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=EnterpriseStore(Path(self.tmp.name)/'hub')
        self.store.create_organization('orchard')
        for name in ('alice','bob'):self.store.create_principal('orchard',name)
        self.store.create_project('orchard','maple')
        for name in ('alice','bob'):self.store.set_membership('orchard','maple',name,True)
        self.tokens={name:self.store.enroll('orchard',name,name,['ingest','read','source_read','feedback','curate','correct','policy','withdraw']) for name in ('alice','bob')}
        self.alice=self.store.authenticate(self.tokens['alice']);self.bob=self.store.authenticate(self.tokens['bob'])
        # Curated documents are capped by their sources' sharing (migration 047): opt in.
        for ctx in (self.alice,self.bob):self.store.sharing_setting(ctx,{'max_sharing':'organization'})

    def tearDown(self):self.tmp.cleanup()

    def source(self,id,body,visibility='team'):
        return self.store.ingest(self.alice,{'version':'enterprise-local-1','external_id':id,
            'session':'chat','turn':id,'project':'maple','kind':'UserPromptSubmit',
            'body':body,'visibility':visibility,'occurred_at':'2026-09-25T10:00:00Z'})['source_id']

    def passage(self,key,body):
        """One of alice's chat messages, captured, shared with the workspace and indexed."""
        from agentclient.enterprise_capture import normalize_capture
        from agenthub.source_index import SourceIndex
        with self.store.open() as state:
            enrolled=state.db.execute("SELECT 1 FROM backend_connections WHERE id='chat-capture'").fetchone()
        if not enrolled:
            self.store.enroll_connection(self.alice,'chat-capture','chat-capture','maple',['agent'],visibility='organization')
        event=normalize_capture({'hook_event_name':'Stop','event_id':key,'session_id':'chat','turn_id':key,
            'timestamp':'2026-09-25T10:00:00Z','last_assistant_message':body,'revision':'1'},'maple','chat-capture',
            sharing='organization')
        source=self.store.ingest_general(self.alice,event)['source_id']
        SourceIndex(self.store).run(max_sources=20,max_seconds=20)
        return source

    def test_reviewed_release_preserves_raw_privacy_and_revocation(self):
        # A reviewed release is a derived document, so it is opened by id: search reads
        # captured passages only and returns neither the note nor its release.
        source=self.source('private','A private decision: Maple CSV was chosen because it preserves a stable header.','private')
        doc=self.store.accept_reviewed_note(self.alice,source,'Maple CSV rationale','Maple CSV was chosen because it preserves a stable header.')
        with self.assertRaises(Denied):self.store.detail(self.bob,doc['document_id'])
        released=self.store.reviewed_release(self.alice,doc['document_id'],doc['revision_id'],'maple',['bob'],'release-fixture')
        self.assertIn('stable header',self.store.detail(self.bob,released['document_id'])['claim']['lesson'])
        with self.assertRaises(Denied):self.store.detail(self.bob,doc['document_id'])
        self.assertEqual(self.store.search(self.bob,{'version':'enterprise-local-1','query':'Maple CSV rationale'})['results'],[])
        with self.assertRaises(Denied):self.store.source(self.bob,source)
        self.store.lifecycle(self.alice,{'version':'enterprise-local-1','target_id':source,'operation':'withdraw',
            'expected_revision':'1','idempotency_key':'withdraw','reason':'owner revoked'})
        with self.assertRaises(Denied):self.store.detail(self.bob,released['document_id'])

    def test_missing_topic_abstains_and_context_suppression_restores_after_boundary(self):
        source=self.passage('csv','Maple CSV was chosen because it preserves a stable header for its importer.')
        self.assertEqual(self.store.search(self.bob,{'version':'enterprise-local-1','query':'What is the orbital mass of Jupiter?'})['results'],[])
        query={'version':'enterprise-local-1','query':'Maple CSV header','mode':'automatic','session':'receiver'}
        result=self.store.search(self.bob,query)
        self.assertTrue(result['results']);self.assertFalse(result['answerable'])
        self.store.receipt(self.bob,{'version':'enterprise-local-1','session':'receiver','cards':[{'id':c['id'],'revision':c['revision']} for c in result['results']],'serialized_chars':500})
        self.assertEqual(self.store.search(self.bob,query)['results'],[])
        self.assertTrue(self.store.search(self.bob,dict(query,mode='explicit'))['results'])
        self.store.boundary(self.bob,{'version':'enterprise-local-1','event':{'hook_event_name':'PostCompact','trigger':'auto','session_id':'receiver','turn_id':'compact'}})
        self.assertTrue(self.store.search(self.bob,query)['results'])
