"""Disposable PostgreSQL schemas for retained algorithm tests; no alternate store.

Schemas survive connection reopen and are dropped at process exit. Supplying a
services manifest opts into database tests; this never reads the user's corpus.
"""
import atexit
import json
import os
from pathlib import Path
import unittest
import uuid

_SCHEMAS={}
_ADMINS={}

def database(home, *, admin=False):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from agenthub.postgres import connect, migrate
    from agenthub.cloud_profile import _grant
    manifest=os.environ.get('AGENTNETWORK_PG_SERVICES')
    if not manifest:raise unittest.SkipTest('explicit disposable PostgreSQL manifest required')
    key=str(Path(home).resolve())
    if key not in _SCHEMAS:
        item=json.loads(Path(manifest).read_text())['tenants']['acme']
        schema='test_alg_'+uuid.uuid4().hex[:16]
        with connect(item['admin_dsn']) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        options='-c search_path='+schema+',public'
        admin_dsn=make_conninfo(item['admin_dsn'],options=options)
        migrate(admin_dsn);_grant(admin_dsn,item['role'])
        def cleanup():
            from agenthub.search_reader import reader_role
            with connect(item['admin_dsn']) as db:
                role=reader_role(db.execute('SELECT current_database()').fetchone()[0],schema,item['role'])
                db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
                db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        atexit.register(cleanup)
        _ADMINS[key]=admin_dsn
        _SCHEMAS[key]=make_conninfo(item['dsn'],options=options)
    return _ADMINS[key] if admin else _SCHEMAS[key]
