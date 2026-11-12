"""Finite owner-operated backend commands and the package build identity."""
import hashlib
import json
from pathlib import Path
import sys

from agenthub.local_connectors import Connector


def parsers(sub):
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
    release=sub.add_parser('reviewed-release');release.add_argument('--token-file',required=True)
    release.add_argument('id');release.add_argument('--revision',required=True);release.add_argument('--project',required=True)
    release.add_argument('--reader',action='append',required=True);release.add_argument('--key',required=True)
    source=sub.add_parser('structured-source');source.add_argument('id');source.add_argument('--token-file',required=True)


def execute(store,args,context):
    if args.command=='structured-source':return store.general_source(context(store,args),args.id)
    if args.command=='reviewed-release':return store.reviewed_release(context(store,args),args.id,args.revision,args.project,args.reader,args.key)
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
    return None


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
