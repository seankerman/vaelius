import tempfile
import unittest
from pathlib import Path
from agenthub.processing.knowledge import apply_resolved_observation
from agenthub.processing.temporal import record_assertion
from vaelius_test_support.fixtures.enterprise import EnterpriseStore
from agenthub.enterprise import Denied


class BackendRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=EnterpriseStore(Path(self.tmp.name)/'hub')
        self.store.create_organization('orchard')
        for name in ('alice','bob'):self.store.create_principal('orchard',name)
        self.store.create_project('orchard','maple')
        for name in ('alice','bob'):self.store.set_membership('orchard','maple',name,True)
        self.tokens={name:self.store.enroll('orchard',name,name,['ingest','read','source_read','correct','policy','withdraw']) for name in ('alice','bob')}
        self.alice=self.store.authenticate(self.tokens['alice']);self.bob=self.store.authenticate(self.tokens['bob'])

    def tearDown(self):self.tmp.cleanup()

    def source(self,id,body,visibility='team'):
        return self.store.ingest(self.alice,{'version':'enterprise-local-1','external_id':id,
            'session':'chat','turn':id,'project':'maple','kind':'UserPromptSubmit',
            'body':body,'visibility':visibility,'occurred_at':'2026-09-25T10:00:00Z'})['source_id']

    def test_historical_current_authorization_and_unknown_interval_abstention(self):
        source=self.source('old','Maple dataset was /tmp/old.csv from 2026-09-01T00:00:00Z to 2026-09-20T00:00:00Z.')
        doc=self.store.accept_reviewed_note(self.alice,source,'Maple dataset location','Maple dataset was at /tmp/old.csv.')
        with self.store.open() as state:
            recorded=record_assertion(state.db,revision_id=doc['revision_id'],subject='Maple dataset',predicate='location',value='/tmp/old.csv',actor='alice',
                evidence_source_ids=[source],validity={'from':'2026-09-01T00:00:00Z','to':'2026-09-20T00:00:00Z','to_status':'bounded','precision':'instant','timezone':'UTC','basis':'explicit_source'})
            state.db.commit()
        request={'version':'enterprise-local-1','query':'Where was the Maple dataset as of 2026-09-10?'}
        result=self.store.search(self.bob,request)
        self.assertTrue(result['answerable']);self.assertIn('/tmp/old.csv',result['results'][0]['lesson'])
        self.assertFalse(self.store.search(self.bob,dict(request,query='Where is the Maple dataset now?'))['answerable'])
        self.store.set_membership('orchard','maple','bob',False)
        self.assertFalse(self.store.search(self.bob,request)['answerable'])
        with self.assertRaises(Denied):self.store.detail(self.bob,recorded['assertion_id'])

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

    def test_current_typed_validity_checks_are_bounded_by_ranked_candidates(self):
        from unittest.mock import patch
        from agenthub.postgres import PostgresConnection
        source=self.source('bounded','Maple dataset was at /tmp/old.csv from 2026-09-01T00:00:00Z to 2026-09-20T00:00:00Z.')
        doc=self.store.accept_reviewed_note(self.alice,source,'Maple dataset','Maple dataset was at /tmp/old.csv.')
        def assertion(document,source,subject):
            with self.store.open() as state,state.db:
                record_assertion(state.db,revision_id=document['revision_id'],subject=subject,predicate='location',
                    value='/tmp/old.csv',actor='alice',evidence_source_ids=[source],
                    validity={'from':'2026-09-01T00:00:00Z','to':'2026-09-20T00:00:00Z',
                        'to_status':'bounded','precision':'instant','timezone':'UTC','basis':'explicit_source'})
        assertion(doc,source,'Maple dataset')
        request={'version':'enterprise-local-1','query':'Where is the Maple dataset now?','mode':'explicit'}
        execute=PostgresConnection.execute
        def measured():
            statements=[]
            def track(db,sql,params=None):statements.append(sql);return execute(db,sql,params)
            with patch.object(PostgresConnection,'execute',track):result=self.store.search(self.bob,request)
            self.assertFalse(result['answerable'])
            return len(statements)
        before=measured()
        for n in range(8):
            body=f'Juniper archive {n} was stored at /tmp/old.csv from 2026-09-01T00:00:00Z to 2026-09-20T00:00:00Z.'
            source=self.source('noise-'+str(n),body)
            other=self.store.accept_reviewed_note(self.alice,source,'Juniper archive '+str(n),body)
            assertion(other,source,'Juniper archive '+str(n))
        after=measured()
        self.assertLessEqual(after,before+2,{'one_temporal_document':before,'nine_temporal_documents':after})
