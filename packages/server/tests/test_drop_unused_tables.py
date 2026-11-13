"""Migration 058 drops only tables nothing writes: the read predicate, every other
function and every row-level-security policy are the same before and after it."""
import json
from pathlib import Path
import unittest
import uuid

from agenthub.postgres import connect, migrate
from test_cloud_postgres import SERVICES

DROPPED = (
    'backend_jobs',
    'backend_model_input_snapshots',
    'backend_observer_dependencies',
    'backend_observers',
    'backend_processing_dependencies',
    'backend_provider_returns',
    'backend_slack_connectors',
    'backend_slack_events',
    'backend_slack_items',
    'backend_slack_limits',
    'backend_worker_receipts',
    'cloud_admission',
    'cloud_usage_attempts',
    'cloud_usage_reconciliations',
    'curation_episode_jobs',
    'curation_episode_receipts',
    'enterprise_model_inputs',
    'episode_candidate_history',
    'episode_candidates',
    'episode_occurrences',
    'episode_revision_claim_links',
    'episode_revision_evidence',
    'episode_revisions',
    'knowledge_temporal_assertions',
    'knowledge_temporal_evidence',
    'knowledge_temporal_relations',
    'memory_metadata',
    'model_calls',
)
# Read by search_document_allowed, so kept although nothing writes them either.
KEPT_FOR_THE_READ_PREDICATE = ('cloud_preference_candidates', 'knowledge_generations', 'knowledge_generation_documents')
READ_PREDICATE = {'search_document_allowed', 'search_reader_in_audience', 'search_session_admits',
                  'search_workspace_role', 'search_audience_is_organization', 'search_lexical_matches'}


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class DropUnusedTables(unittest.TestCase):
    def setUp(self):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        self.item = json.loads(Path(SERVICES).read_text())['tenants']['acme']
        self.schema = 'test_drop_tables_' + uuid.uuid4().hex[:12]
        with connect(self.item['admin_dsn']) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.admin_dsn = make_conninfo(self.item['admin_dsn'], options='-c search_path=' + self.schema + ',public')

    def tearDown(self):
        from psycopg import sql
        from agenthub.search_reader import reader_role
        with connect(self.item['admin_dsn']) as db:
            role = reader_role(db.execute('SELECT current_database()').fetchone()[0], self.schema, self.item['role'])
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
            db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))

    def definitions(self):
        """Every function, row-level-security policy, protected table, trigger and view of the schema."""
        with connect(self.admin_dsn) as db:
            run = lambda query: [tuple(row.values()) for row in db.execute(query, (self.schema,)).fetchall()]
            return {
                'functions': run("""SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid),
                    p.proconfig::text,p.proacl::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname=%s AND p.prokind='f' ORDER BY 1,2"""),
                'policies': run("""SELECT tablename,policyname,permissive,roles::text,cmd,qual,with_check
                    FROM pg_policies WHERE schemaname=%s ORDER BY 1,2"""),
                'row_security': run("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r'
                    AND c.relrowsecurity ORDER BY 1"""),
                'triggers': run("""SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid) FROM pg_trigger t
                    JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND NOT t.tgisinternal ORDER BY 1,2"""),
                'views': run("SELECT viewname,definition FROM pg_views WHERE schemaname=%s ORDER BY 1"),
            }

    def tables(self):
        with connect(self.admin_dsn) as db:
            return {row[0] for row in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s', (self.schema,))}

    def test_058_drops_only_unwritten_tables_and_leaves_the_read_predicate_and_policies_alone(self):
        from agenthub.cloud_profile import _grant
        migrate(self.admin_dsn, through=57)
        # The search reader's row-level-security policies are provisioned with the grants.
        _grant(self.admin_dsn, self.item['role'])
        before, tables = self.definitions(), self.tables()
        self.assertLessEqual(set(DROPPED), tables)
        self.assertLessEqual(READ_PREDICATE, {name for name, *_ in before['functions']})
        self.assertTrue(before['policies'])
        receipts = migrate(self.admin_dsn)
        self.assertEqual({r['version']: r['status'] for r in receipts}[58], 'applied')
        self.assertEqual(self.tables(), tables - set(DROPPED), 'exactly the unwritten tables are gone')
        self.assertLessEqual(set(KEPT_FOR_THE_READ_PREDICATE), self.tables())
        after = self.definitions()
        for kind in before:
            self.assertEqual(after[kind], before[kind], kind + ' are unchanged')


if __name__ == '__main__':
    unittest.main()
