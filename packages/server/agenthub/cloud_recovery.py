"""Bounded logical recovery fixtures with matching individual source objects.

This is explicitly not pg_dump, managed-database backup/failover or S3 durability
evidence. Restored fixtures remain offline and held until current authoritative
source/policy/lifecycle state and retained object checksums are reconciled.
"""
from __future__ import annotations

import base64
from datetime import date,datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from agenthub.cloud_ops import hold_restore
from agenthub.source_objects import READ_SIZE, ObjectCorrupt, bounded_spool, verify


def _encode(value):
    if value is None or type(value) in (str,int,float,bool):return value
    if isinstance(value,(bytes,bytearray,memoryview)):
        return {'kind':'binary','value':base64.b64encode(value).decode()}
    if isinstance(value,Decimal):return {'kind':'decimal','value':str(value)}
    if isinstance(value,datetime):return {'kind':'datetime','value':value.isoformat()}
    if isinstance(value,date):return {'kind':'date','value':value.isoformat()}
    if isinstance(value,dict):return {'kind':'mapping','value':[[key,_encode(item)] for key,item in value.items()]}
    if isinstance(value,(list,tuple)):return {'kind':'array','value':[_encode(item) for item in value]}
    raise ValueError('logical_snapshot_type_unsupported')


def _decode(value):
    if not isinstance(value,dict):return value
    kind=value['kind'];raw=value['value']
    if kind=='binary':return base64.b64decode(raw,validate=True)
    if kind=='decimal':return Decimal(raw)
    if kind=='datetime':return datetime.fromisoformat(raw)
    if kind=='date':return date.fromisoformat(raw)
    if kind=='mapping':return {key:_decode(item) for key,item in raw}
    if kind=='array':return [_decode(item) for item in raw]
    raise ValueError('logical_snapshot_type_unsupported')


