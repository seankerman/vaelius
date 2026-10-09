"""Stop/restart only the explicitly selected milestone PostgreSQL container.

Run with installed packages from /tmp. Receipts contain no credentials or bodies.
This tests local recovery, not managed failover/PITR. No model dispatcher runs.
"""
import argparse,json,os,re,subprocess,time,urllib.error,urllib.request
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True);parser.add_argument('--engine',default='podman')
    parser.add_argument('--container',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();os.umask(0o077)
    from agenthub.cloud_local import runtime,private_json
    from agenthub.backend_ops import build_identity
    from vaelius_test_support.hub.cloud_rehearsal import fingerprint
    profile=Path(args.profile).expanduser().absolute()
    services=json.loads((profile/'services.json').read_text())
    name=services.get('namespace','agentnetwork-cloud-v1')+'-postgres'
    if args.container!=name or not re.fullmatch(r'agentnetwork-cloud-[a-z0-9-]+-postgres',name):
        raise ValueError('explicit_milestone_postgres_container_required')
    def engine(*values):
        return subprocess.run([args.engine,*values],check=True,capture_output=True,text=True,timeout=30).stdout
    inspected=json.loads(engine('inspect',name))[0]
    ports=inspected['NetworkSettings'].get('Ports',{})
    published=ports.get('5432/tcp') or []
    if not inspected['State']['Running'] or not published or any(v['HostIp'] not in ('127.0.0.1','::1') for v in published):
        raise ValueError('running_loopback_milestone_postgres_required')
    settings,registry=runtime(profile)
    url='http://127.0.0.1:'+str(settings.get('api_port',55486))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path,token=None):
        req=urllib.request.Request(url+path,headers={'Authorization':'Bearer '+token} if token else {})
        try:
            with opener.open(req,timeout=10) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)
    health=request('/health')
    if health[0]!=200 or health[1].get('build_ids')!=build_identity()['build_ids']:
        raise ValueError('matching_installed_api_required')
    stores={t:registry.resolve(t) for t in ('acme','bravo')}
    before={t:fingerprint(s) for t,s in stores.items()}
    tokens={t:(profile/'credentials'/f'{t}-alice.token').read_text().strip() for t in stores}
    started=time.monotonic();stopped=False;receipt={'provider_calls':0,'managed_failover_evidence':False}
    try:
        engine('stop','--time','5',name);stopped=True
        receipt['during']={'health':request('/health')[0],'readiness':request('/ready')[0],
            'tenant_status':{t:request('/enterprise/v1/status',token)[0] for t,token in tokens.items()}}
        assert receipt['during']=={'health':200,'readiness':503,'tenant_status':{'acme':503,'bravo':503}}
    finally:
        if stopped:engine('start',name)
    recovery=time.monotonic()
    while request('/ready')[0]!=200:
        if time.monotonic()-recovery>25:raise ValueError('local_database_recovery_timeout')
        time.sleep(.2)
    after={t:fingerprint(s) for t,s in stores.items()}
    assert before==after
    receipt.update(pass_=True,current_authority_preserved=True,sqlite_fallback=False,
        elapsed_seconds=time.monotonic()-started,recovery_seconds=time.monotonic()-recovery,
        runtime=build_identity(),container=name,volumes_preserved=True)
    private_json(args.output,receipt)
    print(json.dumps({k:v for k,v in receipt.items() if k!='runtime'}))

if __name__=='__main__':main()
