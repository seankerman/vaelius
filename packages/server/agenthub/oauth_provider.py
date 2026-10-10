"""OAuth resource-server boundary; the configured issuer owns token lifecycle.

RFC 7662 introspection is deliberately fresh at authentication/delivery. No
refresh token or user password is stored here. Application permissions still run
through CloudIdentityMixin and the indexed search-permission implementation.
"""
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

from agenthub.cloud_identity import IdentityError, _endpoint
from agenthub.enterprise import Denied, Conflict, _digest
from agenthub.postgres import TenantRegistry

ACTIONS={'ingest','read','source_read','feedback','curate','correct','withdraw','policy','audit','settings'}


class OAuthProvider:
    def __init__(self,settings,*,http=None):
        allowed={'issuer','tenant','client_ids','introspection_client_id','client_secret_file','actions'}
        if not isinstance(settings,dict) or set(settings)-allowed:raise ValueError('oauth_provider_configuration')
        self.issuer=_endpoint(settings['issuer']).rstrip('/')
        self.tenant=settings['tenant'];self.client_ids=settings['client_ids']
        self.actions=set(settings.get('actions',ACTIONS))
        if (not isinstance(self.tenant,str) or not self.tenant or not self.client_ids
                or not isinstance(self.client_ids,list) or len(self.client_ids)>32
                or any(not isinstance(x,str) or not x for x in self.client_ids)
                or not self.actions or not self.actions<=ACTIONS):raise ValueError('oauth_provider_configuration')
        self.client_id=settings['introspection_client_id']
        from agenthub.secret_files import read_secret
        self.secret=read_secret(settings['client_secret_file'])
        if http is None:
            import requests
            http=requests.Session();http.trust_env=False
        self.http=http;self._metadata=None;self._loaded=0;self._lock=threading.Lock()

    def metadata(self):
        with self._lock:
            if self._metadata is not None and time.monotonic()-self._loaded<300:return self._metadata
            try:
                response=self.http.get(self.issuer+'/.well-known/openid-configuration',timeout=5,allow_redirects=False)
                if response.status_code!=200 or len(response.content)>128000:raise ValueError()
                value=response.json()
                if value['issuer']!=self.issuer or 'S256' not in value.get('code_challenge_methods_supported',[]):raise ValueError()
                for name in ('authorization_endpoint','token_endpoint','revocation_endpoint','introspection_endpoint'):
                    endpoint=_endpoint(value[name])
                    if urlsplit(endpoint).netloc!=urlsplit(self.issuer).netloc or urlsplit(endpoint).scheme!=urlsplit(self.issuer).scheme:raise ValueError()
                self._metadata=value;self._loaded=time.monotonic()
                return value
            except Exception:raise IdentityError('oauth_provider_unavailable') from None

    def introspect(self,token,resource):
        if not isinstance(token,str) or not 1<=len(token)<=16384:raise Denied()
        try:
            response=self.http.post(self.metadata()['introspection_endpoint'],
                data={'token':token,'token_type_hint':'access_token'},
                auth=(self.client_id,self.secret),timeout=5,allow_redirects=False)
            if response.status_code!=200 or len(response.content)>128000:raise ValueError()
            value=response.json();aud=value.get('aud',[])
            aud=[aud] if isinstance(aud,str) else aud
            if (value.get('active') is not True or value.get('iss',self.issuer)!=self.issuer
                    or not isinstance(aud,list) or resource not in aud
                    or value.get('client_id') not in self.client_ids
                    or not isinstance(value.get('sub'),str) or not 1<=len(value['sub'])<=256
                    or type(value.get('exp')) not in (int,float) or value['exp']<=time.time()
                    or not isinstance(value.get('scope'),str)
                    or value.get('token_type','Bearer').lower()!='bearer'
                    or value.get('typ','Bearer') not in ('Bearer','at+jwt')):raise ValueError()
            actions={s.removeprefix('vaelius:') for s in value['scope'].split() if s.startswith('vaelius:')}&self.actions
            if not actions:raise ValueError()
            return value,actions
        except Exception:raise Denied() from None


