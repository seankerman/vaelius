"""Frozen ANN controls: geometry/ACL recall, revision integrity and bounded SQL.

Synthetic vectors test index mechanics, not embedding quality. Source permissions
come from normal ingestion and canonical dependency triggers in a disposable DB.
"""
import json
import os
from pathlib import Path
import random
import statistics
import time
import unittest
from unittest.mock import patch

from agentclient.enterprise_contract import VERSION
from agenthub.cloud_retrieval import _vector
from agenthub.postgres import PostgresConnection, connect
from agenthub.processing.semantic import MODEL_KEY
from test_cloud_postgres import PostgresFixture, SERVICES


class GeometryEmbedder:
    def embed_queries(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL required')
class VectorIndexTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        try:
            self.postgres_setup()
        except Exception:
            if hasattr(self, 'schema'):
                self.postgres_teardown()
            raise
        self.addCleanup(self.postgres_teardown)
        self.store.semantic_embedder = GeometryEmbedder()
        self.seeds = {}
        for actor in ('alice', 'bob'):
            ctx = self.store.authenticate(self.tokens[actor])
            body = actor + ' keeps a synthetic geometry report.'
            source = self.store.ingest(ctx, dict(version=VERSION, external_id=actor,
                session='geometry', turn='1', project='maple', kind='Stop', body=body,
                visibility='private', occurred_at=100))['source_id']
            document = self.store.accept_reviewed_note(ctx, source, actor + ' geometry', body)['document_id']
            self.seeds[actor] = (source, document)

    def populate(self, count):
        with self.store.open() as state, state.db:
            for actor, (_, document) in self.seeds.items():
                # 1% private Alice records among closer unauthorized Bob records.
                condition = 'n % 100 = 0' if actor == 'alice' else 'n % 100 <> 0'
                suffix = ' FROM knowledge_documents CROSS JOIN generate_series(1,?) n WHERE document_id=? AND ' + condition
                state.db.execute('''INSERT INTO knowledge_documents
                    SELECT 'geometry-'||n, project, owner_session,'geometry-origin-'||n,lifecycle,
                    'geometry-revision-'||n,schema_version,created,updated''' + suffix, (count, document))
                state.db.execute('''INSERT INTO knowledge_revisions
                    SELECT 'geometry-revision-'||n,'geometry-'||n,revision_number,claim_json,
                    identity_json,identity_hash,previous_revision_id,reason,schema_version,created
                    FROM knowledge_revisions CROSS JOIN generate_series(1,?) n
                    WHERE document_id=? AND ''' + condition, (count, document))
                state.db.execute('''INSERT INTO enterprise_documents
                    SELECT 'geometry-'||n,tenant,internal_project,'geometry-revision-'||n,
                    active,policy_version,created,blocked_reason
                    FROM enterprise_documents CROSS JOIN generate_series(1,?) n
                    WHERE id=? AND ''' + condition, (count, document))
                state.db.execute('''INSERT INTO enterprise_dependencies
                    SELECT 'geometry-'||n,source_id
                    FROM enterprise_dependencies CROSS JOIN generate_series(1,?) n
                    WHERE document_id=? AND ''' + condition, (count, document))
            state.db.execute("INSERT INTO cloud_vector_generations VALUES('geometry',?,512,'active',1,1,?)", (MODEL_KEY, count))
            state.db.execute("INSERT INTO cloud_vector_state VALUES(1,'geometry',?,1)", (MODEL_KEY,))
            randomizer = random.Random(91827)
            rows = []
            for n in range(1, count + 1):
                vector = [randomizer.uniform(-1, 1) for _ in range(16)] + [0.0] * 496
                rows.append(('geometry', 'geometry-' + str(n), 'geometry-revision-' + str(n), 'synthetic', _vector(vector)))
            state.db.executemany('INSERT INTO cloud_document_vectors VALUES(?,?,?,?,?::vector)', rows)
        with connect(self.admin_dsn) as db:
            for table in ('cloud_document_vectors', 'knowledge_documents', 'knowledge_revisions', 'enterprise_documents', 'search_document_policies'):
                db.execute('ANALYZE ' + table)

    def test_large_candidate_filter_uses_one_document_key_probe_per_vector(self):
        from agenthub.cloud_retrieval import rank_scoped_candidates
        self.populate(3000)
        with self.store.open() as state:
            calls=[];original=state.db.execute
            def capture(sql,params=None):
                if 'FROM cloud_document_vectors v' in sql:calls.append((sql,params))
                return original(sql,params)
            with patch.object(state.db,'execute',capture):
                rank_scoped_candidates(state.db,'geometry',lexical=False,embedder=GeometryEmbedder(),
                    ranked_where='d.document_id=ANY(?::text[])',
                    ranked_args=([f'geometry-{i}' for i in range(1,3001)],))
            sql,params=calls[-1]
            plan=state.db.execute('EXPLAIN (FORMAT JSON) '+sql,params).fetchone()[0]
            def nodes(node):
                yield node
                for child in node.get('Plans',[]):yield from nodes(child)
            probes=[n for n in nodes(plan[0]['Plan']) if n.get('Relation Name')=='knowledge_documents']
            self.assertTrue(probes)
            self.assertTrue(all('ANY' not in n.get('Index Cond','') for n in probes),
                'Candidate arrays must be filters after a single primary-key probe, not repeated index searches')

    def search(self, *, exact=False, plans=None):
        calls = []
        original = PostgresConnection.execute
        def measured(db, sql, params=None):
            calls.append(sql)
            if '1-(v.embedding' in sql:
                if exact:
                    # Disable only ANN ordering; permission B-tree indexes must
                    # remain available for a fair exact-distance comparison.
                    sql = sql.replace('ORDER BY v.embedding <=> ?::vector LIMIT',
                        'ORDER BY (v.embedding <=> ?::vector) + 0 LIMIT')
                if plans is not None:
                    plans.append(original(db, 'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, params).fetchone()[0][0])
            return original(db, sql, params)
        started = time.perf_counter()
        with patch.object(PostgresConnection, 'execute', measured):
            rows = self.store.candidates(self.ctx, 'synthetic proximity', project='maple', lexical=False, limit=10)
        return rows, len(calls), time.perf_counter() - started

    def test_index_plan_selective_acl_recall_and_constant_query_count(self):
        self.populate(3000)
        expected, _, _ = self.search(exact=True)
        plans = []
        actual, calls, _ = self.search(plans=plans)
        self.assertEqual(len(expected), 10)
        self.assertEqual({x['document_id'] for x in actual}, {x['document_id'] for x in expected})
        self.assertTrue(all(int(x['document_id'].split('-')[-1]) % 100 == 0 for x in actual))
        self.assertIn('cloud_document_vectors_hnsw_cosine', json.dumps(plans))
        if os.environ.get('AGENTNETWORK_VECTOR_RECEIPT'):
            Path(os.environ['AGENTNETWORK_VECTOR_RECEIPT'] + '.plan.json').write_text(
                json.dumps(plans, indent=2) + '\n')
        # Restrict authorized population without per-document SQL additions.
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_documents SET active=0 WHERE id LIKE 'geometry-%' AND id NOT IN ('geometry-100','geometry-200')")
        small, small_calls, _ = self.search()
        self.assertEqual(len(small), 2)
        self.assertEqual(calls, small_calls)
        self.assertLessEqual(calls, 25)

    def test_correction_and_withdrawal_cannot_deliver_stale_vectors(self):
        self.populate(200)
        old = 'geometry-revision-100'
        with self.store.open() as state, state.db:
            state.db.execute('''INSERT INTO knowledge_revisions SELECT 'corrected-revision',document_id,
                revision_number+1,claim_json,identity_json,identity_hash,revision_id,'correction',schema_version,created+1
                FROM knowledge_revisions WHERE revision_id=?''', (old,))
            state.db.execute("UPDATE knowledge_documents SET active_revision_id='corrected-revision' WHERE document_id='geometry-100'")
            state.db.execute("UPDATE enterprise_documents SET revision='corrected-revision' WHERE id='geometry-100'")
        self.assertEqual([r['document_id'] for r in self.search()[0]], ['geometry-200'])
        source = self.seeds['alice'][0]
        self.store.lifecycle(self.ctx, dict(version=VERSION, target_id=source, expected_revision='1',
            operation='withdraw', idempotency_key='geometry-withdraw', reason='synthetic fixture'))
        self.assertEqual(self.search()[0], [])

    def test_paired_measurement_and_index_rollback_preserve_vectors(self):
        self.populate(3000)
        results = {}
        for name, exact in [('exact', True), ('indexed', False)]:
            samples = [self.search(exact=exact) for _ in range(5)]
            results[name] = dict(median_query_seconds=statistics.median(x[2] for x in samples),
                sql_counts=[x[1] for x in samples], ids=[r['document_id'] for r in samples[-1][0]])
        self.assertEqual(set(results['exact']['ids']), set(results['indexed']['ids']))
        with connect(self.admin_dsn) as db:
            before = db.execute('SELECT count(*) FROM cloud_document_vectors').fetchone()[0]
            db.execute('DROP INDEX cloud_document_vectors_hnsw_cosine')
        self.assertEqual({r['document_id'] for r in self.search()[0]}, set(results['exact']['ids']))
        with connect(self.admin_dsn) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cloud_document_vectors').fetchone()[0], before)
        if os.environ.get('AGENTNETWORK_VECTOR_RECEIPT'):
            import agenthub
            results.update(corpus_documents=3000, authorized_fraction=0.01,
                fixture='synthetic geometry; not semantic usefulness', package_path=agenthub.__file__,
                build_id=json.loads((Path(agenthub.__file__).parent/'BUILD_ID.json').read_text())['build_id'])
            Path(os.environ['AGENTNETWORK_VECTOR_RECEIPT']).write_text(json.dumps(results, indent=2)+'\n')
