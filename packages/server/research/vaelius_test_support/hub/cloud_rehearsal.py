"""Explicit offline migration and installed-code rollback around one authority."""
import json
import os
from pathlib import Path
import subprocess
import uuid

def migrate_selected(profile):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from agenthub.postgres import connect, migrate
    from agenthub.cloud_runtime import CloudStore
    from agenthub.cloud_migration import migrate_snapshot
    from agenthub.source_objects import FileSourceObjects
    profile=Path(profile);selection=json.loads((profile/'migration-selection.json').read_text())
    operator=json.loads((profile/'operator.json').read_text());admin=operator['control_admin_dsn']
    name='cloud_migration_rehearsal_'+uuid.uuid4().hex[:12]
    with connect(admin,autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn=make_conninfo(admin,dbname=name);migrate(dsn)
    directory=profile/'migration-runs'/name
    store=CloudStore(directory/'state',dsn,selection['tenant'])
    result=migrate_snapshot(selection['snapshot'],store,expected_sha256=selection['sha256'],
        objects=FileSourceObjects(directory/'objects'),allow_missing_originals=selection.get('allow_missing_originals',False))
    private=directory/'receipt.json';private.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    private.write_text(json.dumps(result,indent=2));private.chmod(0o600)
    # Offline rehearsal was never routed. Preserve its private receipt and objects.
    with connect(admin,autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    return {'status':result['status'],'tables':len(result['counts']),'objects':len(result['objects']),
        'original_gaps':len(result['original_gaps']),'snapshot_sha256':selection['sha256'],'receipt':str(private),
        'live_route_changed':False,'provider_calls':0}

FINGERPRINT_TABLES=('enterprise_sources','enterprise_lifecycle','knowledge_documents',
    'knowledge_revisions','enterprise_audit','enterprise_deletion_journal',
    'enterprise_memberships','backend_connections','cloud_segment_uploads',
    'cloud_conversation_segments')

def fingerprint(store):
    from agenthub.cloud_migration import _row_digest
    with store.open() as state:
        return {table:_row_digest([dict(row) for row in state.db.execute('SELECT * FROM '+table)])
            for table in FINGERPRINT_TABLES}

def rollback_code(profile,store):
    profile=Path(profile);selection=json.loads((profile/'rollback-selection.json').read_text())
    # Resolving a virtualenv's Python symlink selects the base interpreter and
    # loses the installed environment. Preserve the configured executable path.
    interpreter=Path(selection['previous_interpreter']).expanduser().absolute()
    if not interpreter.is_file() or not os.access(interpreter,os.X_OK):
        raise ValueError('previous_installed_interpreter_unavailable')
    before=fingerprint(store);env=dict(os.environ);env.pop('PYTHONPATH',None)
    # Exercise the previous installed application against this same current data.
    code="""import json,sys
from agenthub.cloud_local import runtime
from agenthub.cloud_migration import _row_digest
from agenthub.backend_ops import build_identity
_,registry=runtime(sys.argv[1]);store=registry.resolve(sys.argv[2])
with store.open() as state:
    tables=json.loads(sys.argv[3])
    fingerprint={table:_row_digest([dict(row) for row in state.db.execute('SELECT * FROM '+table)]) for table in tables}
from agenthub.backend_worker import Worker
summary={}
for tenant in ('acme','bravo'):
    current=registry.resolve(tenant)
    with current.open() as state:
        counts={table:state.db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('enterprise_sources','knowledge_documents','backend_document_versions')}
        schemas=[r[0] for r in state.db.execute('SELECT version FROM cloud_schema ORDER BY version')]
    worker=Worker.__new__(Worker);worker.store=current
    summary[tenant]={'counts':counts,'schema_versions':schemas,'queue':worker.status()}
# Probe previous canonical storage/worker code, not a newer operator command.
# Raw sources, credentials and provider usage never enter the receipt.
print(json.dumps({'runtime':build_identity(),'fingerprint':fingerprint,'status':summary}))
"""
    result=subprocess.run([str(interpreter),'-c',code,str(profile),store.tenant_id,json.dumps(FINGERPRINT_TABLES)],cwd='/tmp',env=env,
        capture_output=True,text=True,timeout=30)
    if result.returncode:raise ValueError('previous_installed_code_failed')
    previous=json.loads(result.stdout);after=fingerprint(store)
    if before!=after or before!=previous['fingerprint']:raise ValueError('rollback_authority_state_changed')
    if previous['runtime']['build_ids']!=selection['previous_build_ids']:raise ValueError('rollback_previous_build_mismatch')
    return {'strategy':'previous_installed_code_same_postgresql_authority','installed_previous_build':True,
        'previous_build_ids':previous['runtime']['build_ids'],'state_preserved':True,'current_sources_queried':True,
        'sqlite_restored':False,'compatibility_probe':'current_data_schema_queue_and_lifecycle_policy_fingerprints',
        'read_only_probe':True,'writers_or_dispatchers_activated':False,
        'provider_calls':0}

def rehearse(profile,store,settings,*,migration=False,restore=False,rollback=False):
    result={'provider_calls':0,'live_route_changed':False}
    if migration:result['migration']=migrate_selected(profile)
    if restore:
        from vaelius_test_support.hub.recovery_rehearsal import rehearse as recovery
        result['restore']=recovery(profile,store,settings)
    if rollback:result['rollback']=rollback_code(profile,store)
    if len(result)==2:raise ValueError('select_migration_restore_or_rollback')
    return result
