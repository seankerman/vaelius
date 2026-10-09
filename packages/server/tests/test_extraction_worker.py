"""F2/F3 frozen worker phase and context tests, with no live provider."""
import json
import unittest
import test_backend_worker as fixtures
from agenthub.backend_worker import Worker


class ExtractionWorkerTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    tearDown = fixtures.WorkerTests.tearDown
    turn = fixtures.WorkerTests.turn

    def runner(self, *args, **kwargs):
        self.sessions.append(kwargs.get('session_id'))
        return {'records': [], 'episode_summary': {'intent': None, 'open_work': []}}, {}, '00000000-0000-4000-8000-000000000001'

    def test_extract_advances_session_and_does_not_reclaim_ready(self):
        self.sessions = []; self.config['_pipeline_phase'] = 'extract'
        w = Worker(self.store, self.config, runner=self.runner, live=False)
        result = w.run(max_seconds=10)
        self.assertEqual(result['extracted'], 1)
        self.assertEqual(result['completed'], 0)
        self.assertIsNone(w.claim())
        self.turn('2')
        result = Worker(self.store, self.config, runner=self.runner, live=False).run(max_seconds=10)
        self.assertEqual(result['extracted'], 1)
        self.assertEqual(self.sessions, [None, '00000000-0000-4000-8000-000000000001'])
        with self.store.open() as state:
            checkpoint = json.loads(state.db.execute('SELECT checkpoint FROM backend_observers').fetchone()[0])
            self.assertTrue(checkpoint['context_index'])
        self.config['_pipeline_phase'] = 'consolidate'
        def denied(*args, **kwargs): raise AssertionError('extraction repeated')
        result = Worker(self.store, self.config, runner=denied, live=False).run(max_seconds=10)
        self.assertEqual((result['completed'], result['calls']), (1, 0))

    def test_expired_lease_preserves_confirmed_stage_outputs(self):
        from unittest.mock import patch
        self.sessions = []
        w = Worker(self.store, self.config, runner=self.runner, live=False)
        with patch('agenthub.processing.episode_pipeline._install', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): w.run(max_seconds=10)
        with self.store.open() as state, state.db:
            before = json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            state.db.execute('UPDATE backend_jobs SET lease_until=0')
        w.claim()
        with self.store.open() as state:
            after = json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
        self.assertEqual(before['stage_outputs'], after['stage_outputs'])

    def test_warm_context_reuses_bounded_index_but_withdrawal_denies(self):
        self.sessions = []; w = Worker(self.store, self.config, runner=self.runner, live=False)
        w.run(max_seconds=10); self.turn('2'); w.prepare(); job = w.claim()
        with self.store.open() as state:
            row, current, previous = w._context(state, job)
            self.assertEqual(len(previous), 2)
            self.assertEqual(w.context_profile['cache'], 'warm')
            state.db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?', (previous[0]['id'],)); state.db.commit()
            with self.assertRaises(ValueError): w.guard(state.db, job)
