"""Bounded operator interface for the opt-in local PostgreSQL profile.

Secrets stay in private files. No command changes founder/global hooks or selects
an external provider. Application startup and default worker runs are model-free.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

def private_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as file:json.dump(value,file,indent=2,sort_keys=True)
    path.chmod(0o600)

def runtime(profile):
    from agenthub.cloud_runtime import read_settings, registry_from_settings
    settings=read_settings(Path(profile)/'runtime.json')
    return settings,registry_from_settings(settings,Path(profile)/'server-state')

def worker_config(profile,*,provider_free=True):
    """Provider-free runs get a synthetic fixture; live runs get no provider or model.

    A live worker must name `backend_execution.kind` and its model in the private
    worker-config.json, and runtime.json `models` must allowlist them.
    """
    observer={'enabled':True,'min_interval_seconds':0,'timeout_seconds':60}
    if provider_free:observer['model']='fixture'
    cfg={'paused':False,'knowledge_backend':{'mode':'enterprise_local'},'observer':observer,
        'episode_curation':{'enabled':True,'policy':'durable_memory','settle_seconds':0,
            'generation_id':'cloud-readiness-v1'}}
    if provider_free:cfg['backend_execution']={'kind':'deterministic'}
    selected=Path(profile)/'worker-config.json'
    if selected.exists():cfg.update(json.loads(selected.read_text()))
    return cfg

def require_live_worker_allowed(settings,cfg):
    """Fail a live curation worker at startup unless its stage is allowlisted."""
    from agenthub.model_policy import policy_from_settings, require_stage_allowed
    policy=policy_from_settings(settings)
    require_stage_allowed(policy,cfg,'curation')
    return policy

def receipt(profile,command,value):
    from agenthub.backend_ops import build_identity
    from agenthub.pipeline_pin import verify
    result={'command':command,'time':time.time(),'runtime':build_identity(),'canonical_pin':verify(),
        'result':value}
    path=Path(profile)/'receipts'/f'{command}-{time.time_ns()}.json'
    private_json(path,result)
    return {'receipt':str(path),'result':value}

def status(profile):
    settings,registry=runtime(profile);result={'provider_startup':'off','tenants':{}}
    from agenthub.cloud_ops import Meter
    from agenthub.backend_worker import Worker
    with registry.open_control() as control:
        tenants = [dict(row) for row in control.execute('SELECT id,active FROM cloud_tenants ORDER BY id')]
    for registered in tenants:
        tenant = registered['id']
        if not registered['active']:
            result['tenants'][tenant] = {'active': False}
            continue
        store=registry.resolve(tenant);worker=Worker.__new__(Worker);worker.store=store
        with store.open() as state:
            counts={name:state.db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                for name in ('enterprise_sources','knowledge_documents','backend_document_versions')}
            versions=[r[0] for r in state.db.execute('SELECT version FROM cloud_schema ORDER BY version')]
        from agenthub.source_index import SourceIndex
        result['tenants'][tenant]={'active': True, 'counts':counts,'schema_versions':versions,
            'curation_enabled':store.curation_enabled,'retrieval_corpus':store.retrieval_corpus,
            'source_index':SourceIndex(store).status(),'enrichment_queue':worker.status(),'usage':Meter(store).status()}
    return result

def doctor(profile):
    from agenthub.pipeline_pin import verify
    report={'python':sys.version.split()[0],'canonical_pin':verify(),
        'container_runtime':shutil.which('podman') or shutil.which('docker'),
        'runtime_configured':(Path(profile)/'runtime.json').exists(),'models':'idle'}
    if report['runtime_configured']:report['status']=status(profile)
    return report

def up(profile):
    profile=Path(profile);services=profile/'services.json'
    if not services.exists():raise ValueError('explicit_local_services_configuration_required_see_CLOUD_LOCAL.md')
    metadata=json.loads(services.read_text());engine=shutil.which('podman') or shutil.which('docker')
    namespace=metadata.get('namespace','agentnetwork-cloud-v1')
    if not namespace.replace('-','').isalnum():raise ValueError('invalid_service_namespace')
    if engine:
        for service in ('postgres','moto','keycloak'):
            subprocess.run([engine,'start',namespace+'-'+service],check=True,capture_output=True,timeout=30)
    from agenthub.cloud_profile import setup_profile
    prepared=setup_profile(profile,services)
    settings,_=runtime(profile)
    if settings['objects']['kind']=='s3':
        from agenthub.object_config import objects_from_settings
        adapter=objects_from_settings(settings)
        try:adapter.client.head_bucket(Bucket=adapter.bucket)
        except adapter.client.exceptions.ClientError as error:
            if str(error.response['Error']['Code']) not in ('404','NoSuchBucket'):raise
            adapter.client.create_bucket(Bucket=adapter.bucket)
    return {'profile':prepared,'provider_dispatch':'off','api_start':'explicit_api_command_or_installed_container'}

def stop(profile):
    profile=Path(profile);stopped=[]
    pidfile=profile/'api-process.json'
    if pidfile.exists():
        data=json.loads(pidfile.read_text());pid=data['pid']
        command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
        if 'agenthub.cloud_runtime' not in command or str(profile) not in command:raise ValueError('api_process_identity_mismatch')
        os.kill(pid,signal.SIGTERM);stopped.append('host-api')
    metadata=json.loads((profile/'services.json').read_text());engine=shutil.which('podman') or shutil.which('docker')
    namespace=metadata.get('namespace','agentnetwork-cloud-v1')
    if engine:
        for service in ('api','postgres','moto','keycloak'):
            name=namespace+'-'+service
            known=subprocess.run([engine,'container','exists',name],capture_output=True).returncode==0 if Path(engine).name=='podman' else False
            if known:subprocess.run([engine,'stop','--time','10',name],check=True,capture_output=True,timeout=30);stopped.append(name)
    return {'stopped':stopped,'volumes_preserved':True,'global_hooks_changed':False}

GOVERNANCE_COMMANDS=('principal-quarantine','principal-release','principal-withdraw','governance-status')


def governance_command(store,args,parser):
    if args.command=='governance-status':return store.governance_status()
    if not args.principal or not args.reason:parser.error('--principal and --reason are required')
    if args.command=='principal-quarantine':
        return store.quarantine_principal(args.principal,reason=args.reason,since=args.since)
    if args.command=='principal-release':return store.release_principal(args.principal,reason=args.reason)
    if not args.since:parser.error('principal-withdraw requires --since (and optionally --until)')
    return store.withdraw_principal_content(args.principal,since=args.since,until=args.until,
        apply=args.apply,limit=args.limit,reason=args.reason)


def main(argv=None):
    def tenant_id(value):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
            raise argparse.ArgumentTypeError('invalid tenant identifier')
        return value
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=[
        'doctor','up','worker-run','status','stop','reindex',
        'source-index-reconcile',
        'retry','connection-status','readiness','backup','usage','versions','tenant-disable','tenant-enable',
        *GOVERNANCE_COMMANDS,'workspace','tenant-policy','github','backfill'])
    parser.add_argument('github_command',nargs='?',choices=['status','sync'],
        help='github: status (installations, last sync, stale groups, unlinked members) or sync')
    parser.add_argument('--profile',required=True);parser.add_argument('--tenant',default='acme',type=tenant_id)
    parser.add_argument('--provider-free',action='store_true')
    parser.add_argument('--live',action='store_true');parser.add_argument('--ledger');parser.add_argument('--job-id')
    parser.add_argument('--reviewed-stage-limit',type=int)
    parser.add_argument('--rederive',action='store_true',help='Rebuild a held job from its authorized originals in a fresh observer')
    parser.add_argument('--max-jobs',type=int,default=20);parser.add_argument('--max-seconds',type=int,default=60)
    parser.add_argument('--max-calls',type=int,default=12);parser.add_argument('--max-retries',type=int,default=3)
    parser.add_argument('--output');parser.add_argument('--connection-id')
    parser.add_argument('--after',default='')
    # Memory-governance operator commands (see docs/MEMORY_GOVERNANCE.md).
    parser.add_argument('--principal');parser.add_argument('--reason')
    parser.add_argument('--since',help='epoch seconds or ISO-8601 with offset');parser.add_argument('--until')
    parser.add_argument('--apply',action='store_true',help='principal-withdraw, backfill: apply; default is a dry run')
    parser.add_argument('--limit',type=int,default=200)
    # Workspaces and tenant policy (see docs/ACCESS_MODEL.md).
    parser.add_argument('--workspace',help='workspace: the workspace (name or id); omit to list every workspace')
    parser.add_argument('--action',default='show',
        choices=['show','set-entry','remove-entry','settings','link','unlink','create'],
        help='workspace: what to do (create adds an operator-managed workspace with a members group)')
    parser.add_argument('--subject-kind',choices=['principal','group','organization'])
    parser.add_argument('--subject');parser.add_argument('--role',choices=['reader','writer','admin'])
    parser.add_argument('--name');parser.add_argument('--item-default',choices=['shared','private'])
    parser.add_argument('--active',choices=['yes','no']);parser.add_argument('--remote')
    parser.add_argument('--workspace-creation',choices=['anyone','admins'],help='tenant-policy')
    parser.add_argument('--widest-entry',choices=['none','people_and_groups','organization'],help='tenant-policy')
    parser.add_argument('--owner-sharing',choices=['allowed','lower_only'],help='tenant-policy')
    parser.add_argument('--now',action='store_true',help='github sync: reconcile now instead of at the next tick')
    # Operator backfill of server-side conversation history (agenthub.server_backfill, docs/SELF_HOSTING.md).
    parser.add_argument('--input',help='backfill: a JSONL event file or a folder of them')
    parser.add_argument('--map',help='backfill: the principal and workspace mapping file')
    parser.add_argument('--max-events',type=int,default=5000,help='backfill: events written per run')
    parser.add_argument('--refresh-connections',action='store_true',
        help='backfill: re-observe the backfill connections of active writers (run at least every 30 days)')
    args=parser.parse_args(argv);profile=Path(args.profile).expanduser().resolve();os.umask(0o077)
    if args.live and args.provider_free:parser.error('choose one execution mode')
    if args.rederive and args.command!='retry':parser.error('--rederive requires retry')
    if (args.command=='github')!=(args.github_command is not None):parser.error('use: github status|sync [--now]')
    if args.now and args.github_command!='sync':parser.error('--now applies to github sync')
    if args.max_jobs<1 or args.max_seconds<1 or args.max_calls<1 or args.max_retries<0:parser.error('invalid local run size')
    if args.command=='doctor':value=doctor(profile)
    elif args.command=='up':value=up(profile)
    elif args.command=='stop':value=stop(profile)
    elif args.command=='status':value=status(profile)
    else:
        settings,registry=runtime(profile);store=registry.resolve(args.tenant)
        # Explicit finite commands may initialize CoreML (not API startup).
        # Run cleanup before interpreter module teardown, including failed work.
        import atexit
        from agenthub.model_resources import release_models
        atexit.register(lambda: release_models(registry, dict(registry._stores), store.semantic_embedder))
        if args.command=='worker-run':
            policy=None
            if store.curation_enabled:
                if not (args.provider_free or args.live):parser.error('enabled enrichment requires explicit --provider-free or --live')
                cfg=worker_config(profile,provider_free=args.provider_free)
                # Refuse before any indexing or model work if the stage is not allowlisted.
                if args.live:policy=require_live_worker_allowed(settings,cfg)
            from agenthub.cloud_maintenance import IndexFreshness
            index=IndexFreshness(store)
            if store.semantic_embedder is not None:index.ensure_generation()
            from agenthub.source_index import SourceIndex
            value=SourceIndex(store).run(max_sources=args.max_jobs,max_seconds=args.max_seconds)
            indexed=0;started=time.monotonic()
            if store.semantic_embedder is not None:
                for _ in range(args.max_jobs*64):
                    if time.monotonic()-started>=args.max_seconds:break
                    result=index.run_once(store.semantic_embedder)
                    if result['status']=='idle':break
                    indexed+=int(result['status']=='searchable')
            value['vector_documents']=indexed
            if store.curation_enabled:
                from agenthub.backend_worker import Worker
                from agenthub.cloud_execution import execution_from_config
                def fixture(*_):return {'records':[],'episode_summary':{'intent':None,'open_work':[]}}, {'input_tokens':0,'output_tokens':0}, 'synthetic-observer'
                runner=execution_from_config(cfg,provider_free=args.provider_free,fixture=fixture if args.provider_free else None,
                    policy=policy,stage='curation')
                value['enrichment']=Worker(store,cfg,runner=runner,live=args.live,ledger_path=args.ledger).run(max_jobs=args.max_jobs,
                    max_seconds=args.max_seconds,max_calls=args.max_calls,max_retries=args.max_retries)
        elif args.command=='source-index-reconcile':
            from agenthub.source_index import SourceIndex
            value=SourceIndex(store).reconcile(after=args.after,limit=min(args.max_jobs,1000))
        elif args.command=='reindex':value=store.reindex_vectors(max_documents=10000)
        elif args.command=='backup':
            if not args.output:parser.error('--output new private directory required')
            from agenthub.cloud_recovery import backup
            from agenthub.object_config import objects_from_settings
            value=backup(store,objects_from_settings(settings),args.output)
        elif args.command=='retry':
            if not args.job_id:parser.error('--job-id required')
            from agenthub.backend_worker import Worker
            value=Worker(store,worker_config(profile),live=False).recover(args.job_id,retain_validated=not args.rederive,
                reviewed_stage_limit=args.reviewed_stage_limit,rederive=args.rederive)
        elif args.command=='usage':
            from agenthub.cloud_ops import Meter
            value=Meter(store).status()
        elif args.command=='readiness':store.require_ready();value={'ready':True,'tenant':args.tenant}
        elif args.command in GOVERNANCE_COMMANDS:value=governance_command(store,args,parser)
        elif args.command=='backfill':
            from agenthub import server_backfill
            if args.refresh_connections:value=server_backfill.refresh_connections(store)
            else:
                if not args.input or not args.map:parser.error('backfill requires --input and --map')
                # Always owner-private: there are no sharing options (docs/ACCESS_MODEL.md).
                value=server_backfill.run(store,args.input,args.map,apply=args.apply,max_events=args.max_events,
                    max_seconds=args.max_seconds)
        elif args.command=='github':
            # GitHub App team sync (docs/ACCESS_MODEL.md section 3, docs/ENTERPRISE_AUTH.md).
            if args.github_command=='status':value=store.github_status()
            elif args.now:value=store.github_reconcile()
            else:value=store.github_mark_due()
        elif args.command=='tenant-policy':
            value=store.tenant_policy(None,{key:item for key,item in (('workspace_creation',args.workspace_creation),
                ('widest_entry',args.widest_entry),('owner_sharing',args.owner_sharing)) if item is not None})
        elif args.command=='workspace':
            if not args.workspace:
                if args.action!='show':parser.error('--workspace required')
                value=store.workspace_access_report()
            elif args.action=='create':
                store.create_project(args.tenant,args.workspace);value=store.operator_workspace('show',args.workspace)
            else:
                value=store.operator_workspace(args.action,args.workspace,subject_kind=args.subject_kind,
                    subject=args.subject,role=args.role,name=args.name,item_default=args.item_default,
                    active=None if args.active is None else args.active=='yes',remote=args.remote)
        elif args.command in ('tenant-disable','tenant-enable'):
            from agenthub.postgres import TenantRegistry
            operator=json.loads((profile/'operator.json').read_text());admin=TenantRegistry(operator['control_admin_dsn'],profile/'operator-state')
            admin.set_active(args.tenant,args.command=='tenant-enable');value={'tenant':args.tenant,'active':args.command=='tenant-enable'}
        else:
            with store.open() as state:
                if args.command=='versions':value={'versions':[dict(r) for r in state.db.execute('SELECT source_id,version,sha256,byte_length,parser_status FROM backend_document_versions ORDER BY created')]}
                else:value={'connections':[dict(r) for r in state.db.execute('SELECT id,active,permission_observed,freshness_seconds,policy_version FROM backend_connections')]}
    print(json.dumps(receipt(profile,args.command,value),sort_keys=True))

if __name__=='__main__':main()