class OAuthRegistry(TenantRegistry):
    def __init__(self,*args,authorization,**kwargs):
        super().__init__(*args,**kwargs)
        if (set(authorization)-{'resource','providers','legacy_interactive'}
                or type(authorization.get('legacy_interactive',False)) is not bool):raise ValueError('oauth_configuration')
        self.resource=_endpoint(authorization['resource'])
        if urlsplit(self.resource).query or urlsplit(self.resource).fragment:raise ValueError('oauth_resource')
        items=authorization['providers']
        if not isinstance(items,list) or not 1<=len(items)<=32:raise ValueError('oauth_provider_bound')
        self.providers=[OAuthProvider(item) for item in items]
        if len({p.issuer for p in self.providers})!=len(items):raise ValueError('duplicate_oauth_issuer')
        self.legacy_interactive=authorization.get('legacy_interactive',False)

    def protected_metadata(self,tenant=None):
        providers=[p for p in self.providers if tenant is None or p.tenant==tenant]
        if not providers:raise FileNotFoundError()
        return {'resource':self.resource,'authorization_servers':[p.issuer for p in providers],
                'scopes_supported':['vaelius:'+a for a in sorted(set().union(*(p.actions for p in providers)))],
                'bearer_methods_supported':['header']}

    def authenticate_token(self,token,request_id=None):
        # Explicit operator/service and unmigrated access credentials keep the
        # canonical path. Provider credentials never bypass fresh introspection.
        if not isinstance(token,str) or not 1<=len(token)<=16384:raise Denied()
        existing=None
        try:
            store=self.store_for_token(token)
            with store.open() as state:
                row=state.db.execute('SELECT * FROM enterprise_credentials WHERE digest=?',(_digest(token),)).fetchone()
            if row and row['oauth_provider'] is None:return store,store.authenticate(token,request_id)
            existing=row
        except Denied:pass
        issuer=existing['oauth_provider'] if existing else None
        if issuer is None:
            # An unverified JWT issuer is ONLY a routing hint into this fixed
            # allowlist. Fresh authenticated introspection decides validity.
            import base64
            try:
                payload=token.split('.')[1]
                issuer=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4))).get('iss')
            except (IndexError,ValueError,TypeError,AttributeError):pass
        providers=[p for p in self.providers if p.issuer==issuer] if issuer else self.providers
        if len(providers)!=1:raise Denied()
        for provider in providers:
            try:value,actions=provider.introspect(token,self.resource)
            except Denied:continue
            store=self.resolve(provider.tenant)
            identity={'issuer':provider.issuer,'subject':value['sub'],'provider':value.get('identity_provider','native')}
            binding=store.identity_bindings.resolve(identity,provider.tenant)
            enrollment='oauth-'+hashlib.sha256((provider.issuer+'\0'+value['sub']+'\0'+value['client_id']).encode()).hexdigest()[:32]
            if existing:
                if (existing['tenant']!=provider.tenant or existing['principal']!=binding['principal']
                        or existing['identity_subject']!=value['sub']):raise Denied()
            else:
                with store.open() as state,state.db:
                    state.db.execute('''INSERT INTO enterprise_credentials
                    (digest,tenant,principal,enrollment,actions,active,created,expires_at,
                     identity_issuer,identity_subject,identity_provider,oauth_provider)
                    VALUES(?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING''',
                    (_digest(token),provider.tenant,binding['principal'],enrollment,json.dumps(sorted(actions)),
                     time.time(),value['exp'],provider.issuer,value['sub'],identity['provider'],provider.issuer))
                self.bind_credential(token,provider.tenant)
            ctx=store.authenticate(token,request_id)
            ctx['actions']&=actions
            return store,ctx
        raise Denied()


def adopt_enrollment(store,ctx,enrollment):
    """Bind an existing identity's provider token to its explicit capture profile.

    Tokens remain issuer-owned; this only preserves the application enrollment.
    A login as a different principal cannot take over an old profile/connection.
    """
    import re
    if not isinstance(enrollment,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',enrollment):raise ValueError('invalid_enrollment')
    with store.delivery_lock(),store.open() as state,state.db:
        store.current_identity(state.db,ctx)
        row=state.db.execute('SELECT oauth_provider FROM enterprise_credentials WHERE digest=?',(ctx['credential_digest'],)).fetchone()
        if not row or row['oauth_provider'] is None:raise Denied()
        previous=state.db.execute('''SELECT principal,acting_for FROM enterprise_credentials
            WHERE tenant=? AND enrollment=? AND (principal!=? OR acting_for IS NOT NULL) LIMIT 1''',
            (ctx['tenant'],enrollment,ctx['principal'])).fetchone()
        if previous:raise Conflict()
        state.db.execute('UPDATE enterprise_credentials SET enrollment=? WHERE digest=?',(enrollment,ctx['credential_digest']))
        store._audit(state.db,ctx,'oauth_enrollment','bound',enrollment)
    ctx=dict(ctx,enrollment=enrollment)
    return store.credential_status(ctx)


def prepare_legacy_rollback(store):
    """Explicit operator fence before running a binary without provider auth.

    Never restore these grants on rollback. A later new login can issue a new
    provider token, while old digests and their revocation evidence stay retired.
    """
    with store.delivery_lock(),store.open() as state,state.db:
        count=state.db.execute('UPDATE enterprise_credentials SET active=0 WHERE oauth_provider IS NOT NULL AND active=1').rowcount
        store._audit(state.db,{'tenant':store.tenant_id,'actor':'local_operator'},
            'oauth_rollback','provider_credentials_fenced',str(count))
    return {'tenant':store.tenant_id,'provider_credentials_fenced':count}
