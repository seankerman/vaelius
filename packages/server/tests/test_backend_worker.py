import json
from pathlib import Path
import tempfile
import unittest
from agentclient.enterprise_capture import normalize_capture
from vaelius_test_support.fixtures.enterprise import EnterpriseStore


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=EnterpriseStore(Path(self.tmp.name)/'hub')
        self.store.create_organization('orchard');self.store.create_principal('orchard','alice')
        self.store.create_project('orchard','maple');self.store.set_membership('orchard','maple','alice',True)
        self.token=self.store.enroll('orchard','alice','device',['ingest','read','source_read','withdraw','policy'])
        self.ctx=self.store.authenticate(self.token)
        self.store.enroll_connection(self.ctx,'agent','codex','maple',['agent'])
        self.config={'paused':False,'knowledge_backend':{'mode':'enterprise_local'},
            'observer':{'enabled':True,'model':'fixture','min_interval_seconds':0},
            'episode_curation':{'enabled':True,'policy':'durable_memory','settle_seconds':0,'generation_id':'worker-fixture'}}
        self.turn('1')

    def tearDown(self):self.tmp.cleanup()

    def turn(self,turn):
        for kind in ('UserPromptSubmit','Stop'):
            self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':kind,
                'event_id':turn+kind,'session_id':'chat','turn_id':turn,
                'prompt':'Use CSV because it preserves a stable header.',
                'last_assistant_message':'The chosen CSV format retains the stable header.'},'maple','agent'))

    def test_bounded_observer_epoch_does_not_taint_unseen_later_turn(self):
        from agenthub.backend_worker import Worker
        from agentclient.enterprise_contract import VERSION
        seen=[]
        def runner(*args,**kwargs):
            seen.append(kwargs.get('session_id'))
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},{},'00000000-0000-4000-8000-000000000001'
        self.config['backend_worker']={'max_context_sources':2}
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_seconds=20)['completed'],1)
        with self.store.open() as state:
            first=state.db.execute('SELECT id,episode_job FROM backend_jobs').fetchone()
            source=state.db.execute('SELECT source_id FROM backend_processing_dependencies WHERE episode_job=?',(first['episode_job'],)).fetchone()[0]
        self.turn('2')
        self.assertEqual(worker.run(max_seconds=20)['completed'],1)
        self.assertEqual(seen,[None,None])
        with self.store.open() as state:
            later=state.db.execute('SELECT id,episode_job FROM backend_jobs WHERE id!=?',(first['id'],)).fetchone()
            self.assertFalse(state.db.execute('SELECT 1 FROM backend_processing_dependencies WHERE episode_job=? AND source_id=?',(later['episode_job'],source)).fetchone())
        self.store.lifecycle(self.ctx,dict(version=VERSION,target_id=source,expected_revision='1',
            operation='withdraw',idempotency_key='epoch-withdraw',reason='fixture'))
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs WHERE id=?',(later['id'],)).fetchone()[0],'complete')

    def test_legacy_saved_progress_does_not_claim_complete_dependency_snapshots(self):
        from agenthub.backend_worker import Worker
        worker=Worker(self.store,self.config,live=False);worker.prepare()
        with self.store.open() as state,state.db:
            state.db.execute('DELETE FROM backend_jobs')
            state.db.execute('UPDATE curation_episode_jobs SET progress=?',
                (json.dumps({'resolutions':[{'legacy':'saved model result'}]}),))
        worker.prepare()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT dependency_snapshot_complete FROM backend_jobs').fetchone()[0],0)

    def test_source_rederivation_preserves_denial_and_uses_only_remaining_originals(self):
        from agenthub.backend_worker import Worker
        from agenthub.historical_review import review_generation
        from agentclient.enterprise_contract import VERSION
        calls=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            calls.append((config['_purpose'],payload,kwargs.get('session_id')))
            if config['_purpose']=='episode_resolve':
                return {'candidate_key':payload['candidate']['candidate_key'],'operation':'CREATE',
                    'target_artifact_id':'','reason':'new_claim'},{}
            event=next(e for e in payload['episode']['events'] if e['kind']=='UserPromptSubmit')
            return {'records':[{'title':'CSV rationale','text':'The user chose CSV because its header remains stable.',
                'subject':'CSV','facets':['decision'],'actors':['user'],'artifact':None,'rationale':None,
                'state':'reported','occurred_date':'','event_id':event['event_id'],
                'evidence_span_ids':[event['spans'][0]['span_id']]}],
                'episode_summary':{'intent':None,'open_work':[]}},{},'00000000-0000-4000-8000-000000000001'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_seconds=25)['completed'],1)
        self.turn('2');self.assertEqual(worker.run(max_seconds=25)['completed'],1)
        review_generation(self.store,'worker-fixture',allowed_connections=['agent'],finalize=True)
        with self.store.open() as state:
            jobs=state.db.execute('SELECT * FROM backend_jobs ORDER BY created,id').fetchall()
            first,later=jobs
            source=state.db.execute('SELECT source_id FROM backend_processing_dependencies WHERE episode_job=?',(first['episode_job'],)).fetchone()[0]
            olddoc=state.db.execute('SELECT document_id FROM episode_candidates WHERE job_id=?',(later['episode_job'],)).fetchone()[0]
            self.assertTrue(state.db.execute('SELECT 1 FROM enterprise_dependencies WHERE document_id=? AND source_id=?',(olddoc,source)).fetchone())
        self.store.lifecycle(self.ctx,dict(version=VERSION,target_id=source,expected_revision='1',
            operation='withdraw',idempotency_key='rederive-withdraw',reason='fixture'))
        with self.assertRaises(ValueError):worker.recover(first['id'],rederive=True)
        worker.recover(later['id'],rederive=True);calls.clear()
        self.assertEqual(worker.run(max_seconds=25)['completed'],1)
        curated=next(c for c in calls if c[0]=='durable_memory_curate')
        self.assertIsNone(curated[2]);self.assertFalse(curated[1].get('previous_evidence_index'))
        with self.store.open() as state:
            self.assertFalse(state.db.execute('SELECT 1 FROM backend_processing_dependencies WHERE episode_job=? AND source_id=?',(later['episode_job'],source)).fetchone())
            self.assertTrue(state.db.execute('SELECT 1 FROM enterprise_dependencies WHERE document_id=? AND source_id=?',(olddoc,source)).fetchone())
            docs=[r[0] for r in state.db.execute('SELECT document_id FROM episode_candidates WHERE job_id=? UNION SELECT document_id FROM episode_candidate_history WHERE job_id=?',(later['episode_job'],later['episode_job']))]
            self.assertGreater(len(set(docs)),1)
            for doc in set(docs)-{olddoc}:
                self.assertFalse(state.db.execute('SELECT 1 FROM enterprise_dependencies WHERE document_id=? AND source_id=?',(doc,source)).fetchone())
        from agenthub.enterprise import Denied
        with self.assertRaises(Denied):self.store.detail(self.ctx,olddoc)
        for doc in set(docs)-{olddoc}:self.assertTrue(self.store.detail(self.ctx,doc))

    def test_lease_excludes_duplicate_worker_and_fences_late_completion(self):
        from agenthub.backend_worker import Worker
        worker=Worker(self.store,self.config,live=False)
        worker.prepare();job=worker.claim(lease_seconds=5)
        self.assertIsNotNone(job)
        self.assertIsNone(worker.claim())
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE backend_jobs SET lease_until=0')
        newer=worker.claim()
        self.assertNotEqual(job['fence'],newer['fence'])
        with self.store.open() as state:
            with self.assertRaises(ValueError):worker.guard(state.db,job)
        self.assertEqual(newer['recovery'],'rebuild_uncertain_session')

    def test_reviewed_large_episode_preserves_frozen_definition_and_source_binding(self):
        from agenthub.backend_worker import Worker
        self.config['episode_curation'].update(max_stages=2,max_chars_per_stage=2000,
            split_oversized_events=True)
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostToolUse',
            'event_id':'long-tool','session_id':'chat','turn_id':'1','tool_name':'exec_command',
            'tool_response':{'output':'Cobalt verification completed.\n'*200,'exit_code':0}},'maple','agent'))
        calls=[]
        def runner(*args,**kwargs):
            calls.append(args[3]['episode']['stage_index'])
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},{},None
        worker=Worker(self.store,self.config,runner=runner,live=False)
        worker.run(max_jobs=1,max_calls=32,max_seconds=15)
        with self.store.open() as state:
            job=state.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
            row=state.db.execute('SELECT status,error FROM curation_episode_jobs').fetchone()
            self.assertEqual((row['status'],row['error']),('held','memory_stage_count_exceeds_limit'))
            frozen=dict(state.db.execute('SELECT * FROM knowledge_generations').fetchone())
        self.assertFalse(calls)
        worker.recover(job,retain_validated=True,reviewed_stage_limit=16)
        with self.store.open() as state,state.db:
            saved=state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0]
            corrupted=json.loads(saved);corrupted['reviewed_stage_limit']['source_snapshot_hash']='0'*64
            state.db.execute('UPDATE curation_episode_jobs SET progress=?',(json.dumps(corrupted),))
        worker.run(max_jobs=1,max_calls=32,max_seconds=15)
        self.assertFalse(calls)
        with self.store.open() as state,state.db:
            self.assertEqual(state.db.execute('SELECT error FROM curation_episode_jobs').fetchone()[0],
                'reviewed_stage_source_changed')
            state.db.execute('UPDATE curation_episode_jobs SET progress=?',(saved,))
        worker.recover(job,retain_validated=True)
        worker.run(max_jobs=1,max_calls=32,max_seconds=15)
        self.assertGreater(len(calls),2)
        with self.store.open() as state:
            self.assertEqual(dict(state.db.execute('SELECT * FROM knowledge_generations').fetchone()),frozen)
            self.assertEqual(state.db.execute('SELECT status FROM curation_episode_jobs').fetchone()[0],'no_learning')
            progress=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertEqual(progress['reviewed_stage_limit']['limit'],16)
            self.assertTrue(progress['reviewed_stage_limit']['source_snapshot_hash'])

    def test_partial_capture_requires_opt_in_and_retains_coverage_gap(self):
        from agenthub.backend_worker import Worker
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'UnsupportedHostItem',
            'event_id':'missing-pixels','session_id':'chat','turn_id':'1'},'maple','agent'))
        strict=Worker(self.store,self.config,live=False);strict.prepare()
        with self.store.open() as state:
            episode=state.db.execute('SELECT id,status,error,project,session,turn FROM curation_episode_jobs').fetchone()
            self.assertEqual((episode['status'],episode['error']),('held','incomplete_capture'))
        self.config['backend_worker']={'allow_partial_capture':True}
        from agenthub.processing.episode_pipeline import retry_held_job
        with self.store.open() as state:retry_held_job(state.db,episode['id'])
        calls=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            if config['_purpose']=='episode_resolve':
                return {'candidate_key':payload['candidate']['candidate_key'],
                    'operation':'CREATE','target_artifact_id':'','reason':'new_claim'},{}
            calls.append((instruction,payload))
            event=next(e for e in payload['episode']['events'] if e['kind']=='UserPromptSubmit')
            return {'records':[{'title':'Maple CSV rationale',
                'text':'The user chose CSV because its header remains stable.',
                'subject':'CSV','facets':['decision'],'actors':['user'],
                'artifact':None,'rationale':{'actor':'user','quote':'it preserves a stable header.'},
                'state':'reported','occurred_date':'',
                'event_id':event['event_id'],'evidence_span_ids':[event['spans'][0]['span_id']]}],
                'episode_summary':{'intent':None,'open_work':[]}},{},None
        result=Worker(self.store,self.config,runner=runner,live=False).run(max_jobs=1,max_calls=4,max_seconds=30)
        self.assertEqual(result['completed'],1)
        self.assertTrue(calls)
        self.assertEqual(calls[0][1]['source_capture']['completion'],'partial')
        with self.store.open() as state:
            progress=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertEqual(progress['coverage']['source_capture']['completion'],'partial')
            self.assertEqual(progress['coverage']['completion'],'partial')
            from agenthub.processing.episode_pipeline import get_episode_view
            for mode in ('current','history'):
                view=get_episode_view(state.db,'worker-fixture',episode['project'],
                    episode['session'],episode['turn'],include_building=True,mode=mode,source_authorizer=lambda _:True)
                self.assertIn('incomplete_source_capture',view['coverage_gaps'])
            self.assertEqual(state.db.execute("SELECT count(*) FROM memories WHERE kind='gap' AND active=1").fetchone()[0],1)
        from agenthub.historical_review import review_generation
        reviewed=review_generation(self.store,'worker-fixture',allowed_connections=['agent'],finalize=True)
        self.assertEqual(reviewed['processed_partial_capture_jobs'],1)
        self.assertEqual(reviewed['processed_fully_captured_jobs'],0)

    def test_dispatch_crash_does_not_blindly_resume(self):
        from agenthub.backend_worker import Worker
        worker=Worker(self.store,self.config,live=False);worker.prepare();job=worker.claim()
        worker.mark_dispatch(job,'attempt-fixture')
        with self.store.open() as state,state.db:state.db.execute('UPDATE backend_jobs SET lease_until=0')
        replacement=Worker(EnterpriseStore(self.store.home),self.config,live=False).claim()
        self.assertEqual(replacement['recovery'],'rebuild_uncertain_session')

    def test_recovery_cannot_reset_an_observer_used_by_another_live_job(self):
        from agenthub.backend_worker import Worker
        worker=Worker(self.store,self.config,live=False)
        worker.prepare();older=worker.claim()
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE backend_jobs SET status='held',lease_until=0 WHERE id=?",(older['id'],))
            state.db.execute("UPDATE curation_episode_jobs SET status='held',error='fixture_hold' WHERE id=?",(older['episode_job'],))
        self.turn('2');worker.prepare();newer=worker.claim()
        self.assertEqual(older['observer_id'],newer['observer_id'])
        with self.store.open() as state:
            before=dict(state.db.execute('SELECT * FROM backend_observers WHERE id=?',(older['observer_id'],)).fetchone())
        with self.assertRaisesRegex(ValueError,'observer_still_leased'):
            worker.recover(older['id'],retain_validated=True)
        with self.store.open() as state:
            self.assertEqual(before,dict(state.db.execute('SELECT * FROM backend_observers WHERE id=?',(older['observer_id'],)).fetchone()))
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs WHERE id=?',(older['id'],)).fetchone()[0],'held')

    def test_reviewed_reprocessing_changes_request_and_preserves_first_pass(self):
        from agenthub.backend_worker import Worker
        calls=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            calls.append(payload)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},{},None
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_jobs=1,max_calls=2,max_seconds=15)['completed'],1)
        with self.store.open() as state:
            job=state.db.execute('SELECT id FROM backend_jobs').fetchone()[0]
            original=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])['extraction']
        worker.recover(job,reprocess=True,retain_validated=False,reviewed_reason='missing_durable_fact')
        self.assertEqual(worker.run(max_jobs=1,max_calls=2,max_seconds=15)['completed'],1)
        self.assertNotEqual(calls[0],calls[1])
        self.assertEqual(calls[1]['reviewed_reprocessing']['reason'],'missing_durable_fact')
        with self.store.open() as state:
            progress=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs').fetchone()[0])
            self.assertIn(original,progress['extraction_history'])

    def test_no_learning_continues_one_session_across_restart(self):
        from agenthub.backend_worker import Worker
        sessions=[];payloads=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            payloads.append(payload);sessions.append(kwargs.get('session_id'))
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {'input_tokens':10}, '00000000-0000-4000-8000-000000000001'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_jobs=1,max_calls=3,max_seconds=10)['completed'],1)
        self.turn('2')
        worker=Worker(EnterpriseStore(self.store.home),self.config,runner=runner,live=False)
        self.assertEqual(worker.run(max_jobs=1,max_calls=3,max_seconds=10)['completed'],1)
        self.assertEqual(sessions,[None,'00000000-0000-4000-8000-000000000001'])
        self.assertIn('previous_evidence_index',payloads[1])
        self.assertEqual(len(payloads[1]['episode']['events']),2)

    def test_bounded_staged_job_resumes_same_session_without_retrying_saved_stages(self):
        from agenthub.backend_worker import Worker
        self.config['episode_curation'].update(split_oversized_events=True,max_chars_per_stage=2000)
        self.config['backend_worker']={'resume_bounded_jobs':True,'compact_after_chars':1_000_000}
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostToolUse',
            'event_id':'large-tool','session_id':'chat','turn_id':'1','tool_name':'exec_command',
            'tool_response':'Cobalt retry verified.\n'*300,'exit_code':0},'maple','agent'))
        sessions=[]; stages=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            sessions.append(kwargs.get('session_id'));stages.append(payload['episode']['stage_index'])
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},\
                {'input_tokens':10,'cached_input_tokens':7},'00000000-0000-4000-8000-000000000001'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        first=worker.run(max_jobs=1,max_calls=1,max_retries=0,max_seconds=30)
        self.assertEqual((first['calls'],first['completed']),(1,0))
        with self.store.open() as state:
            job=state.db.execute('SELECT status,last_error FROM backend_jobs').fetchone()
            observer=state.db.execute('SELECT provider_session,observer_epoch FROM backend_observers').fetchone()
            self.assertEqual((job['status'],job['last_error']),('pending',None))
            self.assertEqual(observer['provider_session'],'00000000-0000-4000-8000-000000000001')
            self.assertEqual(observer['observer_epoch'],0)
        second=Worker(self.store,self.config,runner=runner,live=False).run(
            max_jobs=1,max_calls=16,max_retries=0,max_seconds=30,refresh_queue=False)
        self.assertEqual(second['completed'],1)
        self.assertEqual(stages,list(range(len(stages))))
        self.assertEqual(sessions,[None]+['00000000-0000-4000-8000-000000000001']*(len(sessions)-1))

    def test_saved_output_recovers_after_install_failure_without_another_dispatch(self):
        from agenthub.backend_worker import Worker
        from unittest.mock import patch
        calls=[]
        def runner(*args,**kwargs):
            calls.append(1)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},{},'00000000-0000-4000-8000-000000000001'
        worker=Worker(self.store,self.config,runner=runner,live=False)
        with patch('agenthub.processing.episode_pipeline._install',side_effect=ValueError('install_fault')):
            self.assertEqual(worker.run(max_seconds=10)['completed'],0)
        with self.store.open() as state:
            job=state.db.execute("SELECT id FROM backend_jobs WHERE status='held'").fetchone()[0]
            episode=state.db.execute("SELECT status,error FROM curation_episode_jobs").fetchone()
            self.assertEqual((episode['status'],episode['error']),('held','install_fault'))
        worker.recover(job,retain_validated=True)
        result=worker.run(max_seconds=10)
        self.assertEqual(result['completed'],1);self.assertEqual(result['calls'],0);self.assertEqual(len(calls),1)

    def test_time_reserve_checkpoints_once_instead_of_reclaiming_same_job(self):
        from agenthub.backend_worker import Worker
        self.config['backend_worker']={'resume_bounded_jobs':True}
        def runner(*args,**kwargs):raise ValueError('worker_time_reserve')
        worker=Worker(self.store,self.config,runner=runner,live=False)
        result=worker.run(max_jobs=4,max_calls=5,max_seconds=30)
        self.assertEqual(result['processed'],1)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT status FROM backend_jobs').fetchone()[0],'pending')

    def test_reviewed_worker_hold_becomes_explicit_unprocessed_gap(self):
        from agenthub.backend_worker import Worker
        from agenthub.processing.episode_pipeline import activate_generation, skip_held_job
        from unittest.mock import patch
        def runner(*args,**kwargs):
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, None
        with patch('agenthub.processing.episode_pipeline._install',side_effect=ValueError('install_fault')):
            self.assertEqual(Worker(self.store,self.config,runner=runner,live=False).run(
                max_jobs=1,max_calls=2,max_seconds=10)['completed'],0)
        with self.store.open() as state,state.db:
            episode=state.db.execute('SELECT id,status,error FROM curation_episode_jobs').fetchone()
            self.assertEqual((episode['status'],episode['error']),('held','install_fault'))
            gap=skip_held_job(state.db,episode['id'])
            self.assertEqual((gap['status'],gap['error']),('unprocessed','install_fault'))
            self.assertEqual(activate_generation(state.db,'worker-fixture')['status'],'active')

    def test_historical_review_requires_exact_scope_and_explicit_finalize(self):
        from agenthub.backend_worker import Worker
        from agenthub.historical_review import review_generation
        from unittest.mock import patch
        def runner(*args,**kwargs):
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, None
        with patch('agenthub.processing.episode_pipeline._install',side_effect=ValueError('install_fault')):
            Worker(self.store,self.config,runner=runner,live=False).run(
                max_jobs=1,max_calls=2,max_seconds=10)
        with self.assertRaisesRegex(ValueError,'historical_review_scope'):
            review_generation(self.store,'worker-fixture',allowed_connections=['other'])
        preview=review_generation(self.store,'worker-fixture',allowed_connections=['agent'])
        self.assertEqual((preview['status'],preview['held_reasons']),
                         ('building',{'install_fault':1}))
        applied=review_generation(self.store,'worker-fixture',
                                  allowed_connections=['agent'],finalize=True)
        self.assertEqual((applied['status'],applied['gaps_marked']),('active',1))
        self.assertEqual(review_generation(self.store,'worker-fixture',
                         allowed_connections=['agent'],finalize=True)['gaps_marked'],0)

    def test_historical_review_preserves_capture_hold_without_worker_job(self):
        from agenthub.backend_worker import Worker
        from agenthub.historical_review import review_generation
        Worker(self.store,self.config,live=False).prepare()
        with self.store.open() as state,state.db:
            state.db.execute('DELETE FROM backend_jobs')
            state.db.execute("UPDATE curation_episode_jobs SET status='held',error='incomplete_capture'")
        with self.assertRaisesRegex(ValueError,'historical_review_scope'):
            review_generation(self.store,'worker-fixture',allowed_connections=['another'])
        preview=review_generation(self.store,'worker-fixture',allowed_connections=['agent'])
        self.assertEqual(preview['held_reasons'],{'incomplete_capture':1})
        applied=review_generation(self.store,'worker-fixture',
                                  allowed_connections=['agent'],finalize=True)
        self.assertEqual((applied['status'],applied['gaps_marked']),('active',1))
        with self.store.open() as state:
            row=state.db.execute('SELECT status,error FROM curation_episode_jobs').fetchone()
            self.assertEqual((row['status'],row['error']),('unprocessed','incomplete_capture'))

    def test_historical_activation_registers_source_backed_documents_for_delivery(self):
        from agenthub.backend_worker import Worker
        from agenthub.historical_review import review_generation
        def runner(home,config,instruction,payload,schema,**kwargs):
            if config['_purpose']=='episode_resolve':
                return {'candidate_key':payload['candidate']['candidate_key'],
                        'operation':'CREATE','target_artifact_id':'','reason':'new_claim'}, {}
            event=next(e for e in payload['episode']['events'] if e['kind']=='UserPromptSubmit')
            return ({'records':[{'title':'Maple CSV rationale',
                'text':'The user chose CSV because its header remains stable.',
                'subject':'CSV','facets':['decision'],'actors':['user'],
                'artifact':None,'rationale':None,'state':'reported','occurred_date':'',
                'event_id':event['event_id'],
                'evidence_span_ids':[event['spans'][0]['span_id']]}],
                'episode_summary':{'intent':None,'open_work':[]}}, {},
                '00000000-0000-4000-8000-000000000001')
        self.assertEqual(Worker(self.store,self.config,runner=runner,live=False).run(
            max_jobs=1,max_calls=4,max_seconds=15)['completed'],1)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_documents').fetchone()[0],0)
        from unittest.mock import patch
        # Activation must register this generation in bounded transactions;
        # a corpus-wide registration can time out while inserting its source links.
        with patch.object(self.store, 'refresh_documents', wraps=self.store.refresh_documents) as refresh:
            review_generation(self.store,'worker-fixture',allowed_connections=['agent'],finalize=True)
        self.assertTrue(refresh.call_args_list)
        self.assertTrue(all(call.kwargs.get('document_id') for call in refresh.call_args_list))
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM enterprise_documents').fetchone()[0],1)
            self.assertGreater(state.db.execute('SELECT count(*) FROM enterprise_dependencies').fetchone()[0],0)

    def test_registration_failure_rolls_back_curated_claim_with_worker_install(self):
        from agenthub.backend_worker import Worker
        from unittest.mock import patch
        def runner(home,config,instruction,payload,schema,**kwargs):
            if config['_purpose']=='episode_resolve':
                return {'candidate_key':payload['candidate']['candidate_key'],
                    'operation':'CREATE','target_artifact_id':'','reason':'new_claim'},{}
            event=next(e for e in payload['episode']['events'] if e['kind']=='UserPromptSubmit')
            return ({'records':[{'title':'Maple CSV rationale',
                'text':'The user chose CSV because its header remains stable.',
                'subject':'CSV','facets':['decision'],'actors':['user'],
                'artifact':None,'rationale':None,'state':'reported','occurred_date':'',
                'event_id':event['event_id'],
                'evidence_span_ids':[event['spans'][0]['span_id']]}],
                'episode_summary':{'intent':None,'open_work':[]}}, {},
                '00000000-0000-4000-8000-000000000001')
        def fail_registration(*,db=None,document_id=None):
            if db is not None:raise RuntimeError('registration_fault')
            return 0
        with patch.object(self.store,'refresh_documents',side_effect=fail_registration):
            result=Worker(self.store,self.config,runner=runner,live=False).run(
                max_jobs=1,max_calls=4,max_seconds=15)
        self.assertEqual(result['completed'],0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM episode_candidates WHERE status=\'applied\'').fetchone()[0],0)

    def test_revocation_during_dispatch_prevents_commit_and_resume(self):
        from agenthub.backend_worker import Worker
        def runner(home,config,instruction,payload,schema,**kwargs):
            self.store.connection_policy(self.ctx,'agent',active=False)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {}, '00000000-0000-4000-8000-000000000001'
        result=Worker(self.store,self.config,runner=runner,live=False).run(max_jobs=1,max_calls=3,max_seconds=10)
        self.assertEqual(result['completed'],0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM episode_candidates').fetchone()[0],0)
            self.assertIsNone(state.db.execute('SELECT provider_session FROM backend_observers').fetchone()[0])

    def test_early_rationale_input_only_location_and_observer_compaction(self):
        from agenthub.backend_worker import Worker
        sessions=[]
        def runner(home,config,instruction,payload,schema,**kwargs):
            if config['_purpose']=='episode_resolve':
                return {'candidate_key':payload['candidate']['candidate_key'],'operation':'CREATE',
                    'target_artifact_id':'','reason':'new_claim'},{}
            sessions.append(kwargs.get('session_id'))
            event=next(e for e in payload['episode']['events'] if e['kind']=='UserPromptSubmit')
            previous=payload.get('previous_evidence_index',[])
            tool=next((e for e in payload['episode']['events'] if e.get('tool_name')=='save_file'),None)
            if tool:
                anchor=tool;span=tool['spans'][0]['span_id']
                reason=next(e for e in previous if 'because' in e['text'] and e['kind']=='UserPromptSubmit')
                record={'title':'Maple CSV artifact','text':'Maple dataset saved at /Users/demo/My Data/maple.csv because it preserves a stable header.',
                    'subject':'CSV','facets':['activity','decision'],'actors':['user','agent'],
                    'artifact':{'name':'maple.csv','location':'/Users/demo/My Data/maple.csv'},
                    'rationale':{'actor':'user','quote':'because it preserves a stable header.'},
                    'state':'observed','occurred_date':'','event_id':anchor['event_id'],
                    'evidence_span_ids':[span,reason['span_id']]}
            else:
                record={'title':'CSV rationale','text':'User chose CSV because it preserves a stable header.',
                    'subject':'CSV','facets':['decision'],'actors':['user'],'artifact':None,
                    'rationale':{'actor':'user','quote':'because it preserves a stable header.'},
                    'state':'reported','occurred_date':'','event_id':event['event_id'],
                    'evidence_span_ids':[event['spans'][0]['span_id']]}
            return {'records':[record],'episode_summary':{'intent':None,'open_work':[]}},{},'00000000-0000-4000-8000-000000000001'
        result=Worker(self.store,self.config,runner=runner,live=False).run(max_jobs=1,max_calls=4,max_seconds=10)
        self.assertEqual(result['completed'],1)
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostCompact',
            'event_id':'boundary','session_id':'chat','turn_id':'boundary'},'maple','agent'))
        self.turn('2')
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'PostToolUse',
            'event_id':'save','session_id':'chat','turn_id':'2','tool_name':'save_file','tool_use_id':'save',
            'tool_input':{'path':'/Users/demo/My Data/maple.csv'},'tool_response':{'status':'saved'},'exit_code':0},'maple','agent'))
        config=dict(self.config,backend_worker={'compact_after_chars':1})
        result=Worker(EnterpriseStore(self.store.home),config,runner=runner,live=False).run(max_jobs=1,max_calls=4,max_seconds=10)
        self.assertEqual(result['completed'],1)
        self.assertEqual(sessions,[None,None])
        from agenthub.processing.episode_pipeline import activate_generation
        with self.store.open() as state:activate_generation(state.db,'worker-fixture')
        self.store.refresh_documents()
        with self.store.open() as state:
            candidate=json.loads(state.db.execute('SELECT candidate_json FROM episode_candidates ORDER BY created DESC LIMIT 1').fetchone()[0])
            self.assertEqual(candidate['memory_record']['location'],'/Users/demo/My Data/maple.csv')
            self.assertEqual(candidate['memory_record']['reason_actor'],'user')
            self.assertEqual(len(candidate['memory_record']['evidence_span_ids']),2)
            observer=state.db.execute('SELECT * FROM backend_observers').fetchone()
            self.assertEqual(observer['source_epoch'],1);self.assertGreater(observer['observer_epoch'],0)
            doc=state.db.execute('SELECT document_id FROM episode_candidates ORDER BY created DESC LIMIT 1').fetchone()[0]
            self.assertGreaterEqual(state.db.execute('SELECT count(*) FROM enterprise_dependencies WHERE document_id=?',(doc,)).fetchone()[0],5)

    def test_original_commentary_and_tool_metadata_reach_curation(self):
        from agenthub.backend_worker import Worker
        from agenthub.processing.episode_pipeline import sources_for_job
        from agenthub.processing.durable_memory import packet_for
        commentary = self.store.ingest_general(self.ctx, normalize_capture({
            'hook_event_name':'AssistantMessage','event_id':'commentary','session_id':'chat',
            'turn_id':'1','source_order':1,'channel':'commentary','speaker':'agent',
            'parent_id':'parent','message':'I recommend CSV because the header is stable.'},'maple','agent'))
        tool = self.store.ingest_general(self.ctx, normalize_capture({
            'hook_event_name':'PostToolUse','event_id':'tool','session_id':'chat',
            'turn_id':'1','source_order':2,'tool_use_id':'call','parent_id':'parent',
            'tool_name':'save_file','tool_input':{'path':'/tmp/maple.csv'},
            'tool_response':{'status':'saved'},'exit_code':0},'maple','agent'))
        Worker(self.store,self.config,live=False).prepare()
        with self.store.open() as state:
            job=state.db.execute('SELECT * FROM curation_episode_jobs').fetchone()
            sources=sources_for_job(state.db,job)
        by_id={s['id']:s for s in sources}
        self.assertEqual(by_id[commentary['source_id']]['kind'],'AssistantMessage')
        packet=packet_for(sources);events={e['event_id']:e for e in packet['episode']['events']}
        self.assertEqual(events[commentary['source_id']]['role'],'assistant')
        self.assertEqual(events[commentary['source_id']]['channel'],'commentary')
        self.assertEqual(events[commentary['source_id']]['actor'],'agent')
        self.assertEqual(events[tool['source_id']]['call_id'],'call')
        self.assertEqual(events[tool['source_id']]['parent_id'],'parent')

    def test_expected_capture_inventory_includes_boundaries_and_intentional_exclusions(self):
        from agenthub.backend_worker import Worker
        for event in ({'hook_event_name':'PostCompact','event_id':'compact'},
                      {'hook_event_name':'PostToolUse','event_id':'self','tool_name':'agentnetwork_memory'}):
            self.store.ingest_general(self.ctx,normalize_capture(dict(event,session_id='chat',turn_id='1'),'maple','agent'))
        self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':'Stop','event_id':'inventory-stop',
            'session_id':'chat','turn_id':'1','last_assistant_message':'The captured turn is complete.',
            'expected_events':['1UserPromptSubmit','1Stop','compact','self','inventory-stop']},'maple','agent'))
        worker=Worker(self.store,self.config,live=False);worker.prepare()
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_jobs').fetchone()[0],1)
            self.assertEqual(state.db.execute('SELECT status FROM curation_episode_jobs').fetchone()[0],'pending')

    def test_stateless_api_reconstructs_prior_turn_after_restart(self):
        from agenthub.backend_worker import Worker
        from agenthub.cloud_execution import ApiExecution
        packets=[]
        def send(request,timeout):
            packets.append(json.loads(request['input'][1]['content']))
            result={'records':[],'episode_summary':{'intent':None,'open_work':[]}}
            return {'id':'response-not-a-session','status':'completed','output':[{'type':'message',
                'content':[{'type':'output_text','text':json.dumps(result)}]}]}
        config=dict(self.config,backend_execution={'kind':'operator_api','model':'fixture'})
        api=ApiExecution(model='fixture',api_key='fixture',send=send)
        worker=Worker(self.store,config,runner=api,live=True)
        self.assertEqual(worker.run(max_jobs=1,max_calls=3,max_seconds=120)['completed'],1)
        self.turn('2')
        worker=Worker(EnterpriseStore(self.store.home),config,runner=api,live=True)
        self.assertEqual(worker.run(max_jobs=1,max_calls=3,max_seconds=120)['completed'],1)
        self.assertTrue(packets[1]['previous_evidence_index'])
        self.assertIn('text',json.dumps(packets[1]['previous_evidence_index']))
        with self.store.open() as state:
            self.assertIsNone(state.db.execute('SELECT provider_session FROM backend_observers').fetchone()[0])
            self.assertEqual(state.db.execute("SELECT count(*) FROM cloud_usage_attempts WHERE status='returned'").fetchone()[0],2)

    def test_provider_switch_fences_old_worker_and_discards_opaque_handle(self):
        from agenthub.backend_worker import Worker
        old=Worker(self.store,self.config,live=False);old.prepare();job=old.claim()
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE backend_observers SET provider_session='opaque-local-handle'")
        changed=dict(self.config,backend_execution={'kind':'operator_api','model':'fixture'})
        new=Worker(self.store,changed,live=False);new.prepare()
        with self.store.open() as state:
            observer=state.db.execute('SELECT * FROM backend_observers').fetchone()
            self.assertIsNone(observer['provider_session'])
            self.assertEqual(observer['execution_key'],new.execution_key)
            with self.assertRaisesRegex(ValueError,'observer_execution_changed'):old.guard(state.db,job)
