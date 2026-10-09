"""Installed PostgreSQL context path; invented sources only."""
import json
from unittest.mock import patch
from agentclient.enterprise_contract import VERSION
from agenthub.source_context import document_context
from agenthub.postgres import PostgresConnection
from agenthub.enterprise import Denied
from test_cloud_retrieval import HybridPolicyTests


class SourceContextTests(HybridPolicyTests):
    def test_parent_related_context_current_permissions_and_query_bound(self):
        ctx=self.store.authenticate(self.tokens['alice'])
        source,doc=self.documents[0]
        with self.store.open() as st:
            rev=st.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(doc,)).fetchone()[0]
            body=st.db.execute('SELECT body FROM memories WHERE id=?',(source,)).fetchone()[0]
            from agenthub.processing.episode_curator import _spans
            with st.db:
                st.db.execute('DELETE FROM knowledge_support WHERE revision_id=?',(rev,))
                st.db.execute("INSERT INTO knowledge_support VALUES(?,?,?,'supports','unknown',0)",
                    (rev,source,_spans(source,body)[0]['span_id']))
        before=[];execute=PostgresConnection.execute
        def baseline_count(db,sql,params=None):
            before.append(sql);return execute(db,sql,params)
        with patch.object(PostgresConnection,'execute',baseline_count):
            document_context(self.store,ctx,doc,revision=rev,query='dataset')
        for n in range(20):
            self.store.ingest(ctx,{'version':VERSION,'external_id':'later'+str(n),
                'session':'old','turn':str(n+2),'project':'maple','kind':'Stop',
                'body':'Dataset review: previous path is experimental, result pending.',
                'visibility':'private','occurred_at':101+n})
        calls=[];original=PostgresConnection.execute
        def counted(db,sql,params=None):
            calls.append(sql);return original(db,sql,params)
        with patch.object(PostgresConnection,'execute',counted):
            result=document_context(self.store,ctx,doc,revision=rev,query='dataset')
        self.assertLess(len(calls),45)
        self.assertLessEqual(len(calls),len(before))
        self.assertLessEqual(len(json.dumps(result)),4000)
        self.assertIn(source,{s['source_id'] for s in result['sources']})
        self.assertNotIn(self.documents[1][0],{s['source_id'] for s in result['sources']})
        self.assertTrue(any(s['context_relation']=='related_later_message' for s in result['sources']))
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        store=self.store
        class Registry:
            def store_for_token(self,token):return store
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver']))
        headers={'Authorization':'Bearer '+self.tokens['alice']}
        data={'id':doc,'revision':rev,'query':'dataset'}
        response=client.post('/enterprise/v3/document-context',headers=headers,json=data)
        self.assertEqual(response.status_code,200);self.assertTrue(response.json()['sources'])
        self.assertEqual(client.post('/enterprise/v3/document-context',headers=headers,json=dict(data,offset=-1)).status_code,400)
        self.assertEqual(client.post('/enterprise/v3/document-context',headers=headers,json=dict(data,revision='old')).status_code,404)
        from agenthub.mcp_tools import MemoryTools
        class Transport:
            def request(self,path,value=None):
                response=client.post(path,headers=headers,json=value) if value is not None else client.get(path,headers=headers)
                response.raise_for_status();return response.json()
        detail=MemoryTools('maple',Transport()).call('fetch_memory',
            {'id':doc,'revision':rev,'query':'dataset','include_context':True})
        self.assertEqual(detail,result)
        bob=self.store.authenticate(self.tokens['bob'])
        with self.assertRaises((PermissionError,Denied)):
            document_context(self.store,bob,doc,revision=rev)
        self.store.lifecycle(ctx,{'version':VERSION,'target_id':source,'expected_revision':'1',
            'operation':'withdraw','idempotency_key':'context-withdraw','reason':'fixture'})
        with self.assertRaises((PermissionError,Denied)):
            document_context(self.store,ctx,doc,revision=rev,offset=result['next_offset'] or 0)


    def test_context_index_rollback_preserves_prior_schema(self):
        from agenthub.postgres import connect
        with connect(self.admin_dsn) as connection:
            connection.execute('SAVEPOINT context_rollback')
            connection.execute('DROP INDEX memories_conversation_context')
            connection.execute('DROP INDEX memories_turn_context')
            self.assertIsNone(connection.execute('SELECT to_regclass(%s)',(self.schema+'.memories_turn_context',)).fetchone()[0])
            connection.execute('ROLLBACK TO SAVEPOINT context_rollback')
            self.assertIsNotNone(connection.execute('SELECT to_regclass(%s)',(self.schema+'.memories_turn_context',)).fetchone()[0])
