"""Frozen source-first acceptance cases on the canonical PostgreSQL authority."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Denied
from agenthub.postgres import PostgresConnection
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class RawDefaultTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.store.retrieval_corpus='sources'
        self.store.enroll_connection(self.ctx,'raw-test','test','maple',['agent'],visibility='team')
        self.fixture=json.loads((Path(__file__).parent/'fixtures/raw_default_v1.json').read_text())

    def tearDown(self):self.postgres_teardown()

    def ingest(self,index=0,**changes):
        row=dict(self.fixture['messages'][index],**changes)
        event=normalize_capture({'hook_event_name':'UserPromptSubmit' if row['role']=='user' else 'Stop',
            'event_id':row['id'],'session_id':self.fixture['conversation'],'turn_id':row['id'],
            'timestamp':row['time'],'prompt':row['text'],'last_assistant_message':row['text'],
            'revision':row.get('revision','1')},'maple','raw-test')
        return self.store.ingest_general(self.ctx,event)['source_id']

    def drain(self,**kwargs):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config',side_effect=AssertionError('no LLM')):
            return SourceIndex(self.store).run(max_sources=20,max_seconds=20,**kwargs)

    def search(self,ctx=None,**kwargs):
        return self.store.search(ctx or self.ctx,{'version':'enterprise-local-1',
            'query':self.fixture['query'],'project':'maple',**kwargs})

    def test_ingest_queue_resume_and_exact_evidence_without_models(self):
        source=self.ingest();self.assertEqual(self.search()['results'],[])
        self.drain();first=self.search();self.assertTrue(first['results'])
        self.assertFalse(first['answerable'])
        from agenthub.source_evidence import document_evidence
        card=first['results'][0]
        evidence=document_evidence(self.store,self.ctx,card['id'],revision=card['revision'])
        self.assertEqual(evidence['sources'][0]['source_id'],source)
        self.assertIn('/data/drafts/orchard.csv',evidence['sources'][0]['text'])
        self.assertEqual(self.ingest(),source);self.drain()
        self.assertEqual(first['results'],self.search()['results'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_observers').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM episode_candidates').fetchone()[0],0)

    def test_owner_only_even_with_team_connection_and_read_only_credential(self):
        self.ingest();self.drain()
        bob=self.store.authenticate(self.tokens['bob'])
        self.assertEqual(self.search(bob)['results'],[])
        token=self.store.enroll('acme','alice','read-only',['read'])
        self.assertEqual(self.search(self.store.authenticate(token))['results'],[])

    def test_history_cutoff_and_no_invented_supersession(self):
        self.ingest(0);self.ingest(1);self.drain()
        current=self.search();self.assertEqual(len(current['results']),2)
        old=self.search(as_of='2026-09-01',time_mode='effective_at')
        self.assertEqual(len(old['results']),1)
        self.assertIn('/drafts/',old['results'][0]['lesson'])
        absent=self.search(query=self.fixture['unrelated_query'])
        self.assertEqual(absent['results'],[])

    def test_unknown_time_is_readable_context_but_not_dated_evidence(self):
        self.ingest(time='unknown');self.drain();card=self.search()['results'][0]
        from agenthub.source_context import document_context
        result=document_context(self.store,self.ctx,card['id'],revision=card['revision'])
        self.assertTrue(result['sources'])
        self.assertEqual(result['sources'][0]['occurred_at'],'unknown')
        self.assertEqual(self.search(as_of='2026-09-01')['results'],[])

    def test_revision_and_withdrawal_invalidate_search_and_delivery(self):
        old=self.ingest();self.drain();before=self.search()
        new=self.ingest(revision='2',text='Orchard dataset source was revised to /data/final/orchard.csv.')
        self.drain();after=self.search()
        self.assertNotEqual(before['results'][0]['id'],after['results'][0]['id'])
        self.assertEqual(self.store.validate_search_delivery(self.ctx,{},before)['results'],[])
        self.store.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':new,
            'operation':'withdraw','expected_revision':'1','idempotency_key':'raw-withdraw','reason':'fixture'})
        self.assertEqual(self.search()['results'],[])
        with self.assertRaises(Denied):self.store.general_source(self.ctx,old)

    def test_indexed_lexical_entrypoint_reuses_current_owner_and_lifecycle_rules(self):
        from agenthub.search_reader import restrict
        source=self.ingest();self.drain()
        def hits(ctx):
            with self.store.open() as state,restrict(self.store,state,ctx,'maple'):
                return state.db.execute("SELECT * FROM search_lexical_matches(?)",('Orchard',)).fetchall()
        self.assertEqual(len(hits(self.ctx)),1)
        self.assertEqual(hits(self.store.authenticate(self.tokens['bob'])),[])
        with self.store.open() as state,restrict(self.store,state,self.ctx,'maple'):
            state.db.execute("SELECT set_config('agenthub.search_context','',true)")
            self.assertEqual(state.db.execute("SELECT * FROM search_lexical_matches(?)",('Orchard',)).fetchall(),[])
        self.store.lifecycle(self.ctx,{'version':'enterprise-local-1','target_id':source,
            'operation':'withdraw','expected_revision':'1','idempotency_key':'lexical-withdraw','reason':'fixture'})
        self.assertEqual(hits(self.ctx),[])

    def test_source_delivery_checks_are_constant_for_one_and_many_cards(self):
        self.ingest(text=('Orchard dataset evidence. '*300));self.drain()
        cards=self.store.search(self.ctx,{'version':'enterprise-local-1','query':self.fixture['query']},candidate_pool=True)['results']
        self.assertGreater(len(cards),1)
        counts=[];execute=PostgresConnection.execute
        for selected in (cards[:1],cards):
            sql=[]
            def counted(db,query,params=None):
                sql.append(query);return execute(db,query,params)
            with patch.object(PostgresConnection,'execute',counted):
                self.store.validate_search_delivery(self.ctx,{},dict(results=selected,answerable=False))
            counts.append(len(sql))
        self.assertEqual(counts[0],counts[1])

    def test_enrichment_is_disabled_by_runtime_default(self):
        from agenthub.backend_worker import Worker
        worker=Worker.__new__(Worker);worker.store=self.store
        with patch.object(worker,'prepare',side_effect=AssertionError('must stay idle')):
            self.assertEqual(worker.run()['calls'],0)

    def test_vector_index_is_incremental_and_raw_denial_applies_to_both_channels(self):
        from agenthub.cloud_maintenance import IndexFreshness
        from test_hybrid_rank_reuse import FixedEmbedding
        self.store.semantic_embedder=FixedEmbedding();self.store.hybrid_enabled=True
        index=IndexFreshness(self.store);index.ensure_generation()
        self.ingest();self.drain()
        self.assertEqual(index.run_once(self.store.semantic_embedder)['status'],'searchable')
        rows=self.store.candidates(self.ctx,'Orchard dataset',project='maple',vector=True)
        self.assertEqual(set(rows[0]['channels']),{'lexical','vector'})
        self.assertEqual(self.search(self.store.authenticate(self.tokens['bob']))['results'],[])

    def test_pending_vector_from_retired_generation_is_requeued_to_current(self):
        from agenthub.cloud_maintenance import IndexFreshness
        from test_hybrid_rank_reuse import FixedEmbedding
        index=IndexFreshness(self.store);index.ensure_generation();self.ingest();self.drain()
        with self.store.open() as state,state.db:
            row=state.db.execute("SELECT id,payload FROM outbox WHERE id LIKE 'cloud-vector:v1:%'").fetchone()
            payload=json.loads(row['payload']);current=payload['generation_id'];payload['generation_id']='retired-generation'
            state.db.execute('UPDATE outbox SET payload=? WHERE id=?',(json.dumps(payload,sort_keys=True),row['id']))
        self.assertEqual(index.run_once(FixedEmbedding())['status'],'pending')
        with self.store.open() as state:
            fresh=json.loads(state.db.execute('SELECT payload FROM outbox WHERE id=?',(row['id'],)).fetchone()[0])
        self.assertEqual(fresh['generation_id'],current)
        self.assertEqual(index.run_once(FixedEmbedding())['status'],'searchable')

    def test_retired_job_with_existing_current_vector_finishes_without_embedding(self):
        from agenthub.cloud_maintenance import IndexFreshness
        from test_hybrid_rank_reuse import FixedEmbedding
        index=IndexFreshness(self.store);index.ensure_generation();self.ingest();self.drain()
        self.assertEqual(index.run_once(FixedEmbedding())['status'],'searchable')
        with self.store.open() as state,state.db:
            row=state.db.execute("SELECT id,payload FROM outbox WHERE id LIKE 'cloud-vector:v1:%'").fetchone()
            payload=json.loads(row['payload']);payload['generation_id']='retired-generation'
            state.db.execute("UPDATE outbox SET payload=?,status='pending' WHERE id=?",(json.dumps(payload,sort_keys=True),row['id']))
        with patch.object(FixedEmbedding,'embed_documents',side_effect=AssertionError('already indexed')):
            index.run_once(FixedEmbedding())
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM outbox WHERE id=?',(row['id'],)).fetchone()[0],'searchable')

    def test_redaction_media_gap_and_policy_change_preserve_boundaries(self):
        from agenthub.source_index import SourceIndex
        source=self.ingest(text=self.fixture['privacy_text']+' Orchard dataset '+
            'data:image/png;base64,'+'A'*4000)
        self.drain()
        with self.store.open() as state:
            texts=[r[0] for r in state.db.execute('SELECT body FROM knowledge_fts')]
        self.assertNotIn('private-fixture-secret',' '.join(texts))
        # The existing private cleaner retains permitted people/locations;
        # owner-only raw access, not an invented PII policy, protects them.
        self.assertIn('alice@example.test',' '.join(texts))
        self.assertNotIn('A'*100,' '.join(texts))
        before=self.search()
        self.store.connection_policy(self.ctx,'raw-test',visibility='private',reader_ids=['alice'])
        self.assertEqual(self.store.validate_search_delivery(self.ctx,{},before)['results'],[])
        self.drain();self.assertTrue(self.search()['results'])
        self.assertEqual(SourceIndex(self.store).reconcile()['queued'],0)

    def test_long_source_resume_and_full_exact_evidence_paging(self):
        text='Orchard dataset 日本語 😀 '+('exact evidence paragraph. '*230)
        source=self.ingest(text=text)
        self.drain(max_passages=1);self.drain()
        from agenthub.source_evidence import document_evidence
        rows=self.store.candidates(self.ctx,'Orchard dataset',project='maple',vector=False,limit=20)
        for row in rows:
            page=document_evidence(self.store,self.ctx,row['document_id'],revision=row['revision_id'])
            pieces=list(page['sources']);offset=page['next_offset']
            while offset is not None:
                page=document_evidence(self.store,self.ctx,row['document_id'],revision=row['revision_id'],offset=offset)
                self.assertLessEqual(len(json.dumps(page)),4000)
                pieces.extend(page['sources']);offset=page['next_offset']
            self.assertEqual(''.join(p['text'] for p in pieces),json.loads(row['claim_json'])['lesson'])
            self.assertTrue(all(p['source_id']==source for p in pieces))

    def test_mcp_search_fetch_and_default_paging_use_canonical_service(self):
        from agenthub.mcp_tools import MemoryTools
        from agenthub.source_evidence import document_evidence
        self.ingest(text='Orchard dataset '+('日本語 😀 evidence '*150));self.drain()
        store=self.store;ctx=self.ctx
        class Backend:
            def request(self,path,value=None):
                if path.endswith('/search'):return store.search(ctx,value)
                if path.endswith('/document-evidence'):
                    return document_evidence(store,ctx,value['id'],revision=value['revision'],offset=value['offset'])
                if '/documents/' in path:return store.detail(ctx,path.rsplit('/',1)[1])
                raise AssertionError(path)
        client=MemoryTools('maple',Backend())
        result=client.enterprise_call('search_memory',{'query':'Orchard dataset'})
        self.assertFalse(result['answerable']);self.assertEqual(result['support'],'partial')
        card=result['records'][0]
        page=client.enterprise_call('fetch_memory',{'id':card['id'],'revision':card['revision']})
        self.assertTrue(page['sources']);self.assertIsNotNone(page['next_offset'])
        following=client.enterprise_call('fetch_memory',{'id':card['id'],'revision':card['revision'],
            'source_offset':page['next_offset']})
        self.assertGreater(following['sources'][0]['start'],page['sources'][0]['start'])

    def test_optional_ranker_keeps_long_raw_evidence_discoverable(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        self.ingest(text='Orchard dataset '+('long supporting evidence. '*80));self.drain()
        store=self.store
        class Registry:
            def store_for_token(self,token):return store
        class Ranker:
            def rerank(self,query,cards,**kwargs):
                return {'results':cards[:1],'answerable':True,'support':'complete',
                    'reranking':{'status':'returned'}}
        store.serving_reranker=Ranker()
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver']))
        response=client.post('/enterprise/v3/search',headers={'Authorization':'Bearer '+self.tokens['alice']},
            json={'version':'enterprise-local-1','query':'Orchard dataset','project':'maple'})
        self.assertEqual(response.status_code,200)
        result=response.json();self.assertTrue(result['results'])
        self.assertFalse(result['answerable']);self.assertEqual(result['support'],'partial')
        self.assertTrue(result['results'][0]['excerpt_truncated'])


if __name__=='__main__':unittest.main()
