"""A nearly exhausted wave must not dispatch an artificially shortened call."""
import json
from unittest.mock import patch
from test_backend_worker import WorkerTests
from agenthub.backend_worker import Worker
from agentclient.enterprise_capture import normalize_capture
from agenthub.processing.harness import HarnessError


class DeadlineTests(WorkerTests):
    def test_full_native_call_window_is_reserved_before_dispatch(self):
        self.config['backend_execution']={'kind':'codex'}
        self.config['observer']['timeout_seconds']=60
        self.config['episode_curation'].update(split_oversized_events=True,max_chars_per_stage=2000)
        self.config['backend_worker']={'resume_bounded_jobs':True}
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostToolUse',
            'event_id':'long-tool','session_id':'chat','turn_id':'1','tool_name':'exec_command',
            'tool_response':'Cobalt retry verified.\n'*300,'exit_code':0},'maple','agent'))
        clock=[0];calls=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            calls.append(config['observer']['timeout_seconds'])
            if len(calls)>1:raise HarnessError('harness_timeout')
            clock[0]=95
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, 'fixture-session'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        with patch('agenthub.backend_worker.time.monotonic',side_effect=lambda:clock[0]):
            result=worker.run(max_jobs=1,max_calls=8,max_retries=0,max_seconds=120)
        self.assertEqual((result['calls'],result['completed']),(1,0))
        self.assertEqual(calls,[60])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs').fetchone()[0],'pending')
            p=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertEqual(len(p['stage_outputs']),1)
