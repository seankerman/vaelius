"""Crash-recoverable refresh-token rotation for an existing profile; never enroll.

Both replacement tokens are durably prepared before dispatch. A lost reply is
resolved by resending that exact request, which the service answers again while
its successor is unused, never by creating another identity or extending an
expired one. Hooks and the MCP header helper only ever read the access token.

Tokens live in the OS keychain through the optional `keyring` package when the
profile selected it at enrollment (`credential_store: "keyring"`); otherwise, and
for every profile created before refresh tokens existed, in owner-only (0600)
files beside `credential_file`. A profile never silently switches stores.
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

_DENIED = (401, 403, 404)
_SCOPE = ('tenant', 'principal', 'actor', 'enrollment', 'actions')
KEYRING_SERVICE = 'vaelius-client'


class LoginRequired(ValueError):
    """The refresh chain cannot continue; run the OIDC enrollment again."""
    def __init__(self):
        super().__init__('credential_login_required')


def _read(path):
    with path.open() as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError('credential_file_permissions')
        value=stream.read(65537)
    if len(value)>65536:raise ValueError('credential_file_bound')
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


def _token(value):
    value=value.strip() if isinstance(value,str) else None
    if not value or len(value)>16384 or '\n' in value or '\r' in value:raise ValueError('invalid_enterprise_credential')
    return value


def keyring_backend():
    """The usable `keyring` module, or None when absent or without a real backend."""
    try:
        import keyring
        backend=keyring.get_keyring()
        # Priority measures availability, not confidentiality. Never select a
        # plaintext/remote third-party backend under the name "OS keychain".
        secure={('keyring.backends.macOS','Keyring'),
                ('keyring.backends.SecretService','Keyring'),
                ('keyring.backends.Windows','WinVaultKeyring'),
                ('keyring.backends.kwallet','DBusKeyring'),
                ('keyring.backends.kwallet','DBusKeyringKWallet4'),
                ('keyring.backends.kwallet','DBusKeyringKWallet5')}
        if (type(backend).__module__,type(backend).__name__) not in secure:return None
        if getattr(backend,'priority',0)<=0:return None
        return keyring
    except Exception:
        return None


class FileStore:
    """Owner-only files: `credential` (access), `.refresh` and `.pending.json`."""
    kind='file'
    def __init__(self,anchor):
        self.anchor=anchor
        self.paths={'access':anchor,'refresh':anchor.with_name(anchor.name+'.refresh'),
            'pending':anchor.with_name(anchor.name+'.pending.json')}
    def read(self,slot):
        try:return _read(self.paths[slot])
        except FileNotFoundError:return None
    def write(self,slot,value):_write(self.paths[slot],value)
    def create(self,slot,value):
        fd=os.open(self.paths[slot],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(value+'\n');stream.flush();os.fsync(stream.fileno())
    def delete(self,slot):self.paths[slot].unlink(missing_ok=True)


class KeyringStore:
    """OS keychain items keyed by slot and the profile's credential path."""
    kind='keyring'
    def __init__(self,anchor,backend):
        self.anchor=anchor;self.backend=backend
        self.account=hashlib.sha256(str(anchor).encode()).hexdigest()[:32]
    def _name(self,slot):return slot+':'+self.account
    def read(self,slot):return self.backend.get_password(KEYRING_SERVICE,self._name(slot))
    def write(self,slot,value):self.backend.set_password(KEYRING_SERVICE,self._name(slot),value)
    def create(self,slot,value):self.write(slot,value)
    def delete(self,slot):
        if self.read(slot) is None:return
        try:self.backend.delete_password(KEYRING_SERVICE,self._name(slot))
        except Exception:
            if self.read(slot) is not None:raise


def open_store(backend,*,keyring=None):
    anchor=Path(backend['credential_file']).expanduser().resolve()
    kind=backend.get('credential_store','file')
    if kind=='file':return FileStore(anchor)
    if kind=='keyring':
        module=keyring or keyring_backend()
        if module is None:raise ValueError('credential_keyring_unavailable')
        return KeyringStore(anchor,module)
    raise ValueError('unknown_credential_store')


def access_token(backend):
    """The only secret hooks, transports and the MCP header helper receive."""
    value=open_store(backend).read('access')
    if value is None:raise LoginRequired()
    return _token(value)


def _sidecar(store,suffix):return store.anchor.with_name(store.anchor.name+suffix)


def _binding(url,token):return hashlib.sha256((url+'\0'+token).encode()).hexdigest()


def _request(backend,token,path,data=None):
    # Reuse canonical request/response validation without recursive credential lookup.
    # A None token sends no Authorization header (refresh and revocation routes).
    from agentclient.transport import EnterpriseLocal,LoopbackTransport
    client=EnterpriseLocal.__new__(EnterpriseLocal)
    LoopbackTransport.__init__(client,backend['url'],token,timeout=5,transport=backend.get('transport','loopback'))
    return client.request(path,data)


def _scope(value):return {k:value[k] for k in _SCOPE} if all(k in value for k in _SCOPE) else None


