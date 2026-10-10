"""Verified external login and current enterprise identity lifecycle.

No coding-agent credentials are consumed here. OIDC protocol/cryptography use
Authlib and joserfc; local HTTP is allowed only for loopback broker fixtures.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit


class IdentityError(Exception):
    """Content-free authentication failure suitable for a public error response."""


# Operator-configurable credential lifetimes (runtime.json "credentials").
# Access tokens are short-lived bearer credentials; a refresh token is accepted
# only at the renewal route, slides by the idle timeout on each rotation, and can
# never outlive the absolute lifetime measured from the original OIDC login.
DEFAULT_CREDENTIAL_LIFETIMES = {'access_ttl_seconds': 3600,
    'refresh_idle_seconds': 30 * 86400, 'refresh_absolute_seconds': 90 * 86400}
_ISSUED_TOKEN = re.compile(r'[A-Za-z0-9_-]{64}')


def validate_credential_lifetimes(value=None):
    """Return complete lifetimes; omitted keys keep their defaults."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CREDENTIAL_LIFETIMES):
        raise ValueError('runtime_credential_configuration')
    merged = dict(DEFAULT_CREDENTIAL_LIFETIMES, **value)
    if (any(type(item) is not int for item in merged.values())
            or not 60 <= merged['access_ttl_seconds'] <= 86400
            or not merged['access_ttl_seconds'] < merged['refresh_idle_seconds']
                <= merged['refresh_absolute_seconds'] <= 366 * 86400):
        raise ValueError('runtime_credential_configuration')
    return merged


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _endpoint(value, *, callback=False):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.fragment or
            (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('insecure_identity_endpoint')
    if callback and parsed.query:
        raise ValueError('invalid_callback')
    return value


class OIDCBroker:
    """A configured issuer; token contents never select endpoints or algorithms."""
    def __init__(self, *, issuer, client_id, authorization_endpoint, token_endpoint,
                 jwks_uri, redirect_uri, fetch_json=None, clock=time.time,
                 cache_seconds=300, refresh_seconds=5):
        self.issuer = _endpoint(issuer).rstrip('/')
        self.client_id = client_id
        self.authorization_endpoint = _endpoint(authorization_endpoint)
        self.token_endpoint = _endpoint(token_endpoint)
        self.jwks_uri = _endpoint(jwks_uri)
        issuer_origin = (urlsplit(issuer).scheme, urlsplit(issuer).netloc)
        if any((urlsplit(v).scheme, urlsplit(v).netloc) != issuer_origin
               for v in (authorization_endpoint, token_endpoint, jwks_uri)):
            raise ValueError('issuer_endpoint_origin_mismatch')
        self.redirect_uri = _endpoint(redirect_uri, callback=True)
        self.clock = clock
        self.fetch_json = fetch_json or self._fetch_json
        self.cache_seconds = cache_seconds
        self.refresh_seconds = refresh_seconds
        self._keys = None
        self._loaded = float('-inf')
        self._attempted = float('-inf')
        self._lock = threading.Lock()

    @staticmethod
    def _fetch_json(url):
        import requests
        response = requests.get(url, timeout=5, allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 128000:
            raise IdentityError('identity_keys_unavailable')
        return response.json()

    def _refresh(self, *, force=False):
        from joserfc.jwk import KeySet
        with self._lock:
            age = self.clock() - self._loaded
            if self._keys is not None and age < (self.refresh_seconds if force else self.cache_seconds):
                return
            if self.clock()-self._attempted < self.refresh_seconds:
                if self._keys is not None and age < self.cache_seconds:return
                raise IdentityError('identity_keys_unavailable')
            self._attempted=self.clock()
            try:
                value = self.fetch_json(self.jwks_uri)
                if not isinstance(value, dict) or not 1 <= len(value.get('keys', [])) <= 100:
                    raise ValueError()
                self._keys = KeySet.import_key_set(value)
                self._loaded = self.clock()
            except Exception:
                raise IdentityError('identity_keys_unavailable') from None

    def verify(self, raw, *, nonce=None):
        from joserfc import jwt
        if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
            raise IdentityError('identity_denied')
        self._refresh()
        try:
            token = jwt.decode(raw, self._keys, algorithms=['RS256'])
        except Exception:
            # Unknown kid or rotated signing key can refresh at most once within
            # the configured interval. A stream of invalid tokens cannot amplify
            # outbound requests. An expired cache never falls back to old keys.
            self._refresh(force=True)
            try:
                token = jwt.decode(raw, self._keys, algorithms=['RS256'])
            except Exception:
                raise IdentityError('identity_denied') from None
        try:
            jwt.JWTClaimsRegistry(now=int(self.clock()), leeway=30,
                iss={'essential': True, 'value': self.issuer},
                sub={'essential': True}, aud={'essential': True, 'value': self.client_id},
                exp={'essential': True}, iat={'essential': True}).validate(token.claims)
            # Permit a small issuing-clock skew while keeping credential expiry
            # strict: tokens whose explicit expiry has passed are never accepted.
            if token.claims['exp']<=self.clock():raise ValueError()
            if not isinstance(token.claims['sub'], str) or not 1 <= len(token.claims['sub']) <= 256:
                raise ValueError()
            if nonce is not None and not hmac.compare_digest(str(token.claims.get('nonce', '')), nonce):
                raise ValueError()
            if isinstance(token.claims.get('aud'), list) and len(token.claims['aud']) > 1:
                if token.claims.get('azp') != self.client_id:
                    raise ValueError()
            provider = token.claims.get('identity_provider', 'native')
            if not isinstance(provider, str) or len(provider) > 128:
                raise ValueError()
        except Exception:
            raise IdentityError('identity_denied') from None
        return {'issuer': self.issuer, 'subject': token.claims['sub'], 'provider': provider}

    def begin(self):
        from authlib.integrations.requests_client import OAuth2Session
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with OAuth2Session(self.client_id, redirect_uri=self.redirect_uri, scope='openid',
                           code_challenge_method='S256') as session:
            url, returned_state = session.create_authorization_url(self.authorization_endpoint,
                state=state, code_verifier=verifier, nonce=nonce)
        return {'url': url, 'state': returned_state, 'nonce': nonce, 'verifier': verifier,
                'redirect_uri': self.redirect_uri, 'issuer': self.issuer,
                'expires': self.clock() + 300}

    def exchange(self, code, request, *, callback_uri):
        from authlib.integrations.requests_client import OAuth2Session
        if (callback_uri != self.redirect_uri or request.get('redirect_uri') != self.redirect_uri
                or request.get('issuer') != self.issuer or request.get('expires', 0) <= self.clock()
                or not isinstance(code, str) or not 1 <= len(code) <= 4096):
            raise IdentityError('identity_callback_denied')
        try:
            with OAuth2Session(self.client_id, redirect_uri=self.redirect_uri, scope='openid',
                               token_endpoint_auth_method='none') as session:
                token = session.fetch_token(self.token_endpoint, code=code,
                    code_verifier=request['verifier'], timeout=8, allow_redirects=False)
            return self.verify(token['id_token'], nonce=request['nonce'])
        except Exception:
            raise IdentityError('identity_callback_denied') from None


class IdentityBindings:
    """Control-plane issuer/subject bindings; no knowledge is stored here.

    `open_control` yields a database connection with execute()/transaction().
    Binding is an explicit operator action, never email-based automatic joining.
    """
    def __init__(self, open_control):
        self.open_control = open_control

    def initialize(self):
        # Serving may only assert readiness. DDL belongs to the operator's static
        # control-plane migration, outside ordinary application privileges.
        with self.open_control() as db:
            db.execute('SELECT issuer,subject,tenant,principal,active,federation_required,allowed_providers,epoch FROM cloud_identity_bindings LIMIT 0')

    def bind(self, issuer, subject, tenant, principal, *, federation_required=False,
             allowed_providers=('native',), active=True):
        if not issuer or not subject or not tenant or not principal or not allowed_providers:
            raise ValueError('invalid_identity_binding')
        with self.open_control() as db, db.transaction():
            db.execute('''INSERT INTO cloud_identity_bindings
                (issuer,subject,tenant,principal,active,federation_required,allowed_providers)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(issuer,subject,tenant)
                DO UPDATE SET principal=excluded.principal,active=excluded.active,
                federation_required=excluded.federation_required,allowed_providers=excluded.allowed_providers,
                epoch=cloud_identity_bindings.epoch+1''',
                (issuer,subject,tenant,principal,int(active),int(federation_required),json.dumps(sorted(set(allowed_providers)))))

    def resolve(self, identity, tenant):
        with self.open_control() as db:
            row = db.execute('''SELECT * FROM cloud_identity_bindings
                WHERE issuer=%s AND subject=%s AND tenant=%s''',
                (identity['issuer'],identity['subject'],tenant)).fetchone()
            policy=db.execute('SELECT * FROM cloud_identity_tenant_policy WHERE tenant=%s',(tenant,)).fetchone()
        if (not row or not row['active'] or identity['provider'] not in json.loads(row['allowed_providers'])
                or (row['federation_required'] and identity['provider'] == 'native')
                or (policy and (identity['provider'] not in json.loads(policy['allowed_providers'])
                    or (policy['federation_required'] and identity['provider']=='native')))):
            raise IdentityError('identity_denied')
        return dict(row)

    def set_tenant_policy(self,tenant,*,federation_required=False,allowed_providers=('native',)):
        """Operator control-plane policy, also enforced for existing credentials."""
        if (type(federation_required) is not bool or not isinstance(allowed_providers,(list,tuple))
                or not 1<=len(allowed_providers)<=100 or any(not isinstance(p,str) or not 1<=len(p)<=128 for p in allowed_providers)):
            raise ValueError('invalid_identity_tenant_policy')
        with self.open_control() as db,db.transaction():
            db.execute('''INSERT INTO cloud_identity_tenant_policy(tenant,federation_required,allowed_providers)
                VALUES(%s,%s,%s) ON CONFLICT(tenant) DO UPDATE SET federation_required=excluded.federation_required,
                allowed_providers=excluded.allowed_providers,epoch=cloud_identity_tenant_policy.epoch+1''',
                (tenant,int(federation_required),json.dumps(sorted(set(allowed_providers)))))

    def check(self, issuer, subject, tenant, principal, provider):
        row = self.resolve({'issuer': issuer, 'subject': subject, 'provider': provider}, tenant)
        if row['principal'] != principal:
            raise IdentityError('identity_denied')
        return row


def normalize_scim(resource, value):
    """Bounded SCIM full-resource Users/Groups replacement, no privileged roles.

    PATCH filtering, bulk operations and arbitrary extension schemas are explicit
    unsupported operations. Email/userName remains descriptive, never identity.
    """
    if not isinstance(value, dict) or resource not in ('Users', 'Groups'):
        raise ValueError('unsupported_scim_resource')
    allowed = ({'schemas','id','externalId','userName','displayName','active','meta'}
               if resource == 'Users' else {'schemas','id','externalId','displayName','members','meta'})
    if set(value) - allowed:
        raise ValueError('unsupported_scim_fields')
    ident = value.get('id') or value.get('externalId')
    if not isinstance(ident, str) or not 1 <= len(ident) <= 256:
        raise ValueError('invalid_scim_id')
    if resource == 'Users':
        if type(value.get('active', True)) is not bool:
            raise ValueError('invalid_scim_active')
        return {'kind':'user','external_id':ident,'active':value.get('active',True),
                'display':str(value.get('displayName') or value.get('userName') or ident)[:256]}
    members = value.get('members', [])
    if not isinstance(members,list) or len(members)>1000:
        raise ValueError('invalid_scim_members')
    for member in members:
        if (not isinstance(member,dict) or set(member)-{'value','display'}
                or not isinstance(member.get('value'),str) or not 1<=len(member['value'])<=256):
            raise ValueError('invalid_scim_member')
    return {'kind':'group','external_id':ident,'active':True,
            'members':sorted({m['value'] for m in members}), 'display':str(value.get('displayName') or ident)[:256]}


class CloudIdentityMixin:
    """PostgreSQL enterprise identity extension; current checks precede delivery."""
    credential_lifetimes = DEFAULT_CREDENTIAL_LIFETIMES

    def enroll(self, tenant, principal, enrollment, actions, *, acting_for=None,
               expires_in=None, external_identity=None):
        """Operator/service enrollment: one access credential, no refresh token."""
        return self._issue(tenant, principal, enrollment, actions, acting_for=acting_for,
            expires_in=expires_in, external_identity=external_identity, refresh=False)['access_token']

    def enroll_with_refresh(self, tenant, principal, enrollment, actions, *, acting_for=None,
                            external_identity=None):
        """Interactive login: access token plus a rotating refresh token (one family)."""
        return self._issue(tenant, principal, enrollment, actions, acting_for=acting_for,
            expires_in=None, external_identity=external_identity, refresh=True)

    def _issue(self, tenant, principal, enrollment, actions, *, acting_for, expires_in,
               external_identity, refresh):
        from agenthub.enterprise import Denied, Conflict, _digest
        lifetimes = self.credential_lifetimes
        if expires_in is None:
            expires_in = lifetimes['access_ttl_seconds']
        if tenant != self.tenant_id or type(expires_in) is not int or not 60<=expires_in<=86400:
            raise ValueError('invalid_credential_lifetime')
        if external_identity:
            binding = self.identity_bindings.resolve(external_identity, tenant)
            if binding['principal'] != principal:
                raise Denied()
        allowed={'ingest','read','source_read','feedback','curate','correct','withdraw','policy','audit','settings'}
        if not isinstance(enrollment,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',enrollment):
            raise ValueError('invalid_enrollment')
        if not actions or set(actions)-allowed:raise ValueError('invalid_actions')
        token=secrets.token_urlsafe(48)
        refresh_token=secrets.token_urlsafe(48) if refresh else None
        identity=external_identity or {}
        family=secrets.token_hex(16);now=time.time()
        absolute=now+lifetimes['refresh_absolute_seconds']
        expires_at=min(now+expires_in,absolute)
        refresh_expires=min(now+lifetimes['refresh_idle_seconds'],absolute)
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db
            active=db.execute('SELECT active FROM enterprise_principals WHERE tenant=%s AND id=%s',(tenant,principal)).fetchone()
            if not active or not active['active']:raise Denied()
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('enrollment:'+tenant+':'+enrollment,))
            prior=db.execute('SELECT principal,acting_for FROM enterprise_credentials WHERE tenant=%s AND enrollment=%s LIMIT 1',
                (tenant,enrollment)).fetchone()
            if prior and (prior['principal']!=principal or prior['acting_for']!=acting_for):raise Conflict()
            if acting_for:
                grant=db.execute('''SELECT * FROM enterprise_delegations
                    WHERE tenant=%s AND principal=%s AND acting_for=%s AND active=1''',
                    (tenant,principal,acting_for)).fetchone()
                represented=db.execute('SELECT active FROM enterprise_principals WHERE tenant=%s AND id=%s',(tenant,acting_for)).fetchone()
                if not grant or not represented or not represented['active'] or not set(actions)<=set(json.loads(grant['actions'])):raise Denied()
            scope=json.dumps(sorted(set(actions)))
            db.execute('''INSERT INTO enterprise_credential_families
                (id,tenant,principal,acting_for,enrollment,actions,identity_issuer,identity_subject,
                identity_provider,created,absolute_expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (family,tenant,principal,acting_for,enrollment,scope,identity.get('issuer'),
                 identity.get('subject'),identity.get('provider'),now,absolute))
            db.execute('''INSERT INTO enterprise_credentials
                (digest,tenant,principal,acting_for,enrollment,actions,active,created,expires_at,
                identity_issuer,identity_subject,identity_provider,family)
                VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)''',
                (_digest(token),tenant,principal,acting_for,enrollment,scope,now,expires_at,
                 identity.get('issuer'),identity.get('subject'),identity.get('provider'),family))
            if refresh:
                db.execute('''INSERT INTO enterprise_refresh_tokens(digest,family,active,created,idle_expires_at)
                    VALUES(%s,%s,1,%s,%s)''',(_digest(refresh_token),family,now,refresh_expires))
        if getattr(self,'registry',None):
            self.registry.bind_credential(token,tenant)
            if refresh:self.registry.bind_credential(refresh_token,tenant)
        result={'access_token':token,'expires_at':expires_at}
        if refresh:
            result.update(refresh_token=refresh_token,refresh_expires_at=refresh_expires,
                session_expires_at=absolute)
        return result

    def authenticate(self, token, request_id=None):
        from agenthub.enterprise import Denied, _digest
        ctx = super().authenticate(token,request_id)
        if ctx['tenant'] != self.tenant_id:
            raise Denied()
        with self.open() as state:
            row = state.db.execute('SELECT * FROM enterprise_credentials WHERE digest=%s',(_digest(token),)).fetchone()
        if row['expires_at'] <= time.time():
            raise Denied()
        if row['identity_issuer']:
            try:
                self.identity_bindings.check(row['identity_issuer'],row['identity_subject'],ctx['tenant'],
                    ctx['principal'],row['identity_provider'])
            except (AttributeError,IdentityError):
                raise Denied() from None
        ctx['credential_digest'] = _digest(token)
        ctx['credential_expires'] = row['expires_at']
        return ctx

    def current_identity(self, db, ctx):
        """Recheck credentials and tenant membership at the final policy checkpoint."""
        from agenthub.enterprise import Denied
        if ctx.get('tenant') != self.tenant_id:
            raise Denied()
        if getattr(self,'registry',None):self.registry.require_active(self.tenant_id)
        row = db.execute('''SELECT c.*,p.active principal_active,a.active represented_active
            FROM enterprise_credentials c JOIN enterprise_principals p
            ON p.tenant=c.tenant AND p.id=c.principal LEFT JOIN enterprise_principals a
            ON a.tenant=c.tenant AND a.id=c.acting_for WHERE c.digest=%s''',
            (ctx.get('credential_digest',''),)).fetchone()
        if (not row or not row['active'] or not row['principal_active'] or row['expires_at']<=time.time()
                or (row['acting_for'] and not row['represented_active'])):
            raise Denied()
        if (row['principal']!=ctx['principal'] or row['tenant']!=ctx['tenant']
                or (row['acting_for'] or row['principal'])!=ctx['actor']):raise Denied()
        ctx['actions'] &= set(json.loads(row['actions']))
        if row['identity_issuer']:
            try:
                self.identity_bindings.check(row['identity_issuer'],row['identity_subject'],ctx['tenant'],
                    ctx['principal'],row['identity_provider'])
            except (AttributeError,IdentityError): raise Denied() from None
        if row['acting_for']:
            grant = db.execute('''SELECT * FROM enterprise_delegations
                WHERE tenant=%s AND principal=%s AND acting_for=%s AND active=1''',
                (ctx['tenant'],ctx['principal'],ctx['actor'])).fetchone()
            if not grant: raise Denied()
            ctx['actions'] &= set(json.loads(grant['actions']))
            ctx['delegated_projects'] = set(json.loads(grant['projects']))
        return ctx

    def credential_status(self,ctx):
        with self.open() as state:
            self.current_identity(state.db,ctx)
            row=state.db.execute('SELECT expires_at FROM enterprise_credentials WHERE digest=%s',(ctx['credential_digest'],)).fetchone()
        return {k:(sorted(ctx[k]) if k=='actions' else ctx[k]) for k in
                ('tenant','principal','actor','enrollment','actions')} | {'expires_at':row['expires_at']}

    def rotate_credential(self, ctx, *, expires_in=3600, replacement_token=None):
        """Replace the presented access token without extending its lifetime.

        Lifetime extension is only available through a refresh token, so an access
        token alone can never outlive its own expiry or its family's absolute limit.
        """
        from agenthub.enterprise import _digest
        if type(expires_in) is not int or not 60<=expires_in<=86400:
            raise ValueError('invalid_credential_lifetime')
        token = replacement_token if replacement_token is not None else secrets.token_urlsafe(48)
        if not isinstance(token,str) or not _ISSUED_TOKEN.fullmatch(token):raise ValueError('invalid_replacement_credential')
        with self.delivery_lock(),self.open() as state,state.db:
            self.current_identity(state.db,ctx)
            old = state.db.execute('SELECT * FROM enterprise_credentials WHERE digest=%s FOR UPDATE',
                (ctx['credential_digest'],)).fetchone()
            self.current_identity(state.db,ctx)
            state.db.execute('UPDATE enterprise_credentials SET active=0 WHERE digest=%s',(old['digest'],))
            state.db.execute('''INSERT INTO enterprise_credentials
                (digest,tenant,principal,acting_for,enrollment,actions,active,created,expires_at,
                identity_issuer,identity_subject,identity_provider,rotated_from,family)
                VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s)''',
                (_digest(token),old['tenant'],old['principal'],old['acting_for'],old['enrollment'],
                 old['actions'],time.time(),min(time.time()+expires_in,old['expires_at']),old['identity_issuer'],
                 old['identity_subject'],old['identity_provider'],old['digest'],old['family']))
            self._audit(state.db,ctx,'credential_rotation','applied',_digest(token))
        if getattr(self,'registry',None):self.registry.bind_credential(token,ctx['tenant'])
        return token

    def _family_identity(self, db, family, request_id=None):
        """Current principal, delegation and external-binding checks for a family."""
        from agenthub.enterprise import Denied
        if getattr(self,'registry',None):self.registry.require_active(self.tenant_id)
        row=db.execute('''SELECT p.active principal_active,a.active represented_active
            FROM enterprise_principals p LEFT JOIN enterprise_principals a ON a.tenant=p.tenant AND a.id=%s
            WHERE p.tenant=%s AND p.id=%s''',(family['acting_for'],family['tenant'],family['principal'])).fetchone()
        if not row or not row['principal_active'] or (family['acting_for'] and not row['represented_active']):
            raise Denied()
        actions=set(json.loads(family['actions']))
        if family['acting_for']:
            grant=db.execute('''SELECT actions FROM enterprise_delegations
                WHERE tenant=%s AND principal=%s AND acting_for=%s AND active=1''',
                (family['tenant'],family['principal'],family['acting_for'])).fetchone()
            if not grant:raise Denied()
            actions&=set(json.loads(grant['actions']))
        if family['identity_issuer']:
            try:
                self.identity_bindings.check(family['identity_issuer'],family['identity_subject'],
                    family['tenant'],family['principal'],family['identity_provider'])
            except (AttributeError,IdentityError):raise Denied() from None
        return {'tenant':family['tenant'],'principal':family['principal'],
            'actor':family['acting_for'] or family['principal'],'acting_for':family['acting_for'],
            'enrollment':family['enrollment'],'actions':actions,'request_id':request_id}

    @staticmethod
    def _revoke_family(db, family, reason):
        """Deactivate every access and refresh token of a family; True if newly revoked."""
        changed=bool(db.execute('''UPDATE enterprise_credential_families SET revoked_at=%s,revoked_reason=%s
            WHERE id=%s AND revoked_at IS NULL''',(time.time(),reason,family['id'])).rowcount)
        db.execute('UPDATE enterprise_credentials SET active=0 WHERE family=%s AND active=1',(family['id'],))
        db.execute('UPDATE enterprise_refresh_tokens SET active=0 WHERE family=%s AND active=1',(family['id'],))
        return changed

    def refresh_credential(self, refresh_token, *, replacement_access_token, replacement_refresh_token,
                           request_id=None):
        """Rotate a refresh token (RFC 9700 section 4.14.2) into a new access/refresh pair.

        The client prepares both replacements before dispatch, so a lost reply is
        retried with identical values. That exact retry is answered again while its
        successor is unused; any other presentation of a spent refresh token is
        treated as theft and revokes the whole family. Revocation commits before the
        denial is returned.
        """
        from agenthub.enterprise import Denied, _digest
        if (not isinstance(refresh_token,str) or not 1<=len(refresh_token)<=256
                or any(not isinstance(value,str) or not _ISSUED_TOKEN.fullmatch(value)
                       for value in (replacement_access_token,replacement_refresh_token))
                or replacement_access_token==replacement_refresh_token):
            raise ValueError('invalid_credential_renewal')
        lifetimes=self.credential_lifetimes;presented=_digest(refresh_token)
        access_digest=_digest(replacement_access_token);refresh_digest=_digest(replacement_refresh_token)
        outcome='denied';ctx=None
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db;now=time.time()
            row=db.execute('''SELECT r.active refresh_active,r.idle_expires_at,r.used_at,r.successor_digest,
                r.successor_access_digest,f.* FROM enterprise_refresh_tokens r
                JOIN enterprise_credential_families f ON f.id=r.family
                WHERE r.digest=%s FOR UPDATE OF r,f''',(presented,)).fetchone()
            if not row or row['tenant']!=self.tenant_id or row['revoked_at'] is not None:
                pass
            elif row['used_at'] is not None:
                successor=db.execute('''SELECT active,used_at,idle_expires_at FROM enterprise_refresh_tokens
                    WHERE digest=%s''',(row['successor_digest'],)).fetchone()
                if (hmac.compare_digest(row['successor_digest'] or '',refresh_digest)
                        and hmac.compare_digest(row['successor_access_digest'] or '',access_digest)
                        and successor and successor['active'] and successor['used_at'] is None
                        and now<successor['idle_expires_at']):
                    outcome='replayed'
                else:
                    self._revoke_family(db,row,'refresh_token_reuse')
                    self._audit(db,{'tenant':row['tenant'],'actor':row['acting_for'] or row['principal'],
                        'request_id':request_id},'credential_refresh_reuse','revoked',row['id'])
                    outcome='reuse'
            elif row['refresh_active'] and now<row['idle_expires_at'] and now<row['absolute_expires_at']:
                outcome='rotate'
            if outcome in ('rotate','replayed'):
                try:ctx=self._family_identity(db,row,request_id)
                except Denied:outcome='denied'
            if outcome=='rotate':
                access_expires=min(now+lifetimes['access_ttl_seconds'],row['absolute_expires_at'])
                refresh_expires=min(now+lifetimes['refresh_idle_seconds'],row['absolute_expires_at'])
                db.execute('''UPDATE enterprise_refresh_tokens SET active=0,used_at=%s,successor_digest=%s,
                    successor_access_digest=%s WHERE digest=%s''',(now,refresh_digest,access_digest,presented))
                db.execute('UPDATE enterprise_credentials SET active=0 WHERE family=%s AND active=1',(row['id'],))
                db.execute('''INSERT INTO enterprise_credentials
                    (digest,tenant,principal,acting_for,enrollment,actions,active,created,expires_at,
                    identity_issuer,identity_subject,identity_provider,rotated_from,family)
                    VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s)''',
                    (access_digest,row['tenant'],row['principal'],row['acting_for'],row['enrollment'],
                     row['actions'],now,access_expires,row['identity_issuer'],row['identity_subject'],
                     row['identity_provider'],presented,row['id']))
                db.execute('''INSERT INTO enterprise_refresh_tokens(digest,family,active,created,idle_expires_at)
                    VALUES(%s,%s,1,%s,%s)''',(refresh_digest,row['id'],now,refresh_expires))
                self._audit(db,ctx,'credential_refresh','applied',row['id'])
            elif outcome=='replayed':
                access_expires=db.execute('SELECT expires_at FROM enterprise_credentials WHERE digest=%s',
                    (access_digest,)).fetchone()['expires_at']
                refresh_expires=successor['idle_expires_at']
                self._audit(db,ctx,'credential_refresh','replayed',row['id'])
        if outcome not in ('rotate','replayed'):raise Denied()
        if getattr(self,'registry',None):
            # Route binding is idempotent, so a replay repairs a binding lost to a crash.
            self.registry.bind_credential(replacement_access_token,self.tenant_id)
            self.registry.bind_credential(replacement_refresh_token,self.tenant_id)
        return {'tenant':ctx['tenant'],'principal':ctx['principal'],'actor':ctx['actor'],
            'enrollment':ctx['enrollment'],'actions':sorted(ctx['actions']),'expires_at':access_expires,
            'refresh_expires_at':refresh_expires,'session_expires_at':row['absolute_expires_at']}

    def revoke_credential_family(self, token, *, request_id=None):
        """RFC 7009 revocation by possession of any access or refresh token of a family.

        Idempotent: expired, spent or already revoked tokens still identify their
        family, and an unknown token is acknowledged without effect so this route
        is not a token-validity oracle. Pre-family credentials revoke only themselves.
        """
        from agenthub.enterprise import _digest
        if not isinstance(token,str) or not 1<=len(token)<=256:raise ValueError('invalid_revocation_request')
        digest=_digest(token)
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db
            access=db.execute('SELECT * FROM enterprise_credentials WHERE digest=%s',(digest,)).fetchone()
            family_id=access['family'] if access else None
            if not access:
                refresh=db.execute('SELECT family FROM enterprise_refresh_tokens WHERE digest=%s',(digest,)).fetchone()
                family_id=refresh['family'] if refresh else None
            family=db.execute('SELECT * FROM enterprise_credential_families WHERE id=%s FOR UPDATE',
                (family_id,)).fetchone() if family_id else None
            if family and family['tenant']==self.tenant_id:
                changed=self._revoke_family(db,family,'revoked_by_holder')
                self._audit(db,{'tenant':family['tenant'],'actor':family['acting_for'] or family['principal'],
                    'request_id':request_id},'credential_revocation','applied' if changed else 'already_revoked',family['id'])
            elif access and family_id is None and access['tenant']==self.tenant_id:
                changed=bool(db.execute('UPDATE enterprise_credentials SET active=0 WHERE digest=%s AND active=1',
                    (digest,)).rowcount)
                self._audit(db,{'tenant':access['tenant'],'actor':access['acting_for'] or access['principal'],
                    'request_id':request_id},'credential_revocation','applied' if changed else 'already_revoked',digest)
        return {'revoked':True}

    def begin_enrollment(self, broker):
        request = broker.begin()
        with self.open() as state,state.db:
            state.db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('login-admission:'+self.tenant_id,))
            state.db.execute('DELETE FROM cloud_login_states WHERE expires_at<=%s',(time.time(),))
            pending=state.db.execute('SELECT count(*) FROM cloud_login_states').fetchone()[0]
            if pending>=1000:raise IdentityError('identity_admission_denied')
            state.db.execute('INSERT INTO cloud_login_states(digest,request_json,expires_at) VALUES(%s,%s,%s)',
                (_sha(request['state']),json.dumps(request),request['expires']))
        return {'url':request['url'],'state':request['state'],'expires_at':request['expires']}

    def complete_enrollment(self, broker, *, state, code, callback_uri, enrollment, actions):
        if not isinstance(state,str) or not 20<=len(state)<=256:
            raise IdentityError('identity_callback_denied')
        with self.open() as current,current.db:
            row = current.db.execute('DELETE FROM cloud_login_states WHERE digest=%s RETURNING *',(_sha(state),)).fetchone()
        if not row or row['expires_at']<=time.time():
            raise IdentityError('identity_callback_denied')
        identity = broker.exchange(code,json.loads(row['request_json']),callback_uri=callback_uri)
        binding = self.identity_bindings.resolve(identity,self.tenant_id)
        return self.enroll_with_refresh(self.tenant_id,binding['principal'],enrollment,actions,
            external_identity=identity)

    def _visible_source(self, db, ctx, source, *, raw=False):
        from agenthub.enterprise import Denied
        try: self.current_identity(db,ctx)
        except Denied: return False
        freshness = db.execute('SELECT * FROM cloud_source_acl WHERE source_id=%s',
            (source['id'],)).fetchone() if source else None
        if freshness and (freshness['state']!='current' or freshness['valid_until']<=time.time()):
            return False
        return super()._visible_source(db,ctx,source,raw=raw)

    def set_source_acl_freshness(self, ctx, source_id, *, valid_seconds=300, state='current'):
        self._need(ctx,'policy')
        if state not in ('current','unknown','stale') or not 0<=valid_seconds<=86400:
            raise ValueError('invalid_acl_freshness')
        from agenthub.enterprise import Denied
        with self.delivery_lock(),self.open() as current,current.db:
            self.current_identity(current.db,ctx)
            source = current.db.execute('SELECT * FROM enterprise_sources WHERE id=%s',(source_id,)).fetchone()
            if not source or source['tenant']!=ctx['tenant'] or source['owner']!=ctx['actor']: raise Denied()
            current.db.execute('''INSERT INTO cloud_source_acl(source_id,state,valid_until,epoch)
                VALUES(%s,%s,%s,1) ON CONFLICT(source_id) DO UPDATE SET state=excluded.state,
                valid_until=excluded.valid_until,epoch=cloud_source_acl.epoch+1''',
                (source_id,state,time.time()+valid_seconds))
            current.db.execute('UPDATE enterprise_sources SET policy_version=policy_version+1 WHERE id=%s',(source_id,))
            self._audit(current.db,ctx,'source_acl','applied',source_id)

    def _connection_executor_authorized(self,db,connection):
        if not super()._connection_executor_authorized(db,connection):return False
        try:self.processing_identity(db,connection)
        except Exception:return False
        sources=db.execute('''SELECT a.* FROM cloud_source_acl a JOIN backend_source_revisions r
            ON r.source_id=a.source_id WHERE r.connection=%s''',(connection['id'],)).fetchall()
        return all(s['state']=='current' and s['valid_until']>time.time() for s in sources)

    def processing_identity(self,db,connection):
        """Resolve processing attribution from a current enrollment, not prose."""
        from agenthub.enterprise import Denied
        rows=db.execute('''SELECT * FROM enterprise_credentials WHERE tenant=%s AND enrollment=%s
            AND active=1 AND expires_at>%s ORDER BY created DESC''',
            (connection['tenant'],connection['enrollment'],time.time())).fetchall()
        for row in rows:
            actor=row['acting_for'] or row['principal']
            if actor!=connection['owner']:continue
            ctx={'tenant':row['tenant'],'principal':row['principal'],'actor':actor,
                'acting_for':row['acting_for'],'enrollment':row['enrollment'],
                'actions':set(json.loads(row['actions'])),'delegated_projects':None,
                'credential_digest':row['digest'],'request_id':'backend-observer',
                'processing_connection':connection['id']}
            try:self.current_identity(db,ctx)
            except Denied:continue
            if 'ingest' in ctx['actions'] and self._project_member(db,ctx,connection['project']):return ctx
        raise Denied()

    def bind_directory_user(self, ctx, external_id, principal):
        self._need(ctx,'settings')
        from agenthub.enterprise import Denied
        with self.open() as current,current.db:
            self.current_identity(current.db,ctx)
            self._directory_admin(current.db,ctx)
            if not current.db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=%s AND id=%s',
                                     (ctx['tenant'],principal)).fetchone(): raise Denied()
            current.db.execute('''INSERT INTO cloud_directory_users(external_id,principal) VALUES(%s,%s)
                ON CONFLICT(external_id) DO UPDATE SET principal=excluded.principal''',(external_id,principal))

    def bind_directory_group(self, ctx, external_id, project):
        self._need(ctx,'settings')
        from agenthub.enterprise import Denied
        with self.open() as current,current.db:
            self.current_identity(current.db,ctx)
            self._directory_admin(current.db,ctx)
            if not current.db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=%s AND id=%s',
                                     (ctx['tenant'],project)).fetchone(): raise Denied()
            current.db.execute('''INSERT INTO cloud_directory_group_projects(external_id,project)
                VALUES(%s,%s) ON CONFLICT DO NOTHING''',(external_id,project))

    def apply_directory_event(self, ctx, *, resource, value, sequence, key, reconcile=False, deleted=False):
        from agenthub.enterprise import Conflict, Denied
        self._need(ctx,'settings')
        normalized = normalize_scim(resource,value)
        if type(sequence) is not int or sequence<1 or not isinstance(key,str) or not 1<=len(key)<=128:
            raise ValueError('invalid_directory_event')
        if deleted: normalized['active'] = False
        event_hash = _sha(json.dumps([resource,normalized,sequence,reconcile],sort_keys=True))
        with self.delivery_lock(),self.open() as current,current.db:
            db = current.db
            self.current_identity(db,ctx)
            self._directory_admin(db,ctx)
            prior = db.execute('SELECT * FROM cloud_directory_events WHERE key=%s',(key,)).fetchone()
            if prior:
                if prior['payload_hash']!=event_hash: raise Conflict()
                return {'disposition':'duplicate','requires_reconcile':bool(prior['requires_reconcile'])}
            # Advisory lock serializes absent rows as well as updates to this
            # resource. The canonical delivery lock also fences install/delivery.
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('directory:'+resource+':'+normalized['external_id'],))
            old = db.execute('SELECT * FROM cloud_directory_resources WHERE kind=%s AND external_id=%s FOR UPDATE',
                (normalized['kind'],normalized['external_id'])).fetchone()
            ambiguous = bool(old and sequence<=old['sequence'] and not reconcile)
            if normalized['kind']=='user':
                binding = db.execute('SELECT principal FROM cloud_directory_users WHERE external_id=%s',
                    (normalized['external_id'],)).fetchone()
                if not binding: raise Denied()
                db.execute('UPDATE enterprise_principals SET active=%s WHERE tenant=%s AND id=%s',
                    (int(normalized['active'] and not ambiguous),ctx['tenant'],binding['principal']))
            db.execute('''INSERT INTO cloud_directory_resources(kind,external_id,sequence,active,payload_json,requires_reconcile)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(kind,external_id) DO UPDATE SET
                sequence=excluded.sequence,active=excluded.active,payload_json=excluded.payload_json,
                requires_reconcile=excluded.requires_reconcile''',
                (normalized['kind'],normalized['external_id'],max(sequence,old['sequence'] if old else 0),
                 int(normalized['active'] and not ambiguous),json.dumps(normalized),int(ambiguous)))
            self._reconcile_directory_memberships(db,ctx['tenant'])
            db.execute('INSERT INTO cloud_directory_events VALUES(%s,%s,%s,%s)',
                (key,event_hash,int(ambiguous),time.time()))
            self._audit(db,ctx,'directory_'+normalized['kind'],'held' if ambiguous else 'applied',normalized['external_id'])
        return {'disposition':'reconcile_required' if ambiguous else 'applied','requires_reconcile':ambiguous}

    def scim_resource(self,ctx,resource,external_id):
        """Content-free directory identity metadata for settings administration."""
        from agenthub.enterprise import Denied
        if resource not in ('Users','Groups'):raise ValueError('unsupported_scim_resource')
        with self.open() as current:
            self.current_identity(current.db,ctx);self._need(ctx,'settings');self._directory_admin(current.db,ctx)
            row=current.db.execute('SELECT * FROM cloud_directory_resources WHERE kind=%s AND external_id=%s',
                ('user' if resource=='Users' else 'group',external_id)).fetchone()
            if not row:raise Denied()
            normalized=json.loads(row['payload_json'])
            result={'schemas':['urn:ietf:params:scim:schemas:core:2.0:'+('User' if resource=='Users' else 'Group')],
                'id':external_id,'displayName':normalized['display'],
                'meta':{'resourceType':'User' if resource=='Users' else 'Group','version':str(row['sequence'])}}
            if resource=='Users':result.update(userName=external_id,active=bool(row['active']))
            else:result['members']=[{'value':member} for member in normalized['members']] if row['active'] else []
            return result

    @staticmethod
    def _directory_admin(db,ctx):
        from agenthub.enterprise import Denied
        principal=db.execute('SELECT settings_admin FROM enterprise_principals WHERE tenant=%s AND id=%s',
            (ctx['tenant'],ctx['principal'])).fetchone()
        if not principal or not principal['settings_admin']:raise Denied()

    @staticmethod
    def _reconcile_directory_memberships(db,tenant):
        # Materialize only normalized effective grants into canonical memberships.
        # Explicit grants are preserved separately, so group offboarding does not
        # revoke an unrelated operator grant or leave a directory grant behind.
        db.execute('DELETE FROM cloud_directory_memberships')
        groups = db.execute("SELECT * FROM cloud_directory_resources WHERE kind='group' AND active=1 AND requires_reconcile=0").fetchall()
        for group in groups:
            members = json.loads(group['payload_json'])['members']
            projects = [r['project'] for r in db.execute('SELECT project FROM cloud_directory_group_projects WHERE external_id=%s',(group['external_id'],))]
            for external in members:
                user = db.execute('''SELECT u.principal FROM cloud_directory_users u
                    JOIN enterprise_principals p ON p.id=u.principal AND p.tenant=%s
                    WHERE u.external_id=%s AND p.active=1''',(tenant,external)).fetchone()
                if not user: continue
                for project in projects:
                    db.execute('INSERT INTO cloud_directory_memberships(project,principal) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                        (project,user['principal']))
        db.execute('UPDATE enterprise_memberships SET active=0 WHERE tenant=%s',(tenant,))
        for table in ('cloud_explicit_memberships','cloud_directory_memberships'):
            for row in db.execute('SELECT project,principal FROM '+table):
                db.execute('''INSERT INTO enterprise_memberships(tenant,project,principal,active)
                    VALUES(%s,%s,%s,1) ON CONFLICT(tenant,project,principal) DO UPDATE SET active=1''',
                    (tenant,row['project'],row['principal']))

    def set_membership(self,tenant,project,principal,active):
        if tenant != self.tenant_id: raise ValueError('wrong_tenant')
        with self.delivery_lock(),self.open() as current,current.db:
            if active:
                current.db.execute('INSERT INTO cloud_explicit_memberships(project,principal) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                    (project,principal))
            else:
                current.db.execute('DELETE FROM cloud_explicit_memberships WHERE project=%s AND principal=%s',(project,principal))
            self._reconcile_directory_memberships(current.db,tenant)
