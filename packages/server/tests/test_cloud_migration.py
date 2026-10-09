"""Synthetic immutable-backup migration before any selected private rehearsal."""
import hashlib
import json
from pathlib import Path
import unittest
import sqlite3

from agentclient.enterprise_contract import VERSION
from agenthub.processing.episode_pipeline import create_jobs
from agentclient.enterprise_capture import normalize_capture
from agenthub.enterprise import Denied
from vaelius_test_support.fixtures.postgres import database
from agenthub.cloud_migration import migrate_snapshot, rollback_same_authority
from agenthub.source_objects import FileSourceObjects
from agenthub.postgres import PostgresEnterpriseStore
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, "explicit real PostgreSQL services manifest required")
class MigrationTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup(bootstrap=False)
        home=Path(self.temp.name)/'old-synthetic'
        self.old = PostgresEnterpriseStore(home,database(home),'acme')
        self.old.create_organization('acme'); self.old.create_principal('acme', 'alice')
        self.old.create_project('acme', 'maple'); self.old.set_membership('acme', 'maple', 'alice', True)
        token = self.old.enroll('acme', 'alice', 'old-device', ['ingest','read','source_read','withdraw','correct','policy'])
        self.ctx = self.old.authenticate(token)
        self.source = self.old.ingest(self.ctx, {'version': VERSION, 'external_id':'artifact',
            'session':'chat', 'turn':'1', 'project':'maple', 'kind':'Stop',
            'body':'The synthetic Maple dataset was saved at /tmp/maple.csv.',
            'occurred_at':12345, 'visibility':'private'})['source_id']
        self.document = self.old.accept_reviewed_note(self.ctx, self.source, 'Maple dataset',
            'The synthetic Maple dataset was saved at /tmp/maple.csv.')['document_id']
        self.old.enroll_connection(self.ctx, 'agent', 'fixture', 'maple', ['agent'])
        for kind in ('UserPromptSubmit','Stop'):
            self.old.ingest_general(self.ctx, normalize_capture({'hook_event_name':kind,
                'event_id':'held-'+kind, 'session_id':'held-chat', 'turn_id':'held-turn',
                'prompt':'Keep this work pending until the source is reviewed.',
                'last_assistant_message':'The synthetic work remains pending.'}, 'maple', 'agent'))
        config={'knowledge_backend':{'mode':'enterprise_local'}, 'observer':{'model':'fixture'},
            'episode_curation':{'policy':'durable_memory','generation_id':'migration-building','settle_seconds':0}}
        with self.old.open() as state, state.db:
            scope = state.db.execute("SELECT internal_project FROM enterprise_sources WHERE external_id LIKE 'agent:%' LIMIT 1").fetchone()[0]
            create_jobs(state, config, project_scope=scope)
            state.db.execute("UPDATE curation_episode_jobs SET status='held',error='fixture_review_pending'")
        self.snapshot = Path(self.temp.name) / 'selected-immutable.sqlite'
        # Build a selected legacy-format input using only synthetic canonical rows.
        # This fixture is a file-format writer, never a SQLite service.
        from agenthub.cloud_migration import TABLES
        from agenthub.processing.storage import columns
        with self.old.open() as state, sqlite3.connect(self.snapshot) as target:
            for table in TABLES:
                fields=columns(state.db,table)
                if not fields:continue
                target.execute('CREATE TABLE "'+table+'" ('+','.join('"'+c+'"' for c in fields)+')')
                rows=state.db.execute('SELECT * FROM "'+table+'"').fetchall()
                if rows:target.executemany('INSERT INTO "'+table+'" VALUES ('+','.join('?' for _ in fields)+')',
                    [tuple(json.dumps(row[c]) if isinstance(row[c],(dict,list)) else row[c] for c in fields) for row in rows])
        self.snapshot.chmod(0o400)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        self.objects = FileSourceObjects(Path(self.temp.name) / 'objects')

    def tearDown(self):
        self.postgres_teardown()

    def migrate(self):
        # Migration credentials only reset sequences/import rows. Ordinary
        # subsequent operations continue through the separate application role.
        operator = PostgresEnterpriseStore(self.store.home, self.admin_dsn, 'acme')
        return migrate_snapshot(self.snapshot, operator, expected_sha256=self.sha, objects=self.objects)

    def test_all_content_ids_held_states_and_object_checksums_reconcile(self):
        receipt = self.migrate()
        self.assertEqual(receipt['status'], 'migrated')
        self.assertEqual(receipt['counts']['enterprise_sources']['count'], 3)
        self.assertEqual(len(receipt['objects']), 3)
        self.assertFalse(receipt['sqlite_runtime_created'])
        self.assertEqual(hashlib.sha256(self.snapshot.read_bytes()).hexdigest(), self.sha)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT lifecycle FROM knowledge_documents WHERE document_id=?', (self.document,)).fetchone()[0], 'active')
            self.assertEqual(state.db.execute('SELECT status FROM curation_episode_jobs').fetchone()[0], 'held')
            self.assertEqual(state.db.execute('SELECT status FROM knowledge_generations').fetchone()[0], 'building')
        self.assertEqual(list(self.store.home.rglob('*.sqlite')), [])

    def test_cloud_reenrollment_and_post_migration_withdrawal_survive_code_rollback(self):
        receipt = self.migrate()
        token = self.store.enroll('acme', 'alice', 'cloud-device', ['read','ingest','source_read','withdraw','correct'])
        ctx = self.store.authenticate(token)
        self.assertIsNotNone(self.store.detail(ctx, self.document))
        self.store.lifecycle(ctx, {'version':VERSION,'target_id':self.source,'expected_revision':'1',
            'idempotency_key':'after-migration-withdraw','reason':'synthetic owner withdrew source','operation':'withdraw'})
        with self.store.open() as state:
            identity = state.db.execute('SELECT current_database()').fetchone()[0]
        proof = rollback_same_authority(self.store, migration_id=receipt['id'],
            previous_build='synthetic-previous-postgresql-compatible-build', expected_database_identity=identity)
        self.assertEqual(proof['lifecycle_count'], 1)
        self.assertFalse(proof['sqlite_restored'])
        self.assertFalse(proof['installed_previous_build'])
        with self.assertRaises(Denied):
            self.store.detail(ctx, self.document)

    def test_checksum_and_nonempty_target_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'checksum_mismatch'):
            migrate_snapshot(self.snapshot, self.store, expected_sha256='0'*64, objects=self.objects)
        self.store.create_organization('acme')
        with self.assertRaisesRegex(ValueError, 'empty_migration_target'):
            self.migrate()
        with self.assertRaisesRegex(ValueError, 'unreconciled'):
            self.store.require_ready()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0], 0)

    def test_invalid_legacy_provenance_rolls_back_entire_import(self):
        self.snapshot.chmod(0o600)
        with sqlite3.connect(self.snapshot) as db:
            db.execute('INSERT INTO knowledge_document_members VALUES(?,?,?,?)',
                ('missing-document', self.source, 'supports', 1.0))
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.execute('PRAGMA journal_mode=DELETE'); db.close()
        self.snapshot.chmod(0o400)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        import psycopg
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.migrate()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0], 0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_migration_receipts').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError,'unreconciled'):
            self.store.require_ready()

    def test_missing_native_original_is_explicit_gap_and_url_is_not_followed(self):
        self.snapshot.chmod(0o600)
        with sqlite3.connect(self.snapshot) as db:
            db.execute('INSERT INTO backend_native_artifacts VALUES(?,?,?,?)',
                (self.document,self.source,'document','file:///unavailable/historical-private-original.docx'))
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.execute('PRAGMA journal_mode=DELETE'); db.close()
        self.snapshot.chmod(0o400)
        self.sha = hashlib.sha256(self.snapshot.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'original_gap'):
            self.migrate()
        operator = PostgresEnterpriseStore(self.store.home,self.admin_dsn,'acme')
        receipt = migrate_snapshot(self.snapshot,operator,expected_sha256=self.sha,
            objects=self.objects,allow_missing_originals=True)
        self.assertEqual(receipt['status'],'migrated_with_original_gaps')
        self.assertEqual(len(receipt['original_gaps']),1)
        self.assertEqual(receipt['original_gaps'][0]['source_id'],self.source)


if __name__ == '__main__':
    unittest.main()
