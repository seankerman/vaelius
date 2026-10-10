"""Standard public-client OAuth, with private storage and no model dependencies."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from agentclient.credentials import (LoginRequired, open_store, _sidecar, _read, _write,
    _binding, _request, _scope, check_scope, save_metadata, _token)


def endpoint(url):
    p=urlsplit(url)
    if (p.scheme not in ('http','https') or not p.hostname or p.username or p.password
            or p.fragment or p.query or (p.scheme=='http' and p.hostname not in ('127.0.0.1','localhost','::1'))):
        raise ValueError('unsafe_oauth_endpoint')
    return url


def request(url,data=None):
    endpoint(url)
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise ValueError('oauth_redirect_denied')
    body=urlencode(data).encode() if data is not None else None
    headers={'Content-Type':'application/x-www-form-urlencoded'} if body is not None else {}
    with build_opener(ProxyHandler({}),NoRedirect()).open(Request(url,data=body,headers=headers),timeout=10) as response:
        raw=response.read(128001)
        if len(raw)>128000:raise ValueError('oauth_response_bound')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('oauth_response_object')
        return value


def discover(url,tenant,client_id):
    metadata=request(endpoint(url.rstrip('/'))+'/.well-known/oauth-protected-resource/mcp/'+tenant)
    resource=endpoint(metadata['resource'])
    if urlsplit(resource).netloc!=urlsplit(url).netloc or urlsplit(resource).scheme!=urlsplit(url).scheme:
        raise ValueError('oauth_resource_mismatch')
    issuers=metadata['authorization_servers']
    if not isinstance(issuers,list) or len(issuers)!=1:raise ValueError('explicit_single_tenant_issuer_required')
    issuer=endpoint(issuers[0]);provider=request(issuer.rstrip('/')+'/.well-known/openid-configuration')
    if provider.get('issuer')!=issuer or 'S256' not in provider.get('code_challenge_methods_supported',[]):raise ValueError('oauth_discovery_mismatch')
    for name in ('authorization_endpoint','token_endpoint','revocation_endpoint'):
        endpoint(provider[name])
        if (urlsplit(provider[name]).netloc,urlsplit(provider[name]).scheme)!=(urlsplit(issuer).netloc,urlsplit(issuer).scheme):raise ValueError('oauth_endpoint_origin')
    if not isinstance(client_id,str) or not 1<=len(client_id)<=128:raise ValueError('oauth_client_id_required')
    return {k:provider[k] for k in ('authorization_endpoint','token_endpoint','revocation_endpoint')}|{
        'issuer':issuer,'resource':resource,'client_id':client_id,
        'require_issuer_response':provider.get('authorization_response_iss_parameter_supported',False),
        'scope':'openid '+' '.join(s for s in metadata['scopes_supported'] if s in
            ('vaelius:read','vaelius:source_read','vaelius:ingest','vaelius:feedback'))}


def issued(value):
    if not isinstance(value.get('token_type'),str) or value['token_type'].lower()!='bearer':raise ValueError('oauth_token_type')
    access=_token(value.get('access_token'))
    refresh=_token(value['refresh_token']) if value.get('refresh_token') is not None else None
    if access==refresh or type(value.get('expires_in')) not in (int,float) or not 0<value['expires_in']<=86400:
        raise ValueError('oauth_token_response')
    return access,refresh


def install_return(backend,store,pending,*,cleanup=True):
    if pending['endpoint']!=backend['url'] or pending['issuer']!=backend['oauth']['issuer']:
        raise ValueError('credential_endpoint_changed')
    access=pending['access'];refresh=pending['refresh']
    metadata=_request(backend,access,'/enterprise/v3/auth/adopt',{'enrollment':backend['enrollment_id']})
    check_scope(pending.get('scope'),metadata)
    if metadata['tenant']!=backend['tenant']:raise ValueError('credential_tenant_changed')
    expected=pending.get('identity')
    if expected and any(metadata[k]!=expected[k] for k in ('tenant','principal','actor','enrollment')):
        raise ValueError('credential_identity_changed')
    store.write('access',access)
    if refresh is None:store.delete('refresh')
    else:store.write('refresh',refresh)
    save_metadata(_sidecar(store,'.renewal.json'),backend['url'],access,metadata)
    if cleanup:store.delete('pending')
    return metadata


def finish_login(store,pending):
    backend=pending['backend'];config_path=Path(pending['profile'])/'config.json'
    config=json.loads(_read(config_path)) if config_path.exists() else pending['initial_config']
    previous=config.get('knowledge_backend')
    if previous and any(previous.get(k)!=backend.get(k) for k in ('url','tenant','enrollment_id','credential_file')):
        raise ValueError('explicit_profile_identity_must_match')
    metadata=install_return(backend,store,pending,cleanup=False)
    config['knowledge_backend']=backend;_write(config_path,json.dumps(config,indent=2)+'\n')
    store.delete('pending')
    return metadata


def recover_login(backend):
    store=open_store(backend)
    with os.fdopen(os.open(_sidecar(store,'.lock'),os.O_RDWR|os.O_CREAT,0o600),'r+') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        pending=json.loads(store.read('pending') or '{}')
        if pending.get('phase')!='login_returned':raise LoginRequired()
        metadata=finish_login(store,pending)
        return {'status':'recovered','expires_at':metadata['expires_at']}


def renew(backend,*,force=False):
    store=open_store(backend);state=_sidecar(store,'.renewal.json')
    with os.fdopen(os.open(_sidecar(store,'.lock'),os.O_RDWR|os.O_CREAT,0o600),'r+') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        raw=store.read('pending')
        if raw:
            pending=json.loads(raw)
            if pending.get('phase')=='login_returned':
                metadata=finish_login(store,pending)
                return {'status':'recovered','expires_at':metadata['expires_at']}
            if pending.get('phase')!='returned':raise LoginRequired()
            metadata=install_return(backend,store,pending)
            return {'status':'recovered','expires_at':metadata['expires_at']}
        access=store.read('access')
        if access is None:raise LoginRequired()
        saved=json.loads(_read(state)) if state.exists() else {}
        if saved.get('binding')!=_binding(backend['url'],access.strip()):saved={}
        if not force and saved.get('renew_after',0)>time.time():return {'status':'current','expires_at':saved['expires_at']}
        refresh=store.read('refresh')
        if refresh is None:raise LoginRequired()
        oauth=backend['oauth']
        # No provider-independent retry can distinguish a lost reply from a
        # stolen spent refresh token. Persist intent and fail closed on ambiguity.
        store.write('pending',json.dumps({'phase':'dispatched','endpoint':backend['url'],
            'issuer':oauth['issuer'],'refresh_binding':_binding(oauth['issuer'],refresh.strip())}))
        try:
            value=request(oauth['token_endpoint'],{'grant_type':'refresh_token','client_id':oauth['client_id'],
                'refresh_token':refresh.strip(),'resource':oauth['resource']})
            new_access,new_refresh=issued(value)
        except HTTPError as error:
            if error.code in (400,401,403):raise LoginRequired() from None
            raise
        pending={'phase':'returned','endpoint':backend['url'],'issuer':oauth['issuer'],
                 'access':new_access,'refresh':new_refresh,'scope':_scope(saved)}
        store.write('pending',json.dumps(pending))
        metadata=install_return(backend,store,pending)
        return {'status':'renewed','expires_at':metadata['expires_at']}


def revoke(backend,token):
    # RFC 7009 successful responses may have an empty body.
    oauth=backend['oauth'];url=endpoint(oauth['revocation_endpoint'])
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise ValueError('oauth_redirect_denied')
    body=urlencode({'token':token,'token_type_hint':'refresh_token','client_id':oauth['client_id']}).encode()
    with build_opener(ProxyHandler({}),NoRedirect()).open(Request(url,data=body,
            headers={'Content-Type':'application/x-www-form-urlencoded'}),timeout=10) as response:
        if response.status!=200:raise ValueError('oauth_revocation_unconfirmed')


def enroll(home,*,url,tenant,client_id,enrollment,callback,credential_store='auto',
           reauthenticate=False,browser=None,notify=None,timeout=180):
    from agentclient.cloud_enroll import prepare_home,_credential_store
    if reauthenticate:
        profile=Path(home).expanduser().resolve();config_path=profile/'config.json'
        if config_path.stat().st_mode&0o077:raise ValueError('private_profile_required')
        config=json.loads(config_path.read_text());previous=config['knowledge_backend']
        if previous['url']!=url or previous['tenant']!=tenant or previous['enrollment_id']!=enrollment:
            raise ValueError('explicit_profile_identity_must_match')
        store_kind=previous.get('credential_store','file');anchor=previous['credential_file']
        saved=open_store(previous).read('pending')
        if saved and json.loads(saved).get('phase')=='login_returned':
            recover_login(previous)
            return {'profile':str(profile),'reauthenticated':True,'status':'recovered',
                    'projects_enrolled':len(config['projects']),'client_model_calls':0}
    else:
        profile=prepare_home(home);config_path=profile/'config.json'
        config={'paused':False,'projects':{},'sessions':{},'desktop_transcripts':{},'capture_all_codex_sessions':False,
                'observer':{'enabled':False},'publication':{'enabled':False},'remote_publication':False}
        store_kind=_credential_store(credential_store);anchor=str(profile/'credential')
    oauth=discover(url,tenant,client_id)
    state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    auth=oauth['authorization_endpoint']+'?'+urlencode({'client_id':client_id,'response_type':'code',
        'redirect_uri':callback.uri,'state':state,'scope':oauth['scope'],'resource':oauth['resource'],
        'code_challenge':challenge,'code_challenge_method':'S256'})
    callback.state=state
    if notify:notify({'authorization_url':auth,'expires_at':time.time()+timeout})
    if browser:browser(auth)
    result=callback.wait(timeout)
    if not secrets.compare_digest(result['state'],state):raise ValueError('oauth_state_mismatch')
    # RFC 9207 issuer response parameter is checked before exposing the code.
    if ((oauth.get('require_issuer_response') and result.get('iss') is None)
            or (result.get('iss') is not None and result['iss']!=oauth['issuer'])):raise ValueError('oauth_issuer_mismatch')
    value=request(oauth['token_endpoint'],{'grant_type':'authorization_code','client_id':client_id,
        'code':result['code'],'code_verifier':verifier,'redirect_uri':callback.uri,'resource':oauth['resource']})
    access,refresh=issued(value)
    backend={'mode':'enterprise_local','url':url,'credential_file':anchor,'credential_store':store_kind,
        'transport':'https' if urlsplit(url).scheme=='https' else 'loopback',
        'capture_version':'enterprise-local-2','capture_owner':'transcript','api_version':'cloud-local-1',
        'tenant':tenant,'enrollment_id':enrollment,'credential_renewal':True,'oauth':oauth}
    store=open_store(backend)
    with os.fdopen(os.open(_sidecar(store,'.lock'),os.O_RDWR|os.O_CREAT,0o600),'r+') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        scope=None
        metadata_path=_sidecar(store,'.renewal.json')
        if reauthenticate and metadata_path.exists():scope=_scope(json.loads(_read(metadata_path)))
        # A new login can grant different actions, but cannot switch the profile's
        # tenant/principal/actor. Server adopt also guards the original enrollment.
        pending={'phase':'login_returned','endpoint':url,'issuer':oauth['issuer'],'access':access,'refresh':refresh,
                 'backend':backend,'profile':str(profile),'initial_config':config if not reauthenticate else {},'identity':scope}
        metadata=_request(backend,access,'/enterprise/v3/auth/adopt',{'enrollment':enrollment})
        if scope and any(metadata[k]!=scope[k] for k in ('tenant','principal','actor','enrollment')):
            raise ValueError('credential_identity_changed')
        store.write('pending',json.dumps(pending))
        finish_login(store,pending)
    return {'profile':str(profile),'credential_stored':True,'credential_store':store_kind,
            'refresh_token_stored':refresh is not None,'reauthenticated':reauthenticate,'projects_enrolled':len(config['projects']),
            'client_model_calls':0,'local_corpus_opened':False,'host_hooks_installed':False}