def _json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def _write(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as output:output.write(raw)


def _catalog(db):
    rows=db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
    result={}
    for row in rows:
        name=row['table_name']
        # Permission projection is rebuildable from the authoritative sources
        # and dependencies; never restore a potentially stale allow projection.
        if name in {'cloud_schema','cloud_recovery_state','search_policies',
                    'search_source_policies','search_document_policies'}:continue
        result[name]=[column['column_name'] for column in db.execute('''SELECT column_name
            FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s
            ORDER BY ordinal_position''',(name,))]
    return result


def _capture(store,*,max_rows=5000,max_bytes=64*1024*1024):
    from psycopg import sql
    if not 1<=max_rows<=100000 or not 1024<=max_bytes<=256*1024*1024:
        raise ValueError('logical_snapshot_bound')
    tables={};total=0;fingerprint=hashlib.sha256()
    with store.open() as state:
        db=state.db.connection;db.rollback()
        db.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for name,columns in _catalog(db).items():
            rows=db.execute(sql.SQL('SELECT {} FROM {} LIMIT %s').format(
                sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(name)),(max_rows+1,)).fetchall()
            if len(rows)>max_rows:raise ValueError('logical_snapshot_row_bound:'+name)
            encoded=[[_encode(row[column]) for column in columns] for row in rows]
            encoded.sort(key=_json)
            raw=_json({'columns':columns,'rows':encoded});total+=len(raw)
            if total>max_bytes:raise ValueError('logical_snapshot_byte_bound')
            sha=hashlib.sha256(raw).hexdigest();fingerprint.update((name+sha).encode())
            tables[name]={'columns':columns,'rows':encoded,'sha256':sha}
        db.rollback()
    return tables,fingerprint.hexdigest(),total


def _locators(tables):
    found={}
    for table,length in [('backend_document_versions','byte_length'),('cloud_migrated_source_objects','length'),
            ('cloud_conversation_segments','byte_length')]:
        if table not in tables:continue
        item=tables[table];columns=item['columns']
        for encoded in item['rows']:
            row=dict(zip(columns,map(_decode,encoded)))
            if table=='cloud_conversation_segments' and row['status']!='active':continue
            key=row['object_key']
            locator={'key':key,'sha256':row['sha256'],'byte_length':row[length],
                'source_id':row['source_id'],'kind':table}
            if key in found and any(found[key][k]!=locator[k] for k in ('sha256','byte_length')):
                raise ObjectCorrupt('logical_snapshot_locator_conflict')
            found[key]=locator
    return list(found.values())


def backup(store,objects,destination,*,max_rows=5000,max_bytes=64*1024*1024,max_objects=100):
    destination=Path(destination).expanduser().resolve()
    if destination.exists():raise ValueError('logical_snapshot_target_exists')
    destination.mkdir(parents=True,mode=0o700);started=time.monotonic()
    tables,fingerprint,length=_capture(store,max_rows=max_rows,max_bytes=max_bytes)
    locators=_locators(tables)
    if len(locators)>max_objects:raise ValueError('logical_snapshot_object_bound')
    if length+sum(item['byte_length'] for item in locators)>max_bytes:
        raise ValueError('logical_snapshot_byte_bound')
    table_manifest={}
    for name,item in tables.items():
        path=destination/'tables'/(name+'.json');raw=_json({'columns':item['columns'],'rows':item['rows']})
        _write(path,raw);table_manifest[name]={'columns':item['columns'],'rows':len(item['rows']),
            'file':'tables/'+name+'.json','sha256':hashlib.sha256(raw).hexdigest()}
    for locator in locators:
        name=hashlib.sha256(locator['key'].encode()).hexdigest()+'.bin';path=destination/'objects'/name
        verify(objects.head(locator['key']),locator['byte_length'],locator['sha256'])
        with objects.open(locator['key']) as source,bounded_spool(source,expected_sha256=locator['sha256']) as (spool,count,_):
            if count!=locator['byte_length']:raise ObjectCorrupt('logical_snapshot_source_length')
            path.parent.mkdir(mode=0o700,exist_ok=True)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out:
                while block:=spool.read(READ_SIZE):out.write(block)
        locator['file']='objects/'+name
    manifest={'version':'logical_recovery_fixture_v1','kind':'bounded logical fixture, not managed database backup',
        'tenant':store.tenant_id,'tables':table_manifest,'objects':locators,'fingerprint':fingerprint,
        'table_bytes':length,'elapsed_seconds':time.monotonic()-started,'provider_calls':0,
        'max_rows':max_rows,'max_bytes':max_bytes,'max_objects':max_objects,'created':time.time()}
    _write(destination/'manifest.json',_json(manifest))
    return manifest


def _load(snapshot):
    root=Path(snapshot).expanduser().resolve();manifest=json.loads((root/'manifest.json').read_bytes())
    if manifest.get('version')!='logical_recovery_fixture_v1':raise ValueError('logical_snapshot_version')
    tables={}
    for name,item in manifest['tables'].items():
        path=(root/item['file']).resolve()
        if not path.is_relative_to(root):raise ValueError('logical_snapshot_path_escape')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ObjectCorrupt('logical_snapshot_table_checksum')
        data=json.loads(raw)
        if data['columns']!=item['columns'] or len(data['rows'])!=item['rows']:raise ObjectCorrupt('logical_snapshot_table_shape')
        tables[name]=data
    return root,manifest,tables


def _restore(snapshot,target,objects,admin_dsn,*,require_empty):
    from psycopg import sql
    from agenthub.postgres import connect
    root,manifest,tables=_load(snapshot)
    if manifest['tenant']!=target.tenant_id:raise ValueError('logical_snapshot_tenant_mismatch')
    hold_restore(target,'restore_missing_post_backup_deltas',{'snapshot':str(root),'fingerprint':manifest['fingerprint']})
    # Originals are verified before installation; a missing/corrupt object leaves
    # the target held. Files remain individual; no archive packing is introduced.
    for locator in manifest['objects']:
        path=(root/locator['file']).resolve()
        if not path.is_relative_to(root):raise ValueError('logical_snapshot_path_escape')
        with path.open('rb') as source:
            receipt=objects.put(locator['key'],source,expected_sha256=locator['sha256'])
        verify(receipt,locator['byte_length'],locator['sha256'])
        verify(objects.head(locator['key']),locator['byte_length'],locator['sha256'])
    with connect(admin_dsn) as db:
        catalog=_catalog(db)
        if set(catalog)!=set(tables) or any(catalog[name]!=tables[name]['columns'] for name in tables):
            raise ValueError('logical_snapshot_schema_mismatch')
        if require_empty:
            for name in tables:
                if name in {'knowledge_index_state','knowledge_temporal_schema','enterprise_schema'}:continue
                if db.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(name))).fetchone():
                    raise ValueError('logical_restore_target_not_empty:'+name)
        db.execute('SET LOCAL session_replication_role=replica')
        db.execute(sql.SQL('TRUNCATE {} CASCADE').format(sql.SQL(',').join(map(sql.Identifier,tables))))
        for name,item in tables.items():
            if not item['rows']:continue
            with db.cursor().copy(sql.SQL('COPY {} ({}) FROM STDIN').format(sql.Identifier(name),
                    sql.SQL(',').join(map(sql.Identifier,item['columns'])))) as copied:
                for row in item['rows']:copied.write_row([_decode(value) for value in row])
        db.execute('SELECT search_rebuild_permissions()')
        # Identity sequences must advance after restoring explicit IDs.
        identities=db.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND is_identity='YES'").fetchall()
        for row in identities:
            db.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,%s),COALESCE((SELECT max({}) FROM {}),1),EXISTS(SELECT 1 FROM {}))").format(
                sql.Identifier(row['column_name']),sql.Identifier(row['table_name']),sql.Identifier(row['table_name'])),
                (row['table_name'],row['column_name']))
    hold_restore(target,'restore_missing_post_backup_deltas',{'snapshot':str(root),'fingerprint':manifest['fingerprint']})
    return manifest


