"""Synthetic outcome reporting: exact revision, current ACL, no truth promotion."""
import unittest
from unittest.mock import patch
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from agenthub.feedback import record_feedback
from agenthub.enterprise import Denied, Conflict
from agenthub.postgres import PostgresConnection
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES,'explicit disposable PostgreSQL schemas required')
class FeedbackTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.ctx=self.store.authenticate(self.tokens['alice'])
        self.source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'feedback-source','session':'fixture','turn':'1','project':'maple','kind':'Stop','body':'Cedar export is saved in /synthetic/cedar.csv.','occurred_at':100,'visibility':'private'})['source_id']
        self.doc=self.store.accept_reviewed_note(self.ctx,self.source,'Cedar export','Cedar export is saved in /synthetic/cedar.csv.')['document_id']
        with self.store.open() as state:self.revision=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(self.doc,)).fetchone()[0]
        self.value={'id':self.doc,'revision':self.revision,'request_key':'review-1','outcome':'helpful'}

    def tearDown(self):self.postgres_teardown()

    def test_idempotent_self_report_does_not_change_claim_or_assert_observed_use(self):
        one=record_feedback(self.store,self.ctx,self.value);two=record_feedback(self.store,self.ctx,self.value)
        self.assertEqual(one,two);self.assertFalse(one['independently_verified'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM memory_feedback_reports').fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(self.doc,)).fetchone()[0],self.revision)
            self.assertEqual(state.db.execute("SELECT count(*) FROM enterprise_receipts WHERE status='observed_used'").fetchone()[0],0)
        with self.assertRaises(Conflict):record_feedback(self.store,self.ctx,dict(self.value,outcome='incorrect'))

    def test_stale_revision_and_wrong_actor_denied(self):
        with self.assertRaises(Conflict):record_feedback(self.store,self.ctx,dict(self.value,revision='old'))
        with self.assertRaises(Denied):record_feedback(self.store,self.store.authenticate(self.tokens['bob']),self.value)
        with self.store.open() as state,state.db:state.db.execute('UPDATE enterprise_principals SET active=0 WHERE id=?',('alice',))
        with self.assertRaises(Denied):record_feedback(self.store,self.ctx,self.value)

    def test_current_withdrawal_denies_repeated_report(self):
        record_feedback(self.store,self.ctx,self.value)
        with self.store.open() as state,state.db:state.db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?',(self.source,))
        with self.assertRaises(Denied):record_feedback(self.store,self.ctx,self.value)

    def test_unknown_or_fabricated_verification_rejected(self):
        with self.assertRaises(ValueError):record_feedback(self.store,self.ctx,dict(self.value,independently_verified=True))
        with self.assertRaises(ValueError):record_feedback(self.store,self.ctx,dict(self.value,outcome='verified_success'))

    def test_sql_count_does_not_grow_with_unrelated_sources(self):
        original=PostgresConnection.execute
        def count(key):
            calls=[]
            def counted(db,sql,params=None):calls.append(sql);return original(db,sql,params)
            with patch.object(PostgresConnection,'execute',counted):record_feedback(self.store,self.ctx,dict(self.value,request_key=key))
            return len(calls)
        before=count('before')
        with self.store.open() as state,state.db:
            state.db.execute("INSERT INTO memories(id,project,session,kind,body,created,active) SELECT 'feedback-noise-'||n::text,project,session,kind,body,created,active FROM memories CROSS JOIN generate_series(1,1000) n WHERE id=?",(self.source,))
        self.assertEqual(before,count('after'))

    def test_installed_mcp_to_http_to_postgres_delivery_and_feedback(self):
        import json
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        store=self.store
        class Registry:
            def store_for_token(self,token):return store
        headers={'Authorization':'Bearer '+self.tokens['alice'],
            'Accept':'application/json, text/event-stream',
            'MCP-Protocol-Version':'2025-11-25','X-AgentNetwork-Project':'maple'}
        with TestClient(create_app(Registry(),allowed_hosts=['testserver'])) as client:
            init=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize',
                'params':{'protocolVersion':'2025-11-25','capabilities':{},
                    'clientInfo':{'name':'test','version':'1'}}})
            self.assertEqual(init.status_code,200,init.text)
            results=[]
            for number,(name,args) in enumerate([
                ('search_memory',{'query':'Where is the Cedar export?'}),
                ('fetch_memory',{'id':self.doc,'revision':self.revision,'include_sources':False}),
                ('report_memory_outcome',self.value)],2):
                response=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':number,
                    'method':'tools/call','params':{'name':name,'arguments':args}})
                self.assertEqual(response.status_code,200,response.text)
                result=response.json()['result'];self.assertFalse(result.get('isError'),result)
                results.append(json.loads(result['content'][0]['text']))
            self.assertTrue(results[0]['records'])
            self.assertFalse(results[-1]['independently_verified'])
        with store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM memory_feedback_reports').fetchone()[0],1)
