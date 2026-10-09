"""Bounded operator receipts and backward-compatible first-pass migration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agenthub.curation_operator import main, saved_review
from agenthub.backend_worker import Worker
import test_cloud_postgres_worker as fixtures
from test_cloud_postgres import SERVICES


class ReceiptTests(unittest.TestCase):
    def test_existing_receipt_prevents_any_runtime_action(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'receipt.json'
            output.write_text('original receipt')
            with patch('agenthub.curation_operator.runtime') as runtime:
                with self.assertRaisesRegex(ValueError, 'operator_receipt_exists'):
                    main(['pause', '--profile', temp, '--output', str(output)])
                runtime.assert_not_called()
            self.assertEqual(output.read_text(), 'original receipt')


@unittest.skipUnless(SERVICES, 'explicit real PostgreSQL services manifest required')
class MigrationTests(unittest.TestCase):
    setUp = fixtures.PostgresWorkerTests.setUp
    tearDown = fixtures.PostgresWorkerTests.tearDown
    postgres_setup = fixtures.PostgresWorkerTests.postgres_setup
    postgres_teardown = fixtures.PostgresWorkerTests.postgres_teardown
    turn = fixtures.PostgresWorkerTests.turn

    def test_legacy_saved_stage_preview_migration_and_rollback(self):
        def runner(*args, **kwargs):
            return {'records': [], 'episode_summary': {'intent': None, 'open_work': []}}, {}, None
        Worker(self.store, self.config, runner=runner, live=False).run(max_seconds=10)
        with self.store.open() as state, state.db:
            job = state.db.execute('SELECT * FROM backend_jobs').fetchone()
            episode = state.db.execute('SELECT * FROM curation_episode_jobs').fetchone()
            legacy = json.loads(episode['progress'])
            legacy.pop('extraction')
            for stage in legacy['stage_outputs']:
                for key in ('input_manifest', 'schema_hash', 'request_hash', 'media_policy'):
                    stage.pop(key, None)
            before = json.dumps(legacy)
            state.db.execute('UPDATE curation_episode_jobs SET progress=? WHERE id=?', (before, episode['id']))
            old_status = job['status']
            returns = state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0]
        preview = saved_review(self.store, self.config, job['id'])
        self.assertEqual(preview['new_model_attempts'], 0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0], before)
        migrated = saved_review(self.store, self.config, job['id'], persist=True)
        self.assertEqual(migrated['extraction'], preview['extraction'])
        with self.store.open() as state, state.db:
            progress = json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs').fetchone()[0], old_status)
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0], returns)
            self.assertEqual(progress['stage_outputs'], legacy['stage_outputs'])
            # Rollback to the exact old checkpoint is additive and does not
            # delete the immutable provider return or replay extraction.
            state.db.execute('UPDATE curation_episode_jobs SET progress=? WHERE id=?', (before, episode['id']))
        self.assertEqual(saved_review(self.store, self.config, job['id'])['new_model_attempts'], 0)


if __name__ == '__main__':
    unittest.main()
