"""Synthetic generation and continuing-media isolation checks."""
import json
import unittest
import test_backend_worker as fixtures
import tempfile
from pathlib import Path
from agentclient.enterprise_capture import normalize_capture
from agenthub.backend_worker import Worker


class MediaWorkerTests(unittest.TestCase):
    setUp=fixtures.WorkerTests.setUp
    tearDown=fixtures.WorkerTests.tearDown
    turn=fixtures.WorkerTests.turn

    def test_worker_cannot_claim_a_different_frozen_generation(self):
        first=Worker(self.store,self.config,live=False);first.prepare()
        newer=json.loads(json.dumps(self.config))
        newer['episode_curation']['generation_id']='newer-generation'
        worker=Worker(self.store,newer,live=False);worker.prepare();job=worker.claim()
        with self.store.open() as state:
            generation=state.db.execute('SELECT generation_id FROM curation_episode_jobs WHERE id=?',
                                        (job['episode_job'],)).fetchone()[0]
        self.assertEqual(generation,'newer-generation')
        self.assertIsNotNone(first.claim())

    def test_backend_default_is_gpt6_without_changing_explicit_or_client_profiles(self):
        from agenthub.cloud_local import worker_config
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(worker_config(Path(directory),provider_free=False)['observer']['model'],'gpt-6-luna')
            self.assertEqual(worker_config(Path(directory),provider_free=True)['observer']['model'],'fixture')
        self.config['observer'].pop('model')
        worker=Worker(self.store,self.config,live=False);worker.prepare()
        self.assertNotIn('model',self.config['observer'])
        self.assertEqual(worker.config['observer']['model'],'gpt-6-luna')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT model FROM backend_observers').fetchone()[0],'gpt-6-luna')

    def test_explicit_sample_filter_does_not_claim_an_unselected_job(self):
        self.turn('2')
        worker=Worker(self.store,self.config,live=False);worker.prepare()
        with self.store.open() as state:
            rows=state.db.execute('SELECT id,turn FROM curation_episode_jobs').fetchall()
            self.assertEqual(len(rows),2)
            selected=rows[-1]['id']
        self.assertIsNone(worker.claim(episode_job_ids=['unselected-fixture-id']))
        self.assertEqual(worker.claim(episode_job_ids=[selected])['episode_job'],selected)
        self.assertIsNone(worker.claim(episode_job_ids=[selected]))

    def test_media_context_continues_without_changing_original_source(self):
        encoded='data:image/png;base64,'+'ABCD'*20000
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostToolUse',
            'event_id':'media','session_id':'chat','turn_id':'1','tool_name':'exec_command',
            'tool_response':'Cobalt output: '+encoded+' Saved /data/cobalt.csv.','exit_code':0},
            'maple','agent'))
        self.config['episode_curation']['media_policy']='references-v1'
        sent=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            sent.append((payload,kwargs.get('session_id')))
            self.assertNotIn(encoded,json.dumps(payload))
            self.assertIn('not viewed or heard',instruction)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},\
                {'input_tokens':100,'cached_input_tokens':70},'00000000-0000-4000-8000-000000000001'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_jobs=1,max_seconds=20)['completed'],1)
        self.turn('2')
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_jobs=1,max_seconds=20)['completed'],1)
        self.assertEqual(sent[1][1],'00000000-0000-4000-8000-000000000001')
        self.assertTrue(any('/data/cobalt.csv' in span['text']
                            for span in sent[1][0]['previous_evidence_index']))
        with self.store.open() as state:
            self.assertTrue(any(encoded in row[0] for row in state.db.execute('SELECT body FROM memories')))


if __name__=='__main__':unittest.main()
