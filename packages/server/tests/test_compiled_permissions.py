"""Frozen synthetic policy equivalence, atomicity and history-size regressions.

No live models and no private source content. Uses disposable PostgreSQL schemas.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from test_staging_retrieval_sql import CompletePolicyEquivalence


class CompiledPermissions(CompletePolicyEquivalence):
    def compare_full(self, *, expected=None, contexts=None):
        for who, ctx in (contexts or self.contexts).items():
            with self.subTest(principal=who), self.store.open() as state:
                from agenthub.cloud_preferences import CloudPreferencesMixin
                rows=state.db.execute('SELECT document_id FROM knowledge_documents').fetchall()
                oracle={row[0] for row in rows if CloudPreferencesMixin._document_allowed(self.store,state.db,ctx,row[0])}
                if expected and who in expected:self.assertEqual(oracle,expected[who])
                actual=set(self.store._authorized_documents(state,ctx))
                self.assertEqual(actual,oracle)
            ranked=self.store.candidates(ctx,'gauge',vector=False,limit=100)
            self.assertTrue(all(row['document_id'] in oracle for row in ranked))

    def test_same_policy_history_compacts_and_mixed_source_revokes(self):
        source, document = self.note('compact', visibility='team')
        with self.store.open() as state, state.db:
            state.db.execute("""INSERT INTO enterprise_sources
                (id,tenant,owner,external_project,internal_project,external_id,
                 enrollment,payload_hash,visibility,raw_visibility,active,
                 policy_version,source_version,occurred_at,created,
                 occurred_precision,occurred_timezone)
                SELECT 'compact-noise-' || n::text,tenant,owner,external_project,
                 internal_project,'noise-' || n::text,enrollment,payload_hash,
                 visibility,raw_visibility,active,policy_version,source_version,
                 occurred_at,created,occurred_precision,occurred_timezone
                FROM enterprise_sources CROSS JOIN generate_series(1,20000) n WHERE id=?""", (source,))
            state.db.execute("INSERT INTO enterprise_dependencies SELECT ?,id FROM enterprise_sources WHERE id LIKE ?", (document, 'compact-noise-%'))
        self.compare_full(expected={'alice': {document}, 'bob': {document}, 'admin': set()})
        with self.store.open() as state:
            rows = state.db.execute('SELECT dependency_count FROM search_document_policies WHERE document_id=?', (document,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(sum(r[0] for r in rows), 20001)
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_sources SET visibility='private' WHERE id='compact-noise-1'")
        self.compare_full(expected={'alice': {document}, 'bob': set(), 'admin': set()})
        # Removing the restricted input restores the precise remaining policy.
        with self.store.open() as state, state.db:
            state.db.execute("DELETE FROM enterprise_dependencies WHERE document_id=? AND source_id='compact-noise-1'", (document,))
        self.compare_full(expected={'alice': {document}, 'bob': {document}, 'admin': set()})

    def test_missing_source_and_transaction_rollback(self):
        source, document = self.note('atomic', visibility='team')
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with self.store.open() as state, state.db:
                state.db.execute("UPDATE enterprise_sources SET active=0 WHERE id=?", (source,))
                raise RuntimeError('abort')
        self.compare_full(expected={'alice': {document}, 'bob': {document}, 'admin': set()})
        from psycopg.errors import ForeignKeyViolation
        with self.assertRaises(ForeignKeyViolation), self.store.open() as state, state.db:
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)', (document, 'missing-original'))
        # Missing sources cannot enter the authoritative relation. An explicitly
        # injected missing projection must nevertheless fail closed, then repair.
        with self.store.open() as state, state.db:
            state.db.execute('DELETE FROM enterprise_dependencies WHERE document_id=?', (document,))
            state.db.execute('DELETE FROM search_source_policies WHERE source_id=?', (source,))
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)', (document, source))
        self.store.retrieval_authorization_shape = 'compiled'
        self.assertEqual(self.store.candidates(self.contexts['alice'], 'gauge', vector=False), [])
        with self.store.open() as state, state.db:
            state.db.execute('SELECT search_rebuild_permissions()')
        self.compare_full(expected={'alice': {document}, 'bob': {document}, 'admin': set()})

    def test_committed_revocation_visible_across_connections(self):
        source, document = self.note('concurrent', visibility='team')
        self.store.retrieval_authorization_shape = 'compiled'
        self.assertTrue(self.store.candidates(self.contexts['bob'], 'gauge', vector=False))
        def revoke():
            with self.store.open() as state, state.db:
                state.db.execute("UPDATE enterprise_sources SET visibility='private' WHERE id=?", (source,))
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(revoke).result(timeout=10)
            result = pool.submit(self.store.candidates, self.contexts['bob'], 'gauge', vector=False).result(timeout=10)
        self.assertEqual(result, [])
        self.compare_full(expected={'alice': {document}, 'bob': set(), 'admin': set()})

    def test_rank_sql_does_not_traverse_source_provenance(self):
        self.note('plan', visibility='team')
        self.store.retrieval_authorization_shape = 'compiled'
        from agenthub.postgres import PostgresConnection
        from unittest.mock import patch
        original = PostgresConnection.execute
        statements = []
        def capture(connection, sql, params=None):
            if isinstance(sql, str) and ('ts_rank_cd' in sql or 'search_lexical_matches' in sql):
                statements.append(sql)
                plan = original(connection, 'EXPLAIN (FORMAT JSON) ' + sql, params).fetchone()[0]
                raw = json.dumps(plan)
                for relation in ('enterprise_dependencies', 'enterprise_sources', 'backend_source_revisions'):
                    self.assertNotIn('"Relation Name": "' + relation + '"', raw)
            return original(connection, sql, params)
        with patch.object(PostgresConnection, 'execute', capture):
            self.store.candidates(self.contexts['alice'], 'gauge', vector=False)
        self.assertEqual(len(statements), 1)
        self.assertNotIn('authorized_docs AS MATERIALIZED', statements[0])
        if 'search_lexical_matches' in statements[0]:
            with self.store.open() as state:
                definition = state.db.execute("SELECT pg_get_functiondef('search_lexical_matches(text)'::regprocedure)").fetchone()[0]
            self.assertIn('search_document_allowed', definition)
            self.assertNotIn('enterprise_dependencies', definition)

    def test_concurrent_dependency_writers_keep_exact_policy_counts(self):
        source, document = self.note('parallel-target', visibility='team')
        team, other = self.note('parallel-team', visibility='team')
        private, restricted = self.note('parallel-private')
        def add(source_id):
            with self.store.open() as state, state.db:
                state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)', (document, source_id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(add, [team, private]))
        self.compare_full(expected={'alice': {document, other, restricted}, 'bob': {other}, 'admin': set()})
        from agenthub.search_permissions import inspect_projection
        with self.store.open() as state:
            audit = inspect_projection(state.db)
            self.assertEqual(audit['requirement_mismatches'], 0)
            self.assertEqual(audit['source_policy_mismatches'], 0)

    def test_dependency_update_and_truncate_do_not_leave_stale_allows(self):
        source, document = self.note('replace-dependency', visibility='team')
        private, restricted = self.note('replacement-private')
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_dependencies SET source_id=? WHERE document_id=?', (private, document))
        self.compare_full(expected={'alice': {document, restricted}, 'bob': set(), 'admin': set()})
        # Administrative truncation is a disaster/rebuild fixture, not a serving
        # command. Even this must remove allows atomically.
        from agenthub.postgres import connect
        with connect(self.admin_dsn) as db:
            db.execute('TRUNCATE enterprise_dependencies')
        self.compare_full(expected={'alice': set(), 'bob': set(), 'admin': set()})
