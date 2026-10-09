"""Frozen regressions for confirmed-return replay across observer epochs."""
import json
import unittest
from unittest.mock import patch

from test_backend_worker import WorkerTests
from agentclient.enterprise_capture import normalize_capture
from agenthub.processing.harness import run_structured_session
from agenthub.backend_worker import Worker


class ObserverRecoveryTests(WorkerTests):
    def test_missing_current_usage_checkpoint_reconstructs_before_native_dispatch(self):
        self.config['backend_execution']={'kind':'codex'}
        self.config['accounting_home']=str(self.store.home.parent/'accounting')
        self.config['backend_worker']={'compact_after_chars':1_000_000}
        calls=[]
        def invoke(home,config,instruction,payload,schema,**kwargs):
            kwargs['session_dir'].mkdir(parents=True,exist_ok=True,mode=0o700)
            calls.append((payload,kwargs['session_id']))
            return ({'records':[],'episode_summary':{'intent':None,'open_work':[]}},
                    {'input_tokens':10,'output_tokens':1},
                    '00000000-0000-4000-8000-'+f'{len(calls):012d}')
        with patch('agenthub.processing.harness._invoke',side_effect=invoke):
            worker=Worker(self.store,self.config,runner=run_structured_session,live=False)
            self.assertEqual(worker.run(max_seconds=30)['completed'],1)
            checkpoint=next((self.store.home/'observers').glob('*/0/usage.json'))
            checkpoint.unlink()
            self.turn('2')
            resumed=Worker(self.store,self.config,runner=run_structured_session,live=False).run(max_seconds=30)
        self.assertEqual((resumed['completed'],resumed['calls']),(1,1))
        self.assertEqual(len(calls),2)
        self.assertIsNone(calls[1][1])
        self.assertIn('stable header',json.dumps(calls[1][0]['previous_evidence_index']))
        self.assertTrue(checkpoint.parent.parent.joinpath('1/usage.json').exists())

    def test_replayed_stage_reconstructs_original_context_without_resuming_old_epoch(self):
        self.config['backend_execution'] = {'kind': 'codex'}
        self.config['accounting_home'] = str(self.store.home.parent / 'accounting')
        self.config['episode_curation'].update(
            split_oversized_events=True, max_chars_per_stage=2000)
        self.config['backend_worker'] = {'compact_after_chars': 1_000_000}
        self.store.ingest_general(self.ctx, normalize_capture({
            'hook_event_name': 'PostToolUse', 'event_id': 'large-tool',
            'session_id': 'chat', 'turn_id': '1', 'tool_name': 'exec_command',
            'tool_response': 'Cobalt retry verified.\n' * 300, 'exit_code': 0,
        }, 'maple', 'agent'))
        invocations = []
        handles = []

        def invoke(home, config, instruction, payload, schema, **kwargs):
            kwargs['session_dir'].mkdir(parents=True, exist_ok=True, mode=0o700)
            invocations.append((payload, kwargs['session_id']))
            handle = kwargs['session_id']
            if not handle:
                handle = '00000000-0000-4000-8000-' + f'{len(handles)+1:012d}'
                handles.append(handle)
            return ({'records': [], 'episode_summary': {'intent': None, 'open_work': []}},
                    {'input_tokens': 10 * len(invocations), 'output_tokens': len(invocations)}, handle)

        import agenthub.processing.episode_pipeline as episode_pipeline
        save = episode_pipeline._save_progress

        def interrupted_save(state, ident, progress, stage):
            if stage.startswith('curate:1/'):
                raise ValueError('synthetic_checkpoint_interrupt')
            return save(state, ident, progress, stage)

        with patch('agenthub.processing.harness._invoke', side_effect=invoke):
            first = Worker(self.store, self.config, runner=run_structured_session, live=False)
            with patch('agenthub.processing.episode_pipeline._save_progress', side_effect=interrupted_save):
                self.assertEqual(first.run(max_jobs=1, max_seconds=30)['completed'], 0)
            with self.store.open() as state:
                job = state.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
                error = state.db.execute('SELECT error FROM curation_episode_jobs').fetchone()[0]
                self.assertEqual(state.db.execute('SELECT count(*) FROM backend_provider_returns').fetchone()[0], 1, error)
            first.recover(job, retain_validated=True)
            resumed = Worker(self.store, self.config, runner=run_structured_session, live=False).run(
                max_jobs=1, max_calls=20, max_seconds=30, refresh_queue=False)
        self.assertEqual(resumed['completed'], 1)
        self.assertEqual([p['episode']['stage_index'] for p, _ in invocations],
                         list(range(len(invocations))))
        self.assertIsNone(invocations[1][1])
        self.assertNotEqual(handles[0], handles[1])
        self.assertIn('observer_earlier_stage_context', invocations[1][0])
        self.assertIn('stable header', json.dumps(invocations[1][0]['observer_earlier_stage_context']))
        self.assertEqual(resumed['calls'], len(invocations) - 1)


if __name__ == '__main__':
    unittest.main()
