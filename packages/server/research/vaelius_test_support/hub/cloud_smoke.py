"""Synthetic installed consumer rehearsal; natural host use is a separate gate."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

def verify(profile):
    from agenthub.cloud_local import runtime,private_json,worker_config
    from agenthub.backend_worker import Worker
    from agenthub.cloud_execution import DeterministicExecution
    profile=Path(profile);settings,registry=runtime(profile);store=registry.resolve('acme')
    port=settings.get('api_port',55486);url='http://127.0.0.1:'+str(port)
    from agenthub.backend_ops import build_identity
    expected_build=build_identity()['build_ids']
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));process=None
    try:
        with opener.open(url+'/health',timeout=1) as response:
            if json.load(response).get('build_ids')!=expected_build:raise ValueError('installed_api_build_mismatch')
    except (OSError,urllib.error.URLError):
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        log=open(profile/'api.log','a');os.chmod(profile/'api.log',0o600)
        process=subprocess.Popen([sys.executable,'-m','agenthub.cloud_runtime','--profile',str(profile),'--port',str(port)],
            cwd='/tmp',env=env,stdout=log,stderr=log)
        log.close();private_json(profile/'api-process.json',{'pid':process.pid,'profile':str(profile)})
        started=time.monotonic()
        while True:
            try:opener.open(url+'/health',timeout=1).close();break
            except (OSError,urllib.error.URLError):
                if process.poll() is not None or time.monotonic()-started>15:raise ValueError('installed_api_start_failed')
                time.sleep(.1)
    suffix=uuid.uuid4().hex[:10];dataset_path='/synthetic/cloud-smoke-'+suffix+'.csv';title='Synthetic cloud dataset '+suffix;home=profile/('smoke-client-'+suffix);project=profile/'smoke-project';project.mkdir(exist_ok=True)
    tokenfile=profile/'credentials/acme-alice.token';token=tokenfile.read_text().strip();ctx=store.authenticate(token)
    connection='smoke-agent-'+suffix
    store.enroll_connection(ctx,connection,'synthetic-installed','enrolled-test',['agent'])
    config={'paused':False,'projects':{str(project):'enrolled-test'},'sessions':{},'publication_enabled':False,
        'knowledge_backend':{'mode':'enterprise_local','url':url,'credential_file':str(tokenfile),
            'capture_version':'enterprise-local-2','api_version':'cloud-local-1','connection_id':connection,
            'capture_owner':'hooks'}}
    private_json(home/'config.json',config);checks={};commands=[]
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    def command(module,args,*,data=None):
        result=subprocess.run([sys.executable,'-m',module,*args],cwd='/tmp',env=env,
            input=data,capture_output=True,text=True,timeout=30)
        commands.append({'module':module,'returncode':result.returncode,
            'stdout_sha256':hashlib.sha256(result.stdout.encode()).hexdigest(),'stderr_sha256':hashlib.sha256(result.stderr.encode()).hexdigest()})
        if result.returncode:raise ValueError('installed_consumer_failed:'+module)
        return result.stdout
    def post(path,value,binary=False,metadata=None):
        request=urllib.request.Request(url+path,data=value if binary else json.dumps(value).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream' if binary else 'application/json',
                **({'X-Document-Metadata':json.dumps(metadata)} if metadata else {})})
        with opener.open(request,timeout=15) as response:return json.load(response)
    def mcp(name,args):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}}]
        output=command('agentclient.mcp',['--home',str(home),'--project','enrolled-test'],data=''.join(json.dumps(m)+'\n' for m in messages))
        result=json.loads(output.splitlines()[-1])['result']
        if result.get('isError'):raise ValueError('installed_mcp_tool_failed:'+name)
        return json.loads(result['content'][0]['text'])
    try:
        for index,event in enumerate([
            {'hook_event_name':'UserPromptSubmit','prompt':'Use CSV because a stable header is required.'},
            {'hook_event_name':'PostToolUse','tool_use_id':'save','tool_name':'save_file',
             'tool_input':{'path':dataset_path,'password':'synthetic-redaction-canary'},
             'tool_response':{'status':'saved'},'exit_code':0},
            {'hook_event_name':'Stop','last_assistant_message':'The dataset was saved at '+dataset_path+'.'}]):
            event.update(session_id='smoke-'+suffix,turn_id='first',cwd=str(project),event_id='smoke-'+suffix+'-'+str(index),timestamp='2026-09-26T12:00:00Z')
            command('agentclient.cli',['--home',str(home),'hook'],data=json.dumps(event))
        drain_start=time.monotonic();drain_commands=0
        while True:
            drained=json.loads(command('agentclient.cli',['--home',str(home),'outbox-drain','--max-events','20','--max-seconds','5']))
            drain_commands+=1
            if not drained.get('pending_events'):break
            if time.monotonic()-drain_start>15:raise ValueError('capture_not_drained_within_bound')
            # Preserve real outbox backoff and retry IDs after a short hook timeout.
            time.sleep(.25)
        with store.open() as state:
            sources=state.db.execute("SELECT sr.source_id,sr.payload FROM backend_source_revisions sr WHERE sr.external_id LIKE ? ORDER BY sr.external_id",('smoke-'+suffix+'-%',)).fetchall()
        safe=json.dumps([json.loads(s['payload']) for s in sources])
        checks['capture']={'events':len(sources),'redacted': 'synthetic-redaction-canary' not in safe,
            'path_preserved':dataset_path in safe,'outbox_pending':0,'drain_commands':drain_commands}
        assert len(sources)==3 and checks['capture']['redacted'] and checks['capture']['path_preserved']
        no_learning=lambda *_:({'records':[],'episode_summary':{'intent':None,'open_work':[]}},{'input_tokens':0,'output_tokens':0},'synthetic-observer')
        cfg=worker_config(profile);cfg['backend_worker']={'allowed_connections':[connection]}
        cfg['episode_curation']['generation_id']='cloud-smoke-'+suffix
        worker=Worker(store,cfg,runner=DeterministicExecution(no_learning),live=False)
        worked=worker.run(max_jobs=1,max_calls=3,max_retries=0,max_seconds=10)
        assert worked['completed']==1
        checks['finite_worker']=worked
        source=sources[-1]['source_id']
        note=post('/enterprise/v1/reviewed-notes',{'source_id':source,'title':title,
            'lesson':'The synthetic cloud dataset is at '+dataset_path+'.'})
        search=mcp('search_memory',{'query':title})
        assert search['answerable'] and any(c['id']==note['document_id'] for c in search['records'])
        detail=mcp('fetch_memory',{'id':note['document_id']})
        timeline=mcp('expand_memory_timeline',{'id':note['document_id'],'offset':0,'limit':1})
        checks['progressive_mcp']={'search':True,'detail':detail['id']==note['document_id'],'timeline_contract':bool('episodes' in timeline)}
        raw=b'# Synthetic original\nExact original bytes, including this final paragraph.\n'
        uploaded=post('/enterprise/v3/source-documents/upload',raw,True,{
            'connection':'fixture-documents','external_id':'smoke-original-'+suffix,'version':'1','filename':'original.md','title':'Synthetic original '+suffix})
        destination=profile/('download-'+suffix+'.md')
        discovered=mcp('find_source_documents',{'title':'Synthetic original '+suffix,'limit':20})
        assert any(d['source_id']==uploaded['source_id'] for d in discovered['documents'])
        downloaded=mcp('fetch_source_document',{'source_id':uploaded['source_id'],'download_to':str(destination)})
        assert destination.read_bytes()==raw
        checks['original_download']={'discovery':True,'exact':True,'sha256':hashlib.sha256(raw).hexdigest(),
            'private_mode':destination.stat().st_mode&0o077==0,'bytes':len(raw)}
        pref_source=store.ingest(ctx,{'version':'enterprise-local-1','external_id':'pref-'+suffix,'session':'earlier-'+suffix,
            'turn':'pref','project':'enrolled-test','kind':'UserPromptSubmit','body':'Please remember: I prefer pytest for my projects.',
            'visibility':'private','occurred_at':'2026-09-26T11:00:00Z'})['source_id']
        admitted=post('/enterprise/v3/preferences/curate',{'source_id':pref_source,'candidate':{
            'key':'test_runner','value':'pytest','scope':'user','quote':'Please remember: I prefer pytest for my projects.'}})
        assert mcp('get_user_preferences',{})['values']['test_runner']=='pytest'
        startup={'hook_event_name':'SessionStart','session_id':'receiver-'+suffix,'cwd':str(project)}
        first=json.loads(command('agentclient.cli',['--home',str(home),'hook'],data=json.dumps(startup)))
        assert 'pytest' in first['hookSpecificOutput']['additionalContext']
        duplicate=json.loads(command('agentclient.cli',['--home',str(home),'hook'],data=json.dumps(startup)))
        assert duplicate=={}
        boundary={**startup,'hook_event_name':'PostCompact','turn_id':'compact','trigger':'manual'}
        command('agentclient.cli',['--home',str(home),'hook'],data=json.dumps(boundary))
        restored=json.loads(command('agentclient.cli',['--home',str(home),'hook'],data=json.dumps(startup)))
        assert 'pytest' in restored['hookSpecificOutput']['additionalContext']
        post('/enterprise/v3/preferences/withdraw',{'document_id':admitted['document_id']})
        assert not mcp('get_user_preferences',{})['values']
        checks['preferences']={'mcp':True,'hook':True,'duplicate_suppressed':True,'after_compaction':True,'withdrawn':True}
        from agentclient.enterprise_contract import VERSION
        current=store.source(ctx,source)
        post('/enterprise/v1/lifecycle',{'version':VERSION,'target_id':source,'expected_revision':str(current['source_version']),
            'operation':'withdraw','idempotency_key':'withdraw-'+suffix,'reason':'synthetic lifecycle test'})
        assert not mcp('search_memory',{'query':title})['answerable']
        checks['withdrawal']={'dependent_answer_removed':True}
        checks['client_boundary']={'knowledge_sqlite_absent':not (home/'client.sqlite').exists(),
            'model_calls':0,'founder_config_changed':False,'host_confirmed':False,'ordinary_session':False}
        assert checks['client_boundary']['knowledge_sqlite_absent']
        return {'pass':True,'classification':'controlled synthetic subprocess consumer; not ordinary host use',
            'provider_calls':0,'checks':checks,'commands':commands,'profile':str(home)}
    finally:
        if process:
            process.terminate();process.wait(timeout=15);(profile/'api-process.json').unlink(missing_ok=True)