def check_scope(expected,metadata):
    actual=_scope(metadata)
    if expected is not None and (actual is None
            or any(actual[k]!=expected[k] for k in _SCOPE if k!='actions')
            or not set(actual['actions'])<=set(expected['actions'])):
        raise ValueError('credential_scope_changed')


def save_metadata(state,url,token,metadata):
    remaining=max(0,metadata['expires_at']-time.time())
    window=min(300,max(5,remaining*.1))
    _write(state,json.dumps({'binding':_binding(url,token),**metadata,
        'renew_after':metadata['expires_at']-window}))


def _finish(store,state,url,pending,metadata):
    # Order matters for recovery: the refresh slot changes last, so while a journal
    # exists the stored refresh token is either the spent original (resend the
    # identical request) or the new one (everything but cleanup already landed).
    store.write('access',pending['replacement_access_token'])
    store.write('refresh',pending['replacement_refresh_token'])
    if metadata is None:state.unlink(missing_ok=True)
    else:save_metadata(state,url,pending['replacement_access_token'],metadata)
    store.delete('pending')


def _complete(backend,store,state,pending):
    url=backend['url']
    if pending.get('endpoint')!=url:raise ValueError('credential_endpoint_changed')
    refresh=store.read('refresh')
    if refresh is not None and refresh.strip()==pending['replacement_refresh_token']:
        _finish(store,state,url,pending,None);return None
    if refresh is None or _binding(url,refresh.strip())!=pending['refresh_binding']:
        raise ValueError('credential_changed_during_renewal')
    try:
        metadata=_request(backend,None,'/enterprise/v3/auth/renew',{'refresh_token':refresh.strip(),
            'replacement_access_token':pending['replacement_access_token'],
            'replacement_refresh_token':pending['replacement_refresh_token']})
    except HTTPError as exc:
        if exc.code not in _DENIED:raise
        # Revoked, reused or expired family: these candidates can never become valid.
        store.delete('pending');raise LoginRequired() from None
    check_scope(pending.get('scope'),metadata)
    _finish(store,state,url,pending,metadata)
    return metadata


def renew(backend,*,force=False):
    pending=open_store(backend).read('pending')
    if pending and json.loads(pending).get('phase')=='login_returned':
        from agentclient.oauth import recover_login
        return recover_login(backend)
    if backend.get('oauth'):
        from agentclient.oauth import renew as oauth_renew
        return oauth_renew(backend,force=force)
    store=open_store(backend);url=backend['url']
    state=_sidecar(store,'.renewal.json');lock=_sidecar(store,'.lock')
    with os.fdopen(os.open(lock,os.O_RDWR|os.O_CREAT,0o600),'r+') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        recovered=False
        raw=store.read('pending')
        if raw is not None:
            _complete(backend,store,state,json.loads(raw))
            # The interrupted renewal (forced or not) has now completed.
            recovered=True;force=False
        access=store.read('access')
        if access is None:raise LoginRequired()
        access=_token(access);binding=_binding(url,access)
        saved=json.loads(_read(state)) if state.exists() else {}
        status='recovered' if recovered else 'current'
        if saved.get('binding')!=binding:saved={}
        if not force and saved.get('renew_after',saved.get('expires_at',0)-300)>time.time():
            return {'status':status,'expires_at':saved['expires_at']}
        old=None
        if not force and not saved:
            try:old=_request(backend,access,'/enterprise/v3/auth/credential')
            except HTTPError as exc:
                if exc.code not in _DENIED:raise
            if old is not None:
                save_metadata(state,url,access,old)
                if old['expires_at']>time.time()+5:return {'status':status,'expires_at':old['expires_at']}
        refresh=store.read('refresh')
        if refresh is None:raise LoginRequired()
        refresh=_token(refresh)
        pending={'endpoint':url,'refresh_binding':_binding(url,refresh),
            'replacement_access_token':secrets.token_urlsafe(48),
            'replacement_refresh_token':secrets.token_urlsafe(48),
            'scope':_scope(old) if old is not None else _scope(saved)}
        store.write('pending',json.dumps(pending))
        metadata=_complete(backend,store,state,pending)
        return {'status':'renewed','expires_at':metadata['expires_at']}


def logout(backend,*,local_only=False):
    """Revoke the token family at the service (RFC 7009), then delete local tokens.

    If revocation cannot be confirmed the local tokens are kept so the call can be
    retried, unless `local_only` explicitly accepts leaving the family live until
    its idle timeout.
    """
    store=open_store(backend);state=_sidecar(store,'.renewal.json');lock=_sidecar(store,'.lock')
    with os.fdopen(os.open(lock,os.O_RDWR|os.O_CREAT,0o600),'r+') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        tokens=[value.strip() for value in (store.read('refresh'),store.read('access')) if value]
        revoked=False
        if tokens and not local_only:
            # Any token of the family revokes all of it; a pending journal's
            # replacements belong to the same family.
            if backend.get('oauth'):
                from agentclient.oauth import revoke
                revoke(backend,tokens[0])
            else:_request(backend,None,'/enterprise/v3/auth/revoke',{'token':tokens[0]})
            revoked=True
        for slot in ('pending','refresh','access'):store.delete(slot)
        state.unlink(missing_ok=True)
    return {'revoked':revoked,'local_credentials_deleted':True}
