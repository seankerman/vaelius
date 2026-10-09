"""Finite owner-operated backend commands. API startup never dispatches models."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

from agenthub.backend_worker import Worker, initialize
from agenthub.local_connectors import Connector


def parsers(sub):
    worker=sub.add_parser('worker');worker.add_argument('action',choices=['once','run','status','stop','retry'])
    worker.add_argument('--config');worker.add_argument('--ledger');worker.add_argument('--live',action='store_true')
    worker.add_argument('--job-id');worker.add_argument('--max-jobs',type=int,default=1)
    worker.add_argument('--reprocess',action='store_true');worker.add_argument('--retain-validated',action='store_true')
    worker.add_argument('--max-calls',type=int,default=12);worker.add_argument('--max-retries',type=int,default=3)
    worker.add_argument('--max-seconds',type=int,default=120);worker.add_argument('--max-input-tokens',type=int,default=100000)
    connector=sub.add_parser('connector');connector.add_argument('action',choices=['enroll','sync','status'])
    connector.add_argument('id');connector.add_argument('--token-file',required=True)
    connector.add_argument('--adapter',choices=['directory','conversations','records']);connector.add_argument('--path')
    connector.add_argument('--project');connector.add_argument('--visibility',default='private')
    connector.add_argument('--reader',action='append',default=[]);connector.add_argument('--policy-file')
    connector.add_argument('--dry-run',action='store_true');connector.add_argument('--max-items',type=int,default=1000)
    connection=sub.add_parser('connection');connection.add_argument('action',choices=['enroll','refresh','status','disable'])
    connection.add_argument('id');connection.add_argument('--token-file',required=True)
    connection.add_argument('--namespace');connection.add_argument('--project');connection.add_argument('--type',action='append',dest='types')
    connection.add_argument('--visibility');connection.add_argument('--reader',action='append')
    connection.add_argument('--freshness-seconds',type=int,default=86400)
    connection.add_argument('--allow-reviewed-release',action='store_true')
    budget=sub.add_parser('budget-status');budget.add_argument('--config',required=True);budget.add_argument('--ledger',required=True)
    release=sub.add_parser('reviewed-release');release.add_argument('--token-file',required=True)
    release.add_argument('id');release.add_argument('--revision',required=True);release.add_argument('--project',required=True)
    release.add_argument('--reader',action='append',required=True);release.add_argument('--key',required=True)
    source=sub.add_parser('structured-source');source.add_argument('id');source.add_argument('--token-file',required=True)


def execute(store,args,context):
    if args.command=='structured-source':return store.general_source(context(store,args),args.id)
    if args.command=='reviewed-release':return store.reviewed_release(context(store,args),args.id,args.revision,args.project,args.reader,args.key)
    if args.command=='budget-status':
        from agenthub.processing.evaluation_budget import snapshot
        from agenthub.processing.usage import Ledger
        ledger=Ledger(json.loads(Path(args.config).read_text()))
        try:return {'campaign':snapshot(args.ledger),'installation':ledger.summary()}
        finally:ledger.close()
    if args.command=='connection':
        ctx=context(store,args)
        if args.action=='enroll':
            if not all((args.namespace,args.project,args.types)):raise ValueError('connection_enrollment_fields_required')
            return store.enroll_connection(ctx,args.id,args.namespace,args.project,args.types,
                visibility=args.visibility or 'private',reader_ids=args.reader,freshness_seconds=args.freshness_seconds,
                capabilities={'allow_reviewed_release':args.allow_reviewed_release})
        if args.action in {'refresh','disable'}:return store.connection_policy(ctx,args.id,visibility=args.visibility,reader_ids=args.reader,active=args.action!='disable')
        with store.open() as state:
            row=store._connection(state.db,ctx,args.id,allow_stale=True)
            return {key:row[key] for key in ('id','namespace','project','visibility','policy_version','permission_observed','freshness_seconds','active')}
    if args.command=='connector':
        connectors=Connector(store);ctx=context(store,args)
        if args.action=='enroll':
            if not all((args.path,args.adapter,args.project)):raise ValueError('connector_enrollment_fields_required')
            return connectors.enroll(ctx,args.id,args.adapter,args.path,args.project,visibility=args.visibility,
                reader_ids=args.reader,policy_file=args.policy_file)
        if args.action=='sync':return connectors.sync(ctx,args.id,dry_run=args.dry_run,max_items=args.max_items)
        return connectors.status(ctx,args.id)
    if args.command!='worker':return None
    with store.open() as state:initialize(state.db)
    if args.action=='status':
        worker=Worker.__new__(Worker);worker.store=store;return worker.status()
    pidfile=store.home/'worker.json'
    if args.action=='stop':
        if not pidfile.exists():return {'stopped':True}
        current=json.loads(pidfile.read_text())
        import subprocess
        command=subprocess.run(['ps','-p',str(current['pid']),'-o','command='],capture_output=True,text=True).stdout
        if 'agenthub.enterprise_cli' not in command or str(store.home) not in command:raise ValueError('worker_pid_mismatch')
        os.kill(current['pid'],signal.SIGTERM);return {'stop_requested':True,'pid':current['pid']}
    cfg=Path(args.config) if args.config else store.home/'config.json'
    config=json.loads(cfg.read_text())
    worker=Worker(store,config,live=args.live,ledger_path=args.ledger)
    if args.action=='retry':
        if not args.job_id:raise ValueError('job_id_required')
        return worker.recover(args.job_id,reprocess=args.reprocess,retain_validated=args.retain_validated)
    if pidfile.exists():
        old=json.loads(pidfile.read_text())
        try:os.kill(old['pid'],0)
        except ProcessLookupError:pass
        else:raise ValueError('worker_already_running')
    fd=os.open(pidfile,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:json.dump({'pid':os.getpid(),'interpreter':sys.executable,'role':'bounded-worker'},stream)
    def stop(signum,frame):worker.stop=True
    previous=signal.signal(signal.SIGTERM,stop)
    try:return worker.run(max_jobs=1 if args.action=='once' else args.max_jobs,max_calls=args.max_calls,
        max_retries=args.max_retries,max_seconds=args.max_seconds,max_input_tokens=args.max_input_tokens)
    finally:signal.signal(signal.SIGTERM,previous);pidfile.unlink(missing_ok=True)


def verify_package_build(directory):
    directory=Path(directory);manifest_path=directory/'BUILD_ID.json'
    manifest=json.loads(manifest_path.read_text());files=manifest['files']
    if hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()!=manifest['build_id']:
        raise ValueError('runtime_manifest_hash_mismatch')
    for relative,expected in files.items():
        path=directory/relative
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('runtime_file_hash_mismatch:'+relative)
    return {'build_id':manifest['build_id'],'files_verified':len(files),
        'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest()}


def build_identity():
    import agentclient,agenthub
    verified={module.__name__:verify_package_build(Path(module.__file__).parent) for module in (agentclient,agenthub)}
    return {'interpreter':sys.executable,'role':'api-only','client_version':agentclient.__version__,
        'hub_version':agenthub.__version__,'build_ids':{name:value['build_id'] for name,value in verified.items()},'verified_packages':verified,'modules':{module.__name__:{'path':module.__file__,
        'sha256':hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()} for module in (agentclient,agenthub)}}
