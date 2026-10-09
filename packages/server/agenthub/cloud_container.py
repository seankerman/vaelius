"""Explicit installed-wheel ARM64 local containers; no deployment or live dispatch.

Only new private runtime files are prepared. The host profile and control routes
stay authoritative and unchanged. Endpoint mapping is explicit, never fallback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

BASE='docker.io/library/python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f'
NETWORK='agentnetwork-cloud-v1'
API_NAME=NETWORK+'-api'


def _private(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink():raise ValueError('private_container_target_symlink')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2,sort_keys=True)
    path.chmod(0o600)


def prepare(source_profile,destination):
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    from agenthub.cloud_runtime import read_settings
    from agenthub.postgres import connect
    source=Path(source_profile).expanduser().resolve();destination=Path(destination).expanduser().resolve()
    if source==destination:raise ValueError('separate_container_profile_required')
    settings=read_settings(source/'runtime.json');dsn=conninfo_to_dict(settings['control_dsn'])
    if dsn.get('host') not in ('127.0.0.1','localhost','::1'):raise ValueError('explicit_local_source_profile_required')
    with connect(settings['control_dsn']) as db:
        role=db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
        if not role or any(role[i] for i in range(3)):raise ValueError('nonadmin_source_router_required')
    dsn.update(host=NETWORK+'-postgres',port='5432');settings['control_dsn']=make_conninfo(**dsn)
    settings['postgres_endpoint_override']={'host':NETWORK+'-postgres','port':5432}
    settings['objects']=dict(settings['objects'])
    if settings['objects'].get('kind')!='s3':raise ValueError('explicit_local_s3_fixture_required')
    settings['objects'].update(endpoint='http://'+NETWORK+'-moto:5000',local_hosts=[NETWORK+'-moto'])
    model=settings.get('semantic',{}).get('directory')
    if model:
        model=str(Path(model).expanduser().resolve(strict=True))
        settings['semantic']=dict(settings['semantic'],directory='/models/nomic')
    shared_accounting = settings.get('semantic', {}).get('accounting_root')
    if shared_accounting is not None:
        from agenthub.retrieval_embedding_cache import resolve_shared_accounting_root
        shared_accounting = str(resolve_shared_accounting_root(shared_accounting))
        settings['semantic']['accounting_root'] = '/accounting'
    # Synthetic loopback broker issuer URLs cannot be silently remapped inside a
    # private network. Preserve the source configuration until transport is explicit.
    if settings.get('identity_brokers'):raise ValueError('container_broker_transport_requires_explicit_configuration')
    settings['provider_mode']='off';settings['allowed_hosts']=['127.0.0.1','localhost'];settings['allowed_origins']=[]
    destination.mkdir(parents=True,exist_ok=True,mode=0o700);destination.chmod(0o700)
    if shared_accounting is not None:
        (destination/'accounting-mount'/'embedding-derivative').mkdir(parents=True, exist_ok=True, mode=0o700)
        (destination/'accounting-mount').chmod(0o700)
    _private(destination/'runtime.json',settings)
    _private(destination/'worker-config.json',{'backend_execution':{'kind':'deterministic'}})
    metadata={'source_profile':str(source),'profile':str(destination),'model_directory':model,
        'embedding_accounting_root': shared_accounting,
        'network':NETWORK,'uid':501,'gid':20,'base':BASE,'admin_credentials_copied':False,
        'operator_configuration_copied':False,'developer_home_mounted':False,'provider_calls':0}
    _private(destination/'container-profile.json',metadata)
    return metadata


def build_context(wheels,context,dockerfile):
    wheels=Path(wheels).expanduser().resolve();context=Path(context).expanduser().resolve()
    if context.exists() and any(context.iterdir()):raise ValueError('new_private_build_context_required')
    selected=[]
    for package in ('vaelius_client','vaelius_server'):
        matches=list(wheels.glob(package+'-*.whl'))
        if len(matches)!=1:raise ValueError('exactly_one_installed_wheel_per_repository_required')
        selected.append(matches[0])
    context.mkdir(parents=True,exist_ok=True,mode=0o700);target=context/'wheels';target.mkdir(mode=0o700)
    hashes={}
    for source in selected:
        shutil.copyfile(source,target/source.name);hashes[source.name]=hashlib.sha256(source.read_bytes()).hexdigest()
    shutil.copyfile(dockerfile,context/'Dockerfile')
    lock = Path(dockerfile).with_name('requirements.cloud.lock')
    if lock.exists():
        shutil.copyfile(lock, context/lock.name)
    elif Path(dockerfile).name == 'Dockerfile.cloud':
        raise ValueError('canonical_dependency_lock_required')
    return {'context':str(context),'wheel_sha256':hashes,'base':BASE,'credentials_in_build_context':False,
        'dependency_lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None}


def _run(args,*,timeout=60):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('container_command_failed: '+result.stderr[-4000:])
    return result.stdout


def _mounts(profile):
    profile=Path(profile).expanduser().resolve()
    metadata=json.loads((profile/'container-profile.json').read_text())
    mounts=['--user','501:20','--network',NETWORK,'--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=256m',
        '-v',str(profile)+':/profile:rw']
    if metadata.get('model_directory'):mounts+=['-v',metadata['model_directory']+':/models/nomic:ro']
    if metadata.get('embedding_accounting_root'):
        mounts += ['-v', str(profile/'accounting-mount')+':/accounting:ro',
            '-v', str(Path(metadata['embedding_accounting_root'])/'embedding-derivative')
                +':/accounting/embedding-derivative:rw']
    return mounts


def start(engine,image,profile,*,port=55486):
    if not 1024<=port<=65535:raise ValueError('invalid_loopback_port')
    _run([engine,'network','inspect',NETWORK])
    # Do not replace or stop an existing container, even if its name matches.
    return _run([engine,'run','-d','--name',API_NAME,'--label','agentnetwork.cloud-readiness=installed-v1',
        '-p','127.0.0.1:'+str(port)+':8080',*_mounts(profile),image],timeout=30).strip()


def probe():
    import platform
    from agenthub.backend_ops import build_identity
    from agenthub.pipeline_pin import verify
    from agenthub.cloud_runtime import read_settings, registry_from_settings
    from agenthub.postgres import connect
    settings=read_settings('/profile/runtime.json');registry=registry_from_settings(settings,'/profile/server-state')
    roles={}
    for name,dsn in [('control',settings['control_dsn'])]+[(tenant,registry.resolve(tenant).dsn) for tenant in ('acme','bravo')]:
        with connect(dsn) as db:
            row=db.execute('SELECT current_user,rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
            if any(row[i] for i in (1,2,3)):raise ValueError('container_application_role_must_not_be_admin')
            roles[name]={'role':row[0],'superuser':False,'create_database':False,'create_role':False}
    _run([sys.executable,'-m','pip','check'])
    identity=build_identity()
    if any('/site-packages/' not in value['path'] for value in identity['modules'].values()):
        raise ValueError('installed_wheel_runtime_required')
    if platform.machine()!='aarch64':raise ValueError('native_arm64_runtime_required')
    return {'arch':platform.machine(),'uid':os.getuid(),'gid':os.getgid(),'installed':identity,
        'canonical_pin':verify(),'roles':roles,'pip_check':'passed','provider_calls':0,
        'developer_home_present':Path('/Users').exists(),'model_read_only':not os.access('/models/nomic',os.W_OK)}


def verify(engine,image,profile,*,port=55486):
    health={}
    for name in ('health','ready'):
        for attempt in range(60):
            try:
                with urlopen(f'http://127.0.0.1:{port}/{name}',timeout=2) as response:health[name]=json.load(response)
                break
            except Exception:
                if attempt==59:raise
                time.sleep(.25)
    installed=json.loads(_run([engine,'run','--rm',*_mounts(profile),image,'python','-m','agenthub.cloud_container','probe'],timeout=120))
    return {'http':health,'installed_container':installed,'image':json.loads(_run([engine,'image','inspect',image]))[0]['Id']}


def worker(engine,image,profile,*,tenant='acme',max_jobs=20,max_seconds=60):
    if (not isinstance(tenant, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', tenant)
            or not 1<=max_jobs<=20 or not 1<=max_seconds<=60):raise ValueError('finite_worker_bounds')
    raw=_run([engine,'run','--rm',*_mounts(profile),image,'python','-m','agenthub.cloud_local','worker-run',
        '--profile','/profile','--tenant',tenant,'--provider-free','--max-jobs',str(max_jobs),
        '--max-seconds',str(max_seconds)],timeout=max_seconds+60)
    return json.loads(raw)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','context','build','start','verify','worker','stop','probe'])
    parser.add_argument('--profile');parser.add_argument('--source-profile');parser.add_argument('--wheels');parser.add_argument('--context')
    parser.add_argument('--dockerfile');parser.add_argument('--image',default='localhost/agentnetwork-cloud-b2:local')
    parser.add_argument('--engine',default=shutil.which('podman') or shutil.which('docker'))
    parser.add_argument('--port',type=int,default=55486);parser.add_argument('--tenant',default='acme')
    parser.add_argument('--max-jobs',type=int,default=20);parser.add_argument('--max-seconds',type=int,default=60)
    args=parser.parse_args(argv);os.umask(0o077)
    if args.command=='probe':result=probe()
    elif args.command=='prepare':result=prepare(args.source_profile,args.profile)
    elif args.command=='context':result=build_context(args.wheels,args.context,args.dockerfile)
    elif args.command=='build':
        result={'image':args.image,'build_output':_run([args.engine,'build','--platform','linux/arm64','--pull=never',
            '-t',args.image,args.context],timeout=900)[-4000:]}
    elif args.command=='start':result={'container':start(args.engine,args.image,args.profile,port=args.port)}
    elif args.command=='verify':result=verify(args.engine,args.image,args.profile,port=args.port)
    elif args.command=='worker':result=worker(args.engine,args.image,args.profile,tenant=args.tenant,max_jobs=args.max_jobs,max_seconds=args.max_seconds)
    else:
        inspection=json.loads(_run([args.engine,'container','inspect',API_NAME]))[0]
        if inspection['Config']['Labels'].get('agentnetwork.cloud-readiness')!='installed-v1':raise ValueError('owned_container_label_required')
        _run([args.engine,'stop','--time','10',API_NAME],timeout=30);result={'stopped':API_NAME,'volumes_preserved':True}
    if args.profile and args.command!='probe':
        _private(Path(args.profile)/'receipts'/('container-'+args.command+'-'+str(time.time_ns())+'.json'),result)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
