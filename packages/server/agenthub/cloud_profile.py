"""Explicit operator provisioning of an isolated, provider-free local profile.

Administrative credentials live only in operator.json. Request routing and tenant
processing use separate login roles with no ownership or schema creation rights.
This creates no source corpus and changes no founder settings or bootstrap routes.
"""
import json
import os
from pathlib import Path
import re
import secrets

from agenthub.postgres import connect, migrate, PostgresEnterpriseStore, TenantRegistry


def _private_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as output:
        json.dump(value,output,indent=2);output.write('\n')
    temporary.chmod(0o600);temporary.replace(path)


def _database(admin_dsn,name,role,password):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    with connect(admin_dsn,autocommit=True) as db:
        existing=db.execute('SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
        if not existing:
            db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT').format(
                sql.Identifier(role),sql.Literal(password)))
        elif not existing['rolcanlogin'] or existing['rolsuper'] or existing['rolcreatedb'] or existing['rolcreaterole']:
            raise ValueError('profile_role_privileges_conflict')
        if not db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        db.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
        db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(name),sql.Identifier(role)))
    operator=make_conninfo(admin_dsn,dbname=name)
    application=make_conninfo(operator,user=role,password=password)
    with connect(operator) as db:
        owner=db.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()").fetchone()[0]
        if owner==role:raise ValueError('application_role_cannot_own_database')
        db.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
    return operator,application


def _grant(admin_dsn,role,*,control=False):
    from psycopg import sql
    with connect(admin_dsn) as db:
        if control:
            db.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(role)))
            # Enrollment/rotation creates digest-only routes, never tenants or policy.
            db.execute(sql.SQL('GRANT INSERT ON cloud_credential_routes TO {}').format(sql.Identifier(role)))
        else:
            schema=db.execute('SELECT current_schema()').fetchone()[0]
            db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
            db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
            db.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
            from agenthub.search_reader import provision
            provision(db,role,schema)


def _seed_identity(store,tenant,profile):
    """Seed synthetic identities and connection enrollment, never source content."""
    with store.open() as state:
        exists=state.db.execute('SELECT 1 FROM enterprise_organizations WHERE id=?',(tenant,)).fetchone()
    if not exists:store.create_organization(tenant)
    for principal in ('alice','bob','settings-admin'):
        with store.open() as state:
            exists=state.db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=?',(tenant,principal)).fetchone()
        if not exists:store.create_principal(tenant,principal,settings_admin=principal=='settings-admin')
    for project in ('cloud-fixtures','enrolled-test'):
        with store.open() as state:
            exists=state.db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=? AND id=?',(tenant,project)).fetchone()
        if not exists:store.create_project(tenant,project)
        for principal in ('alice','bob'):
            with store.open() as state:
                member=state.db.execute('SELECT active FROM enterprise_memberships WHERE tenant=? AND project=? AND principal=?',(tenant,project,principal)).fetchone()
            # Repeat setup never restores a revoked membership.
            if member is None:store.set_membership(tenant,project,principal,True)
    credentials=Path(profile)/'credentials';credentials.mkdir(exist_ok=True,mode=0o700)
    for principal in ('alice','bob','settings-admin'):
        path=credentials/(tenant+'-'+principal+'.token')
        if path.exists():
            if path.stat().st_mode&0o077:raise ValueError('profile_token_permissions')
            continue
        actions=['read','settings','audit'] if principal=='settings-admin' else ['ingest','read','source_read','correct','withdraw','policy','feedback']
        token=store.enroll(tenant,principal,'local-'+principal,actions,expires_in=86400)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:output.write(token+'\n')
    with store.open() as state:
        enrolled=state.db.execute('SELECT 1 FROM backend_connections WHERE id=?',('captured-agent',)).fetchone()
    if not enrolled:
        ctx=store.authenticate((credentials/(tenant+'-alice.token')).read_text().strip())
        store.enroll_connection(ctx,'captured-agent','codex','enrolled-test',['agent'])


def setup_profile(profile,services_path,*,namespace='cloud_profile'):
    """Provision only explicit local databases; return no tokens, DSNs or secrets.

    namespace is injectable for synthetic tests. The production local default is
    cloud_profile_control/acme/bravo and corresponding application roles.
    """
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,40}',namespace):raise ValueError('invalid_profile_namespace')
    profile=Path(profile).expanduser().resolve();profile.mkdir(parents=True,exist_ok=True,mode=0o700);profile.chmod(0o700)
    services=json.loads(Path(services_path).expanduser().read_text())
    from psycopg.conninfo import conninfo_to_dict
    bootstrap=services['admin_dsn']
    if conninfo_to_dict(bootstrap).get('host') not in ('127.0.0.1','localhost','::1'):
        raise ValueError('local_profile_requires_loopback_postgres')
    operator_path=profile/'operator.json'
    if operator_path.exists():
        if operator_path.stat().st_mode&0o077:raise ValueError('profile_operator_permissions')
        operator=json.loads(operator_path.read_text())
        if operator.get('namespace')!=namespace:raise ValueError('profile_namespace_conflict')
    else:
        operator={'namespace':namespace,'role_passwords':{name:secrets.token_urlsafe(32) for name in ('control','acme','bravo')}}
        _private_json(operator_path,operator) # Preserve credentials across interrupted setup.
    control_role=namespace+'_router';control_database=namespace+'_control'
    control_admin,control_app=_database(bootstrap,control_database,control_role,operator['role_passwords']['control'])
    migrate(control_admin,control=True);_grant(control_admin,control_role,control=True)
    operator['control_admin_dsn']=control_admin;operator['control_database']=control_database;operator['control_role']=control_role
    operator['tenants']={}
    registry=TenantRegistry(control_admin,profile/'server-state')
    for tenant in ('acme','bravo'):
        role=namespace+'_'+tenant+'_app';database=namespace+'_'+tenant
        admin,dsn=_database(bootstrap,database,role,operator['role_passwords'][tenant])
        migrate(admin);_grant(admin,role)
        operator['tenants'][tenant]={'admin_dsn':admin,'dsn':dsn,'database':database,'role':role}
        registry.register(tenant,dsn)
        _seed_identity(PostgresEnterpriseStore(profile/'server-state'/tenant,dsn,tenant,registry=registry),tenant,profile)
    _private_json(operator_path,operator)
    runtime_path=profile/'runtime.json'
    runtime=json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
    runtime.update({'provider_mode':'off','control_dsn':control_app,'max_stores':4,
        'allowed_hosts':['127.0.0.1','localhost'],'allowed_origins':[],'api_port':55486})
    runtime.setdefault('semantic',{'enabled':False})
    runtime.setdefault('objects',{'kind':'s3','endpoint':'http://127.0.0.1:55484','bucket':'agentnetwork-cloud-v1',
        'access_key':'synthetic-local','secret_key':'synthetic-local','local_hosts':['127.0.0.1']})
    _private_json(runtime_path,runtime)
    return {'profile':str(profile),'runtime':str(runtime_path),'operator':str(operator_path),
        'databases':[control_database]+[operator['tenants'][t]['database'] for t in ('acme','bravo')],
        'provider_mode':'off','provider_calls':0,'source_corpus_seeded':False}