def restore_snapshot(snapshot,target,objects,*,admin_dsn):
    manifest=_restore(snapshot,target,objects,admin_dsn,require_empty=True)
    return {'restored':True,'ready':False,'reason':'post_backup_source_policy_deltas_unreconciled',
        'fingerprint':manifest['fingerprint'],'objects':len(manifest['objects']),
        'kind':'offline logical fixture','cutover_performed':False,'provider_calls':0}


def reconcile_restore(snapshot,target,live_authority,target_objects,live_objects,*,admin_dsn,destination):
    _,baseline,_=_load(snapshot)
    if target.tenant_id!=live_authority.tenant_id:raise ValueError('logical_reconcile_tenant_mismatch')
    current,fingerprint,_=_capture(target,max_rows=baseline['max_rows'],max_bytes=baseline['max_bytes'])
    if fingerprint!=baseline['fingerprint']:
        raise ValueError('logical_reconcile_target_has_unpreserved_writes')
    with live_authority.delivery_lock():
        latest=backup(live_authority,live_objects,destination,max_rows=baseline['max_rows'],
            max_bytes=baseline['max_bytes'],max_objects=baseline['max_objects'])
        _restore(destination,target,target_objects,admin_dsn,require_empty=False)
        _,restored,_=_capture(target,max_rows=baseline['max_rows'],max_bytes=baseline['max_bytes'])
        if restored!=latest['fingerprint']:raise ObjectCorrupt('logical_reconcile_state_mismatch')
        for locator in latest['objects']:
            verify(target_objects.head(locator['key']),locator['byte_length'],locator['sha256'])
        with target.open() as state,state.db:
            state.db.execute('''UPDATE cloud_recovery_state SET ready=1,reason='logical_current_authority_reconciled',
                manifest=%s,updated=%s WHERE singleton=1''',
                (json.dumps({'fingerprint':latest['fingerprint'],'source_snapshot':str(destination),'offline_fixture':True}),time.time()))
    old_journal=baseline['tables'].get('enterprise_deletion_journal',{}).get('rows',0)
    new_journal=latest['tables'].get('enterprise_deletion_journal',{}).get('rows',0)
    return {'ready':True,'source_policy_delta_journal_rows':new_journal-old_journal,
        'fingerprint':latest['fingerprint'],'objects_verified':len(latest['objects']),
        'post_backup_writes_preserved':True,'kind':'offline bounded logical fixture',
        'cutover_performed':False,'managed_backup_evidence':False,'provider_calls':0}


