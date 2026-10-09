"""Crash-recoverable rotation of an existing service credential; never enroll.

The replacement is durably prepared before dispatch. A lost reply is resolved by
checking that exact token, not creating another identity or extending an expired one.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time
from urllib.error import HTTPError


def _read(path):
    with path.open() as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError('credential_file_permissions')
        value=stream.read(8193)
    if len(value)>8192:raise ValueError('credential_file_bound')
    return value


def _write(path,value):
    temporary=path.with_name(path.name+'.tmp-'+secrets.token_hex(8))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(value);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _request(backend,token,path,data=None):
    # Reuse canonical request/response validation without recursive credential lookup.
    from agentclient.transport import EnterpriseLocal,LoopbackTransport
    client=EnterpriseLocal.__new__(EnterpriseLocal)
    LoopbackTransport.__init__(client,backend['url'],token,timeout=5,transport=backend.get('transport','loopback'))
    return client.request(path,data)


def renew(backend,*,force=False):
    path=Path(backend['credential_file']).expanduser().resolve()
    state=path.with_name(path.name+'.renewal.json');journal=path.with_name(path.name+'.pending.json')
    lock=path.with_name(path.name+'.lock')
    with os.fdopen(os.open(lock,os.O_RDWR|os.O_CREAT,0o600),'r+') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        token=_read(path).strip()
        binding=hashlib.sha256((backend['url']+'\0'+token).encode()).hexdigest()
        def request(value,route,data=None):return _request(backend,value,route,data)
        def scope(value):return {k:value[k] for k in ('tenant','principal','actor','enrollment','actions')}
        def finish(candidate,metadata):
            _write(path,candidate)
            _write(state,json.dumps({'binding':hashlib.sha256((backend['url']+'\0'+candidate).encode()).hexdigest(),**metadata}))
            journal.unlink(missing_ok=True)
        if journal.exists():
            pending=json.loads(_read(journal))
            if pending['endpoint']!=backend['url']:raise ValueError('credential_endpoint_changed')
            candidate=pending['replacement_token']
            if pending['old_binding']!=binding and token!=candidate:raise ValueError('credential_changed_during_renewal')
            try:metadata=request(candidate,'/enterprise/v3/auth/credential')
            except HTTPError as exc:
                if exc.code not in (401,403):raise
                metadata=None
            if metadata is not None:
                if scope(metadata)!=pending['scope']:raise ValueError('credential_scope_changed')
                finish(candidate,metadata);return {'status':'recovered','expires_at':metadata['expires_at']}
            # A pending dispatch may not have reached the server. The old token
            # must still authenticate; the same replacement makes this recoverable.
            old=request(token,'/enterprise/v3/auth/credential')
            if scope(old)!=pending['scope']:raise ValueError('credential_scope_changed')
        else:
            saved=json.loads(_read(state)) if state.exists() else {}
            if not force and saved.get('binding')==binding and saved.get('expires_at',0)>time.time()+300:
                return {'status':'current','expires_at':saved['expires_at']}
            old=request(token,'/enterprise/v3/auth/credential')
            if not force and old['expires_at']>time.time()+300:
                _write(state,json.dumps({'binding':binding,**old}));return {'status':'current','expires_at':old['expires_at']}
            candidate=secrets.token_urlsafe(48)
            _write(journal,json.dumps({'endpoint':backend['url'],'old_binding':binding,
                'replacement_token':candidate,'scope':scope(old)}))
        metadata=request(token,'/enterprise/v3/auth/renew',{'replacement_token':candidate})
        if scope(metadata)!=scope(old):raise ValueError('credential_scope_changed')
        finish(candidate,metadata)
        return {'status':'renewed','expires_at':metadata['expires_at']}
