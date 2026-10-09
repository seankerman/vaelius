"""Frozen F1/F3 regressions: extra context is not changed original evidence."""
import copy
import json
import unittest
from unittest.mock import patch
import test_durable_pipeline as fixtures
from agenthub.processing.episode_pipeline import run_once, retry_held_job, sources_for_job


class ExtractionCheckpointTests(unittest.TestCase):
    setUp = fixtures.DurablePipelineTests.setUp
    tearDown = fixtures.DurablePipelineTests.tearDown
    source = fixtures.DurablePipelineTests.source
    episode = fixtures.DurablePipelineTests.episode
    save_config = fixtures.DurablePipelineTests.save_config

    def previous(self):
        self.episode('prior', 'Earlier cobalt rationale', created=1)
        run_once(self.state, self.cfg, self.runner)
        row = self.state.db.execute("SELECT * FROM curation_episode_jobs WHERE turn='prior'").fetchone()
        return sources_for_job(self.state.db, row)

    def interrupted(self, context):
        self.episode('current', created=10)
        with patch('agenthub.processing.episode_pipeline._install', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_once(self.state, self.cfg, self.runner, context_sources=context)

    def test_expanded_context_keeps_valid_output_and_does_not_dispatch(self):
        earlier = self.previous()
        self.interrupted([])
        calls = list(self.runner.calls)
        run_once(self.state, self.cfg, self.runner, context_sources=earlier)
        row = self.state.db.execute("SELECT status,error FROM curation_episode_jobs WHERE turn='current'").fetchone()
        self.assertEqual((row['status'], row['error']), ('done', None))
        self.assertEqual(calls, self.runner.calls)

    def test_changed_uncited_original_context_invalidates_saved_output(self):
        earlier = self.previous()
        self.interrupted(earlier)
        changed = copy.deepcopy(earlier); changed[0]['body'] += ' changed'
        run_once(self.state, self.cfg, self.runner, context_sources=changed)
        row = self.state.db.execute("SELECT status,error FROM curation_episode_jobs WHERE turn='current'").fetchone()
        self.assertEqual(row['error'], 'memory_reference_manifest_changed')

    def test_extract_then_consolidate_uses_saved_envelope_without_extraction(self):
        self.episode('current')
        self.cfg['_pipeline_phase'] = 'extract'; self.save_config()
        run_once(self.state, self.cfg, self.runner)
        row = self.state.db.execute('SELECT * FROM curation_episode_jobs').fetchone()
        progress = json.loads(row['progress'])
        self.assertIn('extraction', progress)
        self.assertEqual((row['status'], row['stage']), ('pending', 'extraction_ready'))
        self.assertEqual(self.runner.calls, ['durable_memory_curate'])
        artifact = progress['extraction']
        self.cfg['_pipeline_phase'] = 'consolidate'; self.save_config()
        run_once(self.state, self.cfg, self.runner)
        row = self.state.db.execute('SELECT * FROM curation_episode_jobs').fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertEqual(json.loads(row['progress'])['extraction'], artifact)
        self.assertEqual(self.runner.calls, ['durable_memory_curate', 'episode_resolve'])

    def test_consolidate_without_extraction_denies_without_a_call(self):
        self.episode('current'); self.cfg['_pipeline_phase'] = 'consolidate'; self.save_config()
        run_once(self.state, self.cfg, self.runner)
        self.assertFalse(self.runner.calls)
        row = self.state.db.execute('SELECT error FROM curation_episode_jobs').fetchone()
        self.assertEqual(row[0], 'extraction_artifact_required')

    def test_extract_envelope_tamper_denied(self):
        self.episode('current'); self.cfg['_pipeline_phase'] = 'extract'; self.save_config()
        run_once(self.state, self.cfg, self.runner)
        row = self.state.db.execute('SELECT * FROM curation_episode_jobs').fetchone()
        p = json.loads(row['progress']); p['extraction']['result']['candidates'][0]['claim'] = 'invented'
        with self.state.db:
            self.state.db.execute('UPDATE curation_episode_jobs SET progress=? WHERE id=?', (json.dumps(p), row['id']))
        self.cfg['_pipeline_phase'] = 'consolidate'; self.save_config()
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls, ['durable_memory_curate'])
        self.assertEqual(self.state.db.execute('SELECT error FROM curation_episode_jobs').fetchone()[0], 'extraction_artifact_changed')
