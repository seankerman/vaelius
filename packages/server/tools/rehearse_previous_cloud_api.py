"""Read-only previous installed API probe against current local authority.

Uses existing synthetic current/private and withdrawn documents. No source writes,
provider dispatch, route cutover or SQLite restore. Audit writes remain expected.
"""
import argparse, hashlib, json, os, subprocess, time
from pathlib import Path
import urllib.error, urllib.request

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True);parser.add_argument('--port',type=int,default=55489)
    parser.add_argument('--output',required=True);args=parser.parse_args();os.umask(0o077)
    if not 1024<=args.port<=65535:raise ValueError('loopback_port_bound')
    from agenthub.cloud_local import runtime,private_json
    from agenthub.backend_ops import build_identity,verify_package_build
    from vaelius_test_support.hub.cloud_rehearsal import fingerprint
    profile=Path(args.profile).expanduser().absolute();output=Path(args.output).expanduser().absolute()
    if output.exists():raise ValueError('new_private_receipt_required')
    settings,registry=runtime(profile);store=registry.resolve('acme')
    selection=json.loads((profile/'rollback-selection.json').read_text())
    python=Path(selection['previous_interpreter']).expanduser().absolute()
    if not python.is_file() or not os.access(python,os.X_OK):raise ValueError('previous_installed_python_required')
    environment=dict(os.environ);environment.pop('PYTHONPATH',None)
    probe=subprocess.run([str(python),'-c',
        'import json,agentclient,agenthub;from pathlib import Path;print(json.dumps({m.__name__:str(Path(m.__file__).parent) for m in (agentclient,agenthub)}))'],
        cwd='/tmp',env=environment,capture_output=True,text=True,check=True,timeout=10)
    previous_files={name:verify_package_build(path) for name,path in json.loads(probe.stdout).items()}
    if {name:value['build_id'] for name,value in previous_files.items()}!=selection['previous_build_ids']:
        raise ValueError('previous_installed_files_mismatch')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def read(base,path,token=None):
        request=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token} if token else {})
        try:
            with opener.open(request,timeout=20) as response:return response.status,response.read(1024*1024+1)
        except urllib.error.HTTPError as error:return error.code,error.read(65537)
    current='http://127.0.0.1:'+str(settings.get('api_port',55486));previous='http://127.0.0.1:'+str(args.port)
    status,raw=read(current,'/health')
    if status!=200 or json.loads(raw).get('build_ids')!=build_identity()['build_ids']:raise ValueError('matching_current_api_required')
    # Refuse to replace an existing service at this port.
    import socket
    with socket.socket() as probe:probe.bind(('127.0.0.1',args.port))
    with store.open() as state:
        current_doc=state.db.execute("""SELECT d.id FROM enterprise_documents d
            JOIN knowledge_documents k ON k.document_id=d.id
            JOIN enterprise_dependencies e ON e.document_id=d.id
            JOIN enterprise_sources s ON s.id=e.source_id
            WHERE d.active=1 AND k.lifecycle='active' AND s.owner='alice'
            AND s.visibility='private' ORDER BY d.id LIMIT 1""").fetchone()
        withdrawn=state.db.execute("""SELECT d.id FROM enterprise_documents d
            JOIN knowledge_documents k ON k.document_id=d.id
            JOIN enterprise_dependencies e ON e.document_id=d.id
            JOIN enterprise_sources s ON s.id=e.source_id
            WHERE d.active=0 AND s.owner='alice' ORDER BY d.id LIMIT 1""").fetchone()
    if not current_doc or not withdrawn:raise ValueError('existing_current_private_and_withdrawn_fixtures_required')
    tokens={name:(profile/'credentials'/('acme-'+name+'.token')).read_text().strip() for name in ('alice','bob')}
    # The canonical HTTP facade hides denied IDs with 404.
    cases=[('current_owner',current_doc[0],'alice',200),('current_other_user',current_doc[0],'bob',404),
           ('withdrawn_owner',withdrawn[0],'alice',404)]
    before=fingerprint(store);before.pop('enterprise_audit',None)
    logpath=output.with_suffix('.log');log=open(logpath,'x');logpath.chmod(0o600)
    process=None;started=time.monotonic()
    try:
        process=subprocess.Popen([str(python),'-m','agenthub.cloud_runtime','--profile',str(profile),
            '--bind','127.0.0.1','--port',str(args.port)],cwd='/tmp',env=environment,stdout=log,stderr=log)
        until=time.monotonic()+15
        while True:
            try:code,health=read(previous,'/health');break
            except (OSError,urllib.error.URLError):
                if process.poll() is not None or time.monotonic()>until:raise ValueError('previous_api_start_failed')
                time.sleep(.1)
        previous_health=json.loads(health)
        # B4 predates build IDs in HTTP health. The explicit previous executable's
        # actual installed files were independently verified before it started.
        if code!=200 or previous_health.get('service')!='AgentHub' or (
            previous_health.get('build_ids') is not None and previous_health['build_ids']!=selection['previous_build_ids']):
            raise ValueError('previous_api_build_mismatch')
        checks=[]
        for label,ident,principal,expected in cases:
            path='/enterprise/v1/documents/'+ident
            old_code,old_body=read(previous,path,tokens[principal]);new_code,new_body=read(current,path,tokens[principal])
            if old_code!=expected or new_code!=expected:
                raise ValueError('previous_current_policy_probe_failed:'+label+':previous='+str(old_code)+':current='+str(new_code))
            exact=old_body==new_body if expected==200 else None
            if expected==200 and not exact:raise ValueError('previous_current_document_changed')
            checks.append({'case':label,'previous_status':old_code,'current_status':new_code,
                'document_bytes_equal':exact,'response_sha256':hashlib.sha256(old_body).hexdigest()})
        after=fingerprint(store);after.pop('enterprise_audit',None)
        if before!=after:raise ValueError('previous_api_changed_source_or_policy_authority')
        result={'pass':True,'checks':checks,'current_build_ids':build_identity()['build_ids'],
            'previous_build_ids':selection['previous_build_ids'],'previous_files_verified':previous_files,
            'previous_http_build_ids_available':'build_ids' in previous_health,'current_authority_preserved':True,
            'read_only_requests':True,'audit_records_expected':True,'sqlite_restored':False,
            'writers_or_dispatchers_activated':False,'live_route_changed':False,'provider_calls':0,
            'elapsed_seconds':time.monotonic()-started,'deployment_rollback_proved':False}
        private_json(output,result);print(json.dumps(result))
    finally:
        if process is not None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        log.close()

if __name__=='__main__':main()
