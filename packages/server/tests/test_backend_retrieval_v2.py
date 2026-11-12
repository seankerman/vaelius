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

    def test_reviewed_release_preserves_raw_privacy_and_revocation(self):
        source=self.source('private','A private decision: Maple CSV was chosen because it preserves a stable header.','private')
        doc=self.store.accept_reviewed_note(self.alice,source,'Maple CSV rationale','Maple CSV was chosen because it preserves a stable header.')
        self.assertFalse(self.store.search(self.bob,{'version':'enterprise-local-1','query':'Maple CSV rationale'})['answerable'])
        released=self.store.reviewed_release(self.alice,doc['document_id'],doc['revision_id'],'maple',['bob'],'release-fixture')
        self.assertTrue(self.store.search(self.bob,{'version':'enterprise-local-1','query':'Maple CSV rationale'})['answerable'])
        with self.assertRaises(Denied):self.store.source(self.bob,source)
        self.store.lifecycle(self.alice,{'version':'enterprise-local-1','target_id':source,'operation':'withdraw',
            'expected_revision':'1','idempotency_key':'withdraw','reason':'owner revoked'})
        self.assertFalse(self.store.search(self.bob,{'version':'enterprise-local-1','query':'Maple CSV rationale'})['answerable'])
        with self.assertRaises(Denied):self.store.detail(self.bob,released['document_id'])

    def test_missing_topic_abstains_and_context_suppression_restores_after_boundary(self):
        source=self.source('csv','Maple CSV was chosen because it preserves a stable header for its importer.')
        self.store.accept_reviewed_note(self.alice,source,'Maple CSV rationale','Maple CSV was chosen because it preserves a stable header for its importer.')
        self.assertFalse(self.store.search(self.bob,{'version':'enterprise-local-1','query':'What is the orbital mass of Jupiter?'})['answerable'])
        query={'version':'enterprise-local-1','query':'Maple CSV rationale','mode':'automatic','session':'receiver'}
        result=self.store.search(self.bob,query)
        self.store.receipt(self.bob,{'version':'enterprise-local-1','session':'receiver','cards':[{'id':c['id'],'revision':c['revision']} for c in result['results']],'serialized_chars':500})
        self.assertFalse(self.store.search(self.bob,query)['answerable'])
        self.assertTrue(self.store.search(self.bob,dict(query,mode='explicit'))['answerable'])
        self.store.boundary(self.bob,{'version':'enterprise-local-1','event':{'hook_event_name':'PostCompact','trigger':'auto','session_id':'receiver','turn_id':'compact'}})
        self.assertTrue(self.store.search(self.bob,query)['answerable'])
