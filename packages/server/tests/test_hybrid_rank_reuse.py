"""Real PostgreSQL guardrails for the shared production/evaluation ranker."""
import unittest
from unittest.mock import patch
from test_cloud_retrieval import HybridPolicyTests
from agenthub.cloud_retrieval import rank_scoped_candidates
from agenthub.postgres import PostgresConnection
from agenthub.processing.semantic import MODEL_KEY


class FixedEmbedding:
    model_key=MODEL_KEY
    def embed_documents(self,texts):return [[1.0]+[0.0]*511 for _ in texts]
    def embed_queries(self,texts):return self.embed_documents(texts)


class SharedRankTests(HybridPolicyTests):
    def test_vector_and_lexical_current_scope_and_bounded_queries(self):
        self.store.semantic_embedder=FixedEmbedding()
        self.store.reindex_vectors()
        ctx=self.store.authenticate(self.tokens['bob'])
        statements=[];original=PostgresConnection.execute
        def counted(db,sql,params=None):
            statements.append(sql);return original(db,sql,params)
        with patch.object(PostgresConnection,'execute',counted):
            candidates=self.store.candidates(ctx,'dataset',project='maple',vector=True)
        self.assertTrue(candidates)
        self.assertTrue(all(set(r['channels'])=={'lexical','vector'} for r in candidates))
        with self.store.open() as state:
            allowed={doc for _,doc in self.documents if self.store._document_allowed(state.db,ctx,doc)}
        self.assertEqual({r['document_id'] for r in candidates},allowed)
        self.assertLess(len(statements),35)
        source,doc=self.documents[-1]
        from agentclient.enterprise_contract import VERSION
        self.store.lifecycle(self.ctx,{'version':VERSION,'target_id':source,'expected_revision':'1',
            'operation':'withdraw','idempotency_key':'rank-reuse-withdraw','reason':'fixture'})
        self.assertNotIn(doc,{r['document_id'] for r in self.store.candidates(ctx,'dataset',project='maple',vector=True)})

    def test_rank_sql_count_independent_of_passage_count(self):
        self.store.semantic_embedder=FixedEmbedding();self.store.reindex_vectors()
        with self.store.open() as state:
            statements=[];original=PostgresConnection.execute
            def counted(db,sql,params=None):
                statements.append(sql);return original(db,sql,params)
            with patch.object(PostgresConnection,'execute',counted):
                rank_scoped_candidates(state.db,'dataset',ranked_where='FALSE',embedder=FixedEmbedding())
            # One transaction-local HNSW setting statement is added regardless
            # of scope/cardinality; no per-document permission queries.
            self.assertEqual(len(statements),4)
            self.assertEqual(sum('hnsw.iterative_scan' in sql for sql in statements),1)
            statements.clear()
            with patch.object(PostgresConnection,'execute',counted):
                result=rank_scoped_candidates(state.db,'dataset',ranked_where='TRUE',embedder=FixedEmbedding())
            self.assertEqual(len(statements),4);self.assertEqual(len(result),3)
            self.assertEqual(sum('hnsw.iterative_scan' in sql for sql in statements),1)