def offline_target(path,registry):
    """Admit explicit private, unregistered, nonadministrative restore targets."""
    from agenthub.postgres import connect
    from agenthub.cloud_runtime import CloudStore
    from agenthub.source_objects import FileSourceObjects
    source=Path(path).expanduser().resolve()
    if source.stat().st_mode&0o077:raise ValueError('offline_target_settings_permissions')
    settings=json.loads(source.read_bytes())
    if (settings.get('kind')!='offline_restore_target_v1' or settings.get('serve') is not False
            or settings.get('dispatch') is not False):raise ValueError('explicit_offline_restore_target_required')
    def identity(db):
        row=db.execute('SELECT current_database() db,inet_server_addr()::text address,inet_server_port() port').fetchone()
        return row['db'],row['address'],row['port']
    with connect(settings['dsn']) as db:
        target_identity=identity(db)
        role=db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        owns=db.execute('SELECT pg_get_userbyid(datdba)=current_user FROM pg_database WHERE datname=current_database()').fetchone()[0]
        if any(role.values()) or owns:raise ValueError('offline_application_role_required')
    with connect(settings['admin_dsn']) as db:
        if identity(db)!=target_identity:raise ValueError('offline_target_admin_database_mismatch')
    # Compare actual server/database identities so connection-string aliases do
    # not bypass the route check. No routing or credential registrations change.
    with registry.open_control() as db:
        routes=[row['dsn'] for row in db.execute('SELECT dsn FROM cloud_tenants')]
    for dsn in routes:
        with connect(dsn) as db:
            if identity(db)==target_identity:raise ValueError('offline_target_is_registered_authority')
    if settings['objects'].get('kind')!='file':raise ValueError('offline_private_file_objects_required')
    target=CloudStore(Path(settings['home']),settings['dsn'],settings['tenant'])
    return target,FileSourceObjects(settings['objects']['root']),settings['admin_dsn']


def main(argv=None):
    """Explicit actual offline restore/reconcile commands; no synthetic alias."""
    import argparse
    from agenthub.cloud_runtime import read_settings, registry_from_settings
    from agenthub.object_config import objects_from_settings
    parser=argparse.ArgumentParser(description='Private bounded logical offline recovery; no route cutover')
    parser.add_argument('command',choices=('restore','reconcile'))
    parser.add_argument('--current-profile',required=True)
    parser.add_argument('--target-config',required=True)
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--output',help='new private current-authority snapshot directory, required for reconcile')
    args=parser.parse_args(argv)
    if args.command=='reconcile' and not args.output:parser.error('--output new private directory required')
    os.umask(0o077)
    profile=Path(args.current_profile).expanduser().resolve()
    settings=read_settings(profile/'runtime.json');registry=registry_from_settings(settings,profile/'server-state')
    target,objects,administrator=offline_target(args.target_config,registry)
    if args.command=='restore':
        result=restore_snapshot(args.snapshot,target,objects,admin_dsn=administrator)
    else:
        source=registry.resolve(target.tenant_id)
        result=reconcile_restore(args.snapshot,target,source,objects,objects_from_settings(settings),
            admin_dsn=administrator,destination=args.output)
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
