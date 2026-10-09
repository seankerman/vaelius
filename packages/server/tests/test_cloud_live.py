"""Provider-free preflight for the frozen cloud live orchestration."""
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vaelius_test_support.hub.cloud_live import fixture,_receive_args,_parse_events,verify,_save,_same_core_files,_retry_core_identity,_retry_current_state,RETRY_ORIGINAL_BUILD_IDS,retry_receiver
from agenthub.postgres import PostgresEnterpriseStore
from test_cloud_postgres import PostgresFixture,SERVICES


class ReceiverContractTests(unittest.TestCase):
    def test_receiver_retry_preserves_fresh_virtualenv_configured_interpreter(self):
        # Reuse the already-frozen installed interpreter fixture. A real fresh
        # venv reproduces the same symlink shape as the original B11 runtime.
        path=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_rollback_interpreter_v1.json'
        self.assertEqual(json.loads(path.read_text())['provider_calls'],0)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();previous=root/'installed-b11';profile=root/'profile';profile.mkdir()
            subprocess.run([sys.executable,'-m','venv','--without-pip',str(previous)],check=True,capture_output=True)
            python=previous/'bin/python';self.assertTrue(python.is_symlink())
            info=json.loads(subprocess.run([str(python),'-c','import json,sys; print(json.dumps({"interpreter":sys.executable,"prefix":sys.prefix}))'],
                check=True,capture_output=True,text=True).stdout)
            self.assertEqual(Path(info['prefix']),previous)
            self.assertNotEqual(python.resolve().parent.parent,previous)
            old_dir=previous/'lib'/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'site-packages/agenthub'
            now=root/'current-package';old_dir.mkdir(parents=True);now.mkdir()
            (old_dir/'__init__.py').write_text('');(old_dir/'cloud_live.py').write_text('old')
            files={name:hashlib.sha256((old_dir/name).read_bytes()).hexdigest() for name in ('__init__.py','cloud_live.py')}
            build=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
            _save(old_dir/'BUILD_ID.json',{'build_id':build,'files':files})
            _save(now/'BUILD_ID.json',{'files':{**files,'cloud_live.py':'new'}})
            failed={'runtime':{'interpreter':info['interpreter'],'modules':{'agenthub':{'path':str(old_dir/'__init__.py')}},
                'build_ids':{'agenthub':build,'agentclient':'unchanged'}}}
            with patch('agenthub.__file__',str(now/'__init__.py')):
                _retry_core_identity(profile,failed,{'build_ids':{'agentclient':'unchanged'}})

    def test_receiver_code_mode_remains_enabled_for_allowlisted_mcp(self):
        path=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_receiver_retry_v1.json'
        raw=path.read_bytes();manifest=json.loads(path.with_name(path.stem+'_manifest.json').read_text())
        self.assertEqual(manifest['sha256'],'85d2846363a16223222c35c8784b569fbd244c777251553bfdf240cb04fd916d')
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['sha256'])
        case=json.loads(raw);args=_receive_args('/codex','/python','/client','project',Path('/work'))
        disabled=[args[i+1] for i,value in enumerate(args[:-1]) if value=='--disable']
        self.assertNotIn('code_mode_host',disabled)
        self.assertIn('features.code_mode_host=true',args)
        self.assertTrue(set(case['forbidden'])-{'web_search'}<=set(disabled))
        self.assertIn('web_search="disabled"',args)
        self.assertIn('mcp_servers.cloud-live.enabled_tools=["search_memory"]',args)

    def test_receiver_retry_only_allows_authorized_harness_and_new_fixture_changes(self):
        previous={'cloud_live.py':'old','backend_worker.py':'unchanged','CLIENT_PIPELINE_PIN.json':'unchanged','migrations/019.sql':'unchanged'}
        current={**previous,'cloud_live.py':'new','fixtures/cloud_receiver_retry_v1.json':'new','fixtures/cloud_receiver_retry_v1_manifest.json':'new'}
        _same_core_files(previous,current)
        for changed in ('backend_worker.py','CLIENT_PIPELINE_PIN.json','migrations/019.sql','new_runtime.py'):
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'receiver_retry_core_code_changed'):
                _same_core_files(previous,{**current,changed:'changed'})

    def test_nonexecuted_slack_exception_requires_agent_manifest_and_keeps_core_frozen(self):
        path=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_receiver_retry_boundary_v1.json'
        raw=path.read_bytes();manifest=json.loads(path.with_name(path.stem+'_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['sha256'])
        self.assertEqual(manifest['sha256'],'f68b8267cb787e61cbc5a4bc914f4694280b87b3d87ad9aa6b124c65c92b98a1')
        fixture=json.loads(raw);previous={name:'unchanged' for name in fixture['always_deny']}
        current={**previous,**{name:'changed' for name in fixture['nonexecuted_addition']}}
        with self.assertRaisesRegex(ValueError,'receiver_retry_core_code_changed'):_same_core_files(previous,current)
        _same_core_files(previous,current,agent_only=True)
        for changed in fixture['always_deny']:
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'receiver_retry_core_code_changed'):
                _same_core_files(previous,{**current,changed:'changed'},agent_only=True)

    def test_nonexecuted_slack_deletion_fixture_is_exactly_named_only(self):
        path=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_slack_backend_deletion_v1.json'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),'8ce4de2fa889b678e61ce42fec53823fa9f721ec11fc3f6b69db91b90ad6dd23')
        names=('fixtures/cloud_slack_backend_deletion_v1.json','fixtures/cloud_slack_backend_deletion_v1_manifest.json')
        previous={'backend_worker.py':'unchanged'};current={**previous,**{name:'new' for name in names}}
        with self.assertRaisesRegex(ValueError,'receiver_retry_core_code_changed'):_same_core_files(previous,current)
        _same_core_files(previous,current,agent_only=True)
        with self.assertRaisesRegex(ValueError,'receiver_retry_core_code_changed'):
            _same_core_files(previous,{**current,'unrelated.py':'changed'},agent_only=True)

    def test_receiver_retry_reserves_original_campaign_retry_and_installation_once(self):
        from vaelius_test_support.hub.cloud_live import receive
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)/'receiver';installation=unittest.mock.Mock();installation.reserve.return_value=1
            def launch(*args,**kwargs):
                event={'type':'item.completed','item':{'type':'mcp_tool_call','server':'cloud-live','tool':'search_memory',
                    'status':'completed','result':{'content':[{'type':'text','text':json.dumps({'records':[{'id':'document1'}]})}]}}}
                kwargs['stdout'].write(json.dumps(event)+'\n')
                kwargs['stdout'].write(json.dumps({'type':'turn.completed','usage':{'input_tokens':50,'cached_input_tokens':20}})+'\n')
                _save(work/'answer.json',{'answer':'Supported.','used_reference_ids':['document1'],'abstained':False})
                proc=unittest.mock.Mock();proc.returncode=0;return proc
            with patch('agenthub.processing.harness.resolve_executable',return_value='/codex'),\
                 patch('agenthub.processing.usage.Ledger',return_value=installation),\
                 patch('agenthub.processing.evaluation_budget.reserve') as reserve,\
                 patch('agenthub.processing.evaluation_budget.finish'),\
                 patch('subprocess.run',return_value=unittest.mock.Mock(returncode=0,stdout='Logged in using ChatGPT',stderr='')),\
                 patch('subprocess.Popen',side_effect=launch) as process:
                result=receive('/client','project','Question',{'observer':{'executable':'codex'}},'/original-ledger',work,retry=True)
            reserve.assert_called_once_with('/original-ledger',result['attempt_id'],'task_retrieval',retry=True)
            self.assertEqual(installation.reserve.call_args.args[0],'task_retrieval_retry')
            self.assertEqual(installation.reserve.call_args.args[2]['job_kind'],'cloud_receiver_retry')
            self.assertEqual(process.call_count,1)

    def test_matching_api_build_is_required_before_any_run_mutation(self):
        path=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_live_runtime_v1.json'
        raw=path.read_bytes()
        manifest=json.loads(path.with_name(path.stem+'_manifest.json').read_text())
        self.assertEqual(manifest['sha256'],'de5935c221351a6b59debe660290fcdd9668b8a29e8b705ba3001e002a005f85')
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['sha256'])
        case=json.loads(raw)
        for item in case['cases']:
            with self.subTest(allowed=item['allowed']),tempfile.TemporaryDirectory() as temp:
                profile=Path(temp);_save(profile/'runtime.json',{'provider_mode':'off','api_port':55486})
                report=profile/'new-report.json'
                opener=unittest.mock.Mock()
                opener.open.return_value=io.BytesIO(json.dumps(item['health']).encode())
                # Source-level identity fixture is explicit; installed checks
                # must use the actual hashed package manifests.
                with patch('agenthub.backend_ops.build_identity',return_value={'build_ids':case['expected_build_ids']}),\
                    patch('urllib.request.build_opener',return_value=opener),\
                    patch('agenthub.cloud_runtime.registry_from_settings',side_effect=RuntimeError('fixture_registry_boundary')) as registry,\
                    patch('agenthub.processing.evaluation_budget.snapshot') as ledger,\
                    patch('vaelius_test_support.hub.cloud_live.deterministic_provider') as provider:
                    expected='fixture_registry_boundary' if item['allowed'] else 'installed_api_build_mismatch'
                    with self.assertRaisesRegex((ValueError,RuntimeError),expected):verify(profile,report)
                    self.assertEqual(registry.call_count,int(item['allowed']))
                    self.assertEqual(ledger.call_count,0);self.assertEqual(provider.call_count,0)
                    opener.open.assert_called_once_with('http://127.0.0.1:55486/health',timeout=5)
                if not item['allowed']:
                    self.assertFalse((profile/'live-runs').exists())
                    self.assertFalse((profile/'cloud-live-allocation.json').exists())
                    self.assertFalse(report.exists())

    def test_fixture_and_receiver_register_only_actual_delivery_tool(self):
        self.assertEqual(fixture()['allocation']['per_run_attempts'],12)
        args=_receive_args('/codex','/python','/client','project',Path('/work'))
        self.assertIn('mcp_servers.cloud-live.required=true',args)
        self.assertIn('mcp_servers.cloud-live.enabled_tools=["search_memory"]',args)
        self.assertIn('--ignore-user-config',args)
        self.assertNotIn('hook_context',' '.join(args))

    def test_actual_completed_mcp_response_is_required_not_only_answer_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            events=Path(temp)/'events'
            events.write_text(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Used document1'}})+'\n')
            self.assertEqual(_parse_events(events)[1:3],([],[]))
            events.write_text(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call','server':'cloud-live',
                'tool':'search_memory','status':'completed','result':{'content':[{'type':'text','text':json.dumps({'records':[{'id':'document1'}]})}]}}})+'\n')
            self.assertEqual(_parse_events(events)[2],[{'id':'document1'}])


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class LivePreflightTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store.curation_enabled=True  # This preflight explicitly exercises enrichment.
        self.store.retrieval_corpus='all'
        profile=Path(self.temp.name)/'profile';profile.mkdir();self.profile=profile
        credentials=profile/'credentials';credentials.mkdir()
        for principal in ('alice','bob'):
            token=credentials/('acme-'+principal+'.token');token.write_text(self.tokens[principal]);token.chmod(0o600)
        fixture_self=self
        self.store.semantic_embedder=None  # Provider-free fixture's idle model state.
        class Registry:
            def __init__(self):self._stores={'acme':fixture_self.store}
            def resolve(self,tenant):return fixture_self.store
            def store_for_token(self,token):
                fixture_self.store.authenticate(token);return fixture_self.store
            def store_factory(self,path,dsn,tenant,registry=None):
                store=PostgresEnterpriseStore(path,dsn,tenant)
                store.semantic_embedder=None
                store.curation_enabled=True
                store.retrieval_corpus='all'
                return store
        self.registry=Registry()
        self.build={'build_ids':{'agentclient':'source-fixture-client','agenthub':'source-fixture-hub'}}
        self.identity_patch=patch('agenthub.backend_ops.build_identity',return_value=self.build)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        from agenthub.cloud_api import create_app
        import uvicorn
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        app=create_app(self.registry,build_ids=self.build['build_ids'])
        self.server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
        self.thread=threading.Thread(target=self.server.run,daemon=True);self.thread.start()
        for _ in range(100):
            if self.server.started:break
            time.sleep(.02)
        if not self.server.started:raise RuntimeError('preflight_api_not_started')
        _save(profile/'runtime.json',{'provider_mode':'off','api_port':port})

    def tearDown(self):
        self.server.should_exit=True;self.thread.join(timeout=10)
        self.postgres_teardown()

    def test_fake_provider_full_capture_continuity_no_learning_and_revocation(self):
        # A second completed conversation exists before the scoped run. Its
        # private canary must remain unprocessed and outside observer context.
        from agentclient.enterprise_capture import normalize_capture
        self.store.enroll_connection(self.ctx,'unselected','codex','maple',['agent'])
        for kind in ('UserPromptSubmit','Stop'):
            self.store.ingest_general(self.ctx,normalize_capture({'hook_event_name':kind,'event_id':'unselected-'+kind,
                'session_id':'unselected','turn_id':'1','prompt':'CANARY_UNSELECTED_9 stays outside this bounded run.',
                'last_assistant_message':'Acknowledged.'},'maple','unselected'))
        from agenthub.processing.episode_pipeline import ensure_generation
        source=self.store.ingest(self.ctx,{'version':'enterprise-local-1','external_id':'draft-source','session':'draft','turn':'1',
            'project':'maple','kind':'Stop','body':'A building draft must remain unavailable until explicit activation.',
            'occurred_at':12345,'visibility':'private'})['source_id']
        draft=self.store.accept_reviewed_note(self.ctx,source,'Unrelated building draft',
            'A building draft must remain unavailable until explicit activation.')['document_id']
        with self.store.open() as state,state.db:
            ensure_generation(state.db,{'episode_curation':{'generation_id':'unrelated-building','policy':'durable_memory'}})
            state.db.execute('INSERT INTO knowledge_generation_documents VALUES(?,?,?)',('unrelated-building',draft,time.time()))
        with patch('agenthub.cloud_runtime.registry_from_settings',return_value=self.registry):
            try:result=verify(self.profile,self.profile/'preflight.json',live=False)
            except Exception:
                with self.store.open() as state:
                    details=[dict(row) for row in state.db.execute('SELECT status,stage,error FROM curation_episode_jobs')]
                report=json.loads((self.profile/'preflight.json').read_text())
                if 'HTTPError' in report.get('error',''):
                    self.store.search(self.store.authenticate(self.tokens['bob']),{'version':'enterprise-local-1','query':fixture()['question'],
                        'project':report['run'],'mode':'explicit','limit':8})
                self.fail(str(details))
        self.assertEqual(result['status'],'passed')
        self.assertTrue(result['checks']['continued_after_restart'])
        self.assertTrue(result['checks']['no_learning'])
        self.assertTrue(result['checks']['revocation_denied'])
        self.assertIsNone(result['checks']['actual_receiver_grounded'])
        self.assertEqual(result['attempts'],7)
        self.assertFalse(result['original_shared_accounting'])
        self.assertEqual(list(self.profile.rglob('*.sqlite')),[])
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT count(*) FROM backend_observers WHERE connection='unselected'").fetchone()[0],0)
            self.assertEqual(state.db.execute("SELECT count(*) FROM backend_processing_dependencies d JOIN backend_source_revisions r ON r.source_id=d.source_id WHERE r.connection='unselected'").fetchone()[0],0)
            from agenthub.processing.accounting import report
            accounting=report(state.db)
            self.assertEqual(accounting['total']['statuses'],{'done':7})
            self.assertEqual(accounting['total']['purposes'],{'durable_memory_curate':4,'episode_resolve':3})
            self.assertFalse(state.db.execute('SELECT 1 FROM knowledge_generation_documents WHERE generation_id=? AND document_id=?',
                (result['run'],draft)).fetchone())
            self.assertEqual(state.db.execute("SELECT status FROM knowledge_generations WHERE generation_id='unrelated-building'").fetchone()[0],'building')

    def test_same_policy_cell_filter_never_queues_disallowed_conversation(self):
        import hashlib
        from agentclient.enterprise_capture import normalize_capture
        from agenthub.processing.episode_pipeline import create_jobs
        location=Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_connection_scope_v1.json'
        raw=location.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'6e3e0527feec9bdce4d926e5bcf48234c602dc1ed3380ea4a0cbcc3bb5891386')
        case=json.loads(raw)
        payloads={}
        for connection in case['connections']:
            self.store.enroll_connection(self.ctx,connection,'codex','maple',['agent'],visibility='team',reader_ids=['alice','bob'])
            for kind in ('UserPromptSubmit','Stop'):
                event=normalize_capture({'hook_event_name':kind,'event_id':connection+kind,'session_id':connection,'turn_id':'1',
                    'prompt':'A meaningful synthetic decision because it preserves a header.','last_assistant_message':'Acknowledged.'},'maple',connection)
                result=self.store.ingest_general(self.ctx,event);payloads[connection]=result['source_id']
        config={'knowledge_backend':{'mode':'enterprise_local'},'episode_curation':{'policy':'durable_memory','generation_id':'scope-filter-fixture','settle_seconds':0}}
        with self.store.open() as state,state.db:
            scope=state.db.execute('SELECT internal_project FROM enterprise_sources WHERE id=?',(payloads['selected'],)).fetchone()[0]
            self.assertEqual(scope,state.db.execute('SELECT internal_project FROM enterprise_sources WHERE id=?',(payloads['unselected'],)).fetchone()[0])
            self.assertEqual(create_jobs(state,config,project_scope=scope,connection_ids=case['allowed']),case['expected_episode_jobs'])
            row=state.db.execute('SELECT * FROM curation_episode_jobs').fetchone()
            connections={r[0] for r in state.db.execute('SELECT connection FROM backend_source_revisions WHERE source_id=ANY(?::text[])',(json.loads(row['source_ids']),))}
            self.assertEqual(connections,set(case['expected_job_connections']))
            self.assertEqual(state.db.execute('SELECT count(*) FROM curation_episode_jobs').fetchone()[0],case['expected_episode_jobs'])
            original_hash=row['source_hash']
            state.db.execute('UPDATE enterprise_sources SET policy_version=policy_version+1 WHERE id=?',(payloads['selected'],))
            create_jobs(state,config,project_scope=scope,connection_ids=case['allowed'])
            self.assertNotEqual(original_hash,state.db.execute('SELECT source_hash FROM curation_episode_jobs').fetchone()[0])
            # run_once must preserve the operator scope while planning, even
            # when the later worker claim itself would reject the other job.
            from agenthub.processing.episode_pipeline import run_once
            runtime={**config,'observer':{'enabled':True,'min_interval_seconds':0},
                'episode_curation':{**config['episode_curation'],'enabled':True,'generation_id':'run-once-scope'},
                'backend_worker':{'allowed_connections':case['allowed']}}
            with patch('agenthub.processing.episode_pipeline._run_durable') as advance:
                self.assertTrue(run_once(state,runtime,project_scope=scope))
                self.assertEqual(advance.call_count,1)
            planned=state.db.execute('SELECT source_ids FROM curation_episode_jobs WHERE generation_id=?',('run-once-scope',)).fetchall()
            self.assertEqual(len(planned),1)
            selected_ids=json.loads(planned[0]['source_ids'])
            selected_connections={r[0] for r in state.db.execute('SELECT connection FROM backend_source_revisions WHERE source_id=ANY(?::text[])',(selected_ids,))}
            self.assertEqual(selected_connections,{'selected'})
            unfiltered={**config,'episode_curation':{**config['episode_curation'],'generation_id':'unfiltered-fixture'}}
            self.assertEqual(create_jobs(state,unfiltered,project_scope=scope),2)
            with self.assertRaisesRegex(ValueError,'enterprise_connection_scope_required'):
                create_jobs(state,{'episode_curation':config['episode_curation']},connection_ids=['selected'])

    def test_receiver_retry_preflight_rejects_changed_sources_policy_and_generation_without_calls(self):
        from agenthub.conversation_segments import ConversationSegments
        from agenthub.source_objects import FileSourceObjects
        self.store.conversation_segments=ConversationSegments(self.store,FileSourceObjects(self.profile/'objects'))
        # Source fixture simulates an MCP-only failure after a fully curated run;
        # the fake provider and package metadata are explicitly not live evidence.
        with patch('agenthub.cloud_runtime.registry_from_settings',return_value=self.registry):
            original=verify(self.profile,self.profile/'fake-original.json')
        self.store.set_membership('acme',original['run'],'bob',True)
        failed={**original,'status':'failed','live':True,'original_shared_accounting':True,
            'error':'ValueError:receiver_actual_mcp_delivery_missing','runtime':{'build_ids':RETRY_ORIGINAL_BUILD_IDS}}
        # Original metadata is projected before any source change; no model runs.
        with self.store.open() as state:
            failed['checks']['observer_checkpoints']=[{'epoch':r['observer_epoch'],'status':r['status'],
                'session_hash':hashlib.sha256(r['provider_session'].encode()).hexdigest() if r['provider_session'] else None,
                'context_chars':r['context_chars']} for r in state.db.execute('SELECT * FROM backend_observers WHERE connection=?',(original['run'],))]
        failed_path=self.profile/'failed-receiver.json';_save(failed_path,failed)
        alice=self.store.authenticate(self.tokens['alice']);bob=self.store.authenticate(self.tokens['bob'])
        with patch('agenthub.cloud_runtime.registry_from_settings',return_value=self.registry),\
             patch('vaelius_test_support.hub.cloud_live._retry_core_identity'),patch('vaelius_test_support.hub.cloud_live.receive') as receiver:
            report=retry_receiver(self.profile,failed_path,self.profile/'receiver-preflight.json')
            self.assertEqual(report['status'],'preflight_passed');self.assertEqual(report['attempts'],0)
            self.assertTrue(report['checks']['continued_after_restart'])
            receiver.assert_not_called()
            with self.store.open() as state,state.db:
                row=state.db.execute('SELECT source_id,payload FROM backend_source_revisions WHERE connection=? LIMIT 1',(original['run'],)).fetchone()
                state.db.execute('UPDATE backend_source_revisions SET payload=? WHERE source_id=?',
                    (json.dumps({'source_type':'agent','origin':'host_hook'}),row['source_id']))
            with self.assertRaisesRegex(ValueError,'receiver_retry_source_changed'):
                _retry_current_state(self.store,alice,bob,failed,fixture())
            with self.store.open() as state,state.db:
                state.db.execute('UPDATE backend_source_revisions SET payload=? WHERE source_id=?',(row['payload'],row['source_id']))
                slack=json.loads(row['payload']);slack.update(source_type='conversation',origin='slack')
                state.db.execute('UPDATE backend_source_revisions SET payload=? WHERE source_id=?',(json.dumps(slack),row['source_id']))
            with self.assertRaisesRegex(ValueError,'receiver_retry_non_agent_source'):
                _retry_current_state(self.store,alice,bob,failed,fixture())
            with self.store.open() as state,state.db:
                state.db.execute('UPDATE backend_source_revisions SET payload=? WHERE source_id=?',(row['payload'],row['source_id']))
                state.db.execute('UPDATE enterprise_sources SET policy_version=2 WHERE id=?',(row['source_id'],))
            with self.assertRaisesRegex(ValueError,'receiver_retry_source_policy_changed'):
                _retry_current_state(self.store,alice,bob,failed,fixture())
            with self.store.open() as state,state.db:
                state.db.execute('UPDATE enterprise_sources SET policy_version=1 WHERE id=?',(row['source_id'],))
                state.db.execute("UPDATE knowledge_generation_state SET active_generation_id='different' WHERE singleton=1")
            with self.assertRaisesRegex(ValueError,'receiver_retry_generation_changed'):
                _retry_current_state(self.store,alice,bob,failed,fixture())
            receiver.assert_not_called()


if __name__=='__main__':unittest.main()
