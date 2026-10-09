from vaelius_test_support.fixtures.state import invalidate_source
"""Finish-plan integration gates, authored before connecting the broad writer."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_episode_pipeline as fixtures
from agenthub.processing.episode_pipeline import run_once, create_jobs, activate_generation, generation_status
from vaelius_test_support.fixtures.state import State


class MemoryRunner(fixtures.FixtureRunner):
    def __call__(self, home, config, instruction, payload, schema):
        if config['_purpose'] != 'durable_memory_curate':
            return super().__call__(home, config, instruction, payload, schema)
        self.calls.append(config['_purpose'])
        event = next(e for e in payload['episode']['events'] if e['kind'] == 'PostToolUse')
        corrected = 'corrected' in event['spans'][0]['text']
        return {'records': [dict(title='Cobalt retry verification',
            text='Use the corrected Cobalt retry policy.' if corrected else 'Use the initial Cobalt retry policy.',
            subject='Cobalt retry', facets=['activity', 'procedure'], actors=['agent'],
            artifact=None, rationale=None, state='observed', occurred_date='',
            event_id=event['event_id'], evidence_span_ids=[event['spans'][0]['span_id']])]}, {}


class DurablePipelineTests(unittest.TestCase):
    tearDown = fixtures.EpisodePipelineTests.tearDown
    source = fixtures.EpisodePipelineTests.source
    episode = fixtures.EpisodePipelineTests.episode

    def setUp(self):
        fixtures.EpisodePipelineTests.setUp(self)
        self.cfg['episode_curation']['policy'] = 'durable_memory'
        self.cfg['durable_memory_retrieval'] = True
        self.save_config()
        self.runner = MemoryRunner()

    def save_config(self):
        (self.home / 'config.json').write_text(json.dumps(self.cfg))

    def visible(self):
        from vaelius_test_support.fixtures.state import visible_episode_claims
        return visible_episode_claims(self.state)

    def test_checkpoint_restart_delivery_correction_and_withdrawal(self):
        self.episode('first')
        with patch('agenthub.processing.episode_pipeline._install', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls, ['durable_memory_curate', 'episode_resolve'])
        self.assertEqual(generation_status(self.state.db, 'fixture-generation')['documents'], 0)
        self.state.close(); self.state = State(self.home)
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls, ['durable_memory_curate', 'episode_resolve'])
        self.assertEqual(self.visible(), [])  # Building generation is invisible.
        activate_generation(self.state.db, 'fixture-generation')
        self.assertIn('initial', self.visible()[0]['text'])
        self.episode('second', 'Cobalt corrected retry test passed', created=10)
        run_once(self.state, self.cfg, self.runner)
        self.assertIn('corrected', self.visible()[0]['text'])
        self.assertNotIn('initial', self.visible()[0]['text'])
        self.assertFalse(run_once(self.state, self.cfg, self.runner))
        self.assertEqual(len(self.runner.calls), 4)
        invalidate_source(self.state,'second-t')
        self.assertNotIn('corrected', json.dumps(self.visible()))
        invalidate_source(self.state,'first-t')
        self.assertEqual(self.visible(), [])

    def test_candidate_generation_cannot_modify_active_generation(self):
        self.episode('first')
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'fixture-generation')
        before = self.visible()[0]['revision']
        self.cfg['episode_curation']['generation_id'] = 'rebuild'
        self.save_config()
        run_once(self.state, self.cfg, self.runner)
        self.episode('second', 'Cobalt corrected retry test passed', created=10)
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.visible()[0]['revision'], before)
        self.assertIn('initial', self.visible()[0]['text'])

    def test_source_content_changed_after_checkpoint_requires_new_curation(self):
        self.episode('first')
        with patch('agenthub.processing.episode_pipeline._install', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_once(self.state, self.cfg, self.runner)
        with self.state.db:
            self.state.db.execute("UPDATE memories SET body='Cobalt corrected retry test passed' WHERE id='first-t'")
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls.count('durable_memory_curate'), 2)
        activate_generation(self.state.db, 'fixture-generation')
        self.assertIn('corrected', self.visible()[0]['text'])

    def test_completed_jobs_do_not_starve_later_turns(self):
        self.episode('first'); self.episode('second', created=10)
        with self.state.db:
            self.assertEqual(create_jobs(self.state, self.cfg, limit=1), 1)
            self.state.db.execute("UPDATE curation_episode_jobs SET status='no_learning'")
            self.assertEqual(create_jobs(self.state, self.cfg, limit=1), 1)

    def test_policy_change_cannot_reuse_generation_definition(self):
        self.episode('first')
        with self.state.db:
            create_jobs(self.state, self.cfg)

    def test_explicit_rollback_restores_previous_generation_without_resurrection(self):
        from agenthub.processing.episode_pipeline import rollback_generation
        self.episode('first')
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'fixture-generation')
        original = self.visible()[0]['revision']
        self.cfg['episode_curation']['generation_id'] = 'rebuild'
        self.save_config()
        run_once(self.state, self.cfg, self.runner)
        self.episode('second', 'Cobalt corrected retry test passed', created=10)
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'rebuild')
        self.assertIn('corrected', self.visible()[0]['text'])
        rollback_generation(self.state.db, 'fixture-generation')
        self.assertEqual(self.visible()[0]['revision'], original)
        invalidate_source(self.state,'first-t')
        rollback_generation(self.state.db, 'rebuild')
        rollback_generation(self.state.db, 'fixture-generation')
        self.assertEqual(self.visible(), [])

    def test_withdrawal_during_model_call_cannot_install_stale_candidate(self):
        self.episode('first')
        def withdraw_runner(*args):
            result = self.runner(*args)
            if args[1]['_purpose'] == 'episode_resolve':
                invalidate_source(self.state,'first-t')
            return result
        run_once(self.state, self.cfg, withdraw_runner)
        status = generation_status(self.state.db, 'fixture-generation')
        self.assertEqual(status['documents'], 0)
        self.assertEqual(status['jobs'], {'withdrawn': 1})

    def test_invalid_candidates_remain_a_gap_with_no_repeated_model_dispatch(self):
        self.episode('first')
        def bad_runner(*args):
            result, usage = self.runner(*args)
            result['records'][0]['event_id'] = 'invented'
            return result, usage
        for _ in range(3):
            run_once(self.state, self.cfg, bad_runner)
            with self.state.db:
                self.state.db.execute('UPDATE curation_episode_jobs SET next_attempt=0')
        status = generation_status(self.state.db, 'fixture-generation')
        self.assertEqual(status['jobs'], {'held': 1})
        self.assertEqual(status['documents'], 0)
        self.assertEqual(self.runner.calls, ['durable_memory_curate'])
        with self.assertRaisesRegex(ValueError, 'generation_not_drained'):
            activate_generation(self.state.db, 'fixture-generation')

    def test_reviewed_held_rejection_can_dispatch_one_fresh_attempt(self):
        from agenthub.processing.episode_pipeline import retry_held_job
        self.episode('first')
        def bad_runner(*args):
            result, usage = self.runner(*args)
            result['records'][0]['event_id'] = 'invented'
            return result, usage
        for _ in range(3):
            run_once(self.state, self.cfg, bad_runner)
            with self.state.db:
                self.state.db.execute('UPDATE curation_episode_jobs SET next_attempt=0')
        row = self.state.db.execute('SELECT id,progress FROM curation_episode_jobs').fetchone()
        self.assertEqual(len(json.loads(row['progress'])['stage_outputs']), 1)
        self.assertEqual(self.runner.calls, ['durable_memory_curate'])
        retry_held_job(self.state.db, row['id'])
        self.assertEqual(self.state.db.execute("SELECT progress::jsonb->'stage_outputs' FROM curation_episode_jobs").fetchone()[0], None)
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls.count('durable_memory_curate'), 2)
        self.assertEqual(generation_status(self.state.db, 'fixture-generation')['documents'], 1)
