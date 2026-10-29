"""AWS IAM Identity Center sign-in from a signed sts:GetCallerIdentity request.

This follows the Vault AWS-auth and aws-iam-authenticator pattern. The client
signs one GetCallerIdentity request with its current AWS credentials and sends
the signed request, never the AWS secret key, to this service. The service
forwards it unchanged to a regional STS endpoint chosen from the operator's
region allowlist, so STS itself verifies the signature and names the caller.

The request carries this service's identifier in a signed header, so a request
signed for another service cannot be replayed here. An accepted signature is
recorded until STS would reject it as expired and is refused on reuse.

The verified role session name is only an external subject. An operator binding
(issuer, subject, tenant) -> principal is still required, and principal, tenant
and binding state are rechecked by the canonical credential path on every use.
No refresh token exists: renewal signs a new request with current credentials.
"""
import fnmatch
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from agenthub.cloud_identity import IdentityError
from agenthub.enterprise import Denied

SERVER_ID_HEADER = 'x-vaelius-server-id'
# A signed random value makes each request unique, so two sign-ins in the same
# second with the same credentials never collide in the replay record.
NONCE_HEADER = 'x-vaelius-nonce'
BODY = 'Action=GetCallerIdentity&Version=2011-06-15'
IDENTITY_PROVIDER = 'aws-sts'
IDENTITY_CENTER_ROLE_PREFIX = 'AWSReservedSSO_'
ACTIONS = {'ingest', 'read', 'source_read', 'feedback', 'curate', 'correct', 'withdraw', 'share', 'manage', 'moderate',
           'policy', 'audit', 'settings'}
DEFAULTS = {'issuer': 'aws-sts:identity-center', 'allowed_role_patterns': [IDENTITY_CENTER_ROLE_PREFIX + '*'],
    'require_identity_center': True, 'regions': ['us-east-1'], 'token_ttl_seconds': 3600,
    'max_clock_skew_seconds': 300, 'actions': ['feedback', 'ingest', 'manage', 'read', 'share', 'source_read', 'withdraw']}
# STS refuses a SigV4 signature more than 15 minutes from its timestamp. A
# recorded signature is kept at least that long, plus the permitted skew.
SIGNATURE_LIFETIME = 900

# Only headers a GetCallerIdentity signature needs. Anything else, including
# hop-by-hop or proxy headers, is refused rather than forwarded to STS.
_HEADERS = {'authorization', 'host', 'content-type', 'x-amz-date', 'x-amz-security-token',
    'x-amz-content-sha256', SERVER_ID_HEADER, NONCE_HEADER, 'accept', 'user-agent', 'content-length'}
_REGION = re.compile(r'(?!cn-)[a-z]{2}(?:-gov)?-[a-z]+-[0-9]{1,2}')
_ACCOUNT = re.compile(r'[0-9]{12}')
_AMZ_DATE = re.compile(r'[0-9]{8}T[0-9]{6}Z')
_AUTHORIZATION = re.compile(r'AWS4-HMAC-SHA256 Credential=([A-Z0-9]{16,128})/([0-9]{8})/([a-z0-9-]{1,32})/sts/aws4_request,'
    r' ?SignedHeaders=([a-z0-9;-]{1,512}), ?Signature=([0-9a-f]{64})')
_NAME = r'[A-Za-z0-9_+=,.@-]'
_PATTERN = re.compile(r'[A-Za-z0-9_+=,.@*?-]{1,64}')
# Only an assumed-role session is accepted. IAM users, the account root and
# federated users have other ARN shapes and never match.
_ASSUMED_ROLE = re.compile(r'arn:(aws|aws-us-gov):sts::([0-9]{12}):assumed-role/(' + _NAME + r'{1,64})/(' + _NAME + r'{2,64})')
_USER_ID = re.compile(r'[A-Z0-9]{16,128}:(' + _NAME + r'{2,64})')


class StsUnavailable(Exception):
    """STS could not be asked; the caller is neither verified nor denied."""


def _partition(region):
    return 'aws-us-gov' if '-gov-' in region else 'aws'


def sts_host(region):
    return 'sts.' + region + '.amazonaws.com'


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme + '://' + parsed.netloc


def validate_settings(settings, *, resource):
    """Complete, strictly validated settings; unknown keys are configuration errors."""
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS) - {'tenant', 'server_id', 'allowed_account_ids'}:
        raise ValueError('aws_sts_configuration')
    value = dict(DEFAULTS, server_id=_origin(resource), **settings)
    patterns, regions, accounts, actions = (value[k] for k in
        ('allowed_role_patterns', 'regions', 'allowed_account_ids', 'actions'))
    if (not isinstance(value.get('tenant'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['tenant'])
            or not isinstance(value['issuer'], str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', value['issuer'])
            or not isinstance(value['server_id'], str) or not re.fullmatch(r'[\x21-\x7e]{1,256}', value['server_id'])
            or not isinstance(accounts, list) or not 1 <= len(accounts) <= 100
            or any(not isinstance(a, str) or not _ACCOUNT.fullmatch(a) for a in accounts)
            or not isinstance(patterns, list) or not 1 <= len(patterns) <= 32
            or any(not isinstance(p, str) or not _PATTERN.fullmatch(p) for p in patterns)
            or not isinstance(regions, list) or not 1 <= len(regions) <= 32
            or any(not isinstance(r, str) or not _REGION.fullmatch(r) for r in regions)
            or type(value['require_identity_center']) is not bool
            or type(value['token_ttl_seconds']) is not int or not 60 <= value['token_ttl_seconds'] <= 43200
            or type(value['max_clock_skew_seconds']) is not int or not 30 <= value['max_clock_skew_seconds'] <= 900
            or not isinstance(actions, list) or not actions
            or any(not isinstance(a, str) or a not in ACTIONS for a in actions)):
        raise ValueError('aws_sts_configuration')
    # A pattern that can never match an Identity Center role is a silent lockout.
    if value['require_identity_center'] and not all(
            p.startswith(IDENTITY_CENTER_ROLE_PREFIX) or p == '*' for p in patterns):
        raise ValueError('aws_sts_role_patterns_require_identity_center')
    return value


def _header_map(raw):
    if not isinstance(raw, dict) or not 1 <= len(raw) <= len(_HEADERS):
        raise ValueError('aws_sts_headers')
    headers = {};size = 0
    for name, value in raw.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', name)
                or not isinstance(value, str) or not 1 <= len(value) <= 8192
                or any(ord(c) < 0x20 or ord(c) > 0x7e for c in value)):
            raise ValueError('aws_sts_headers')
        key = name.lower();size += len(key) + len(value)
        if key in headers or key not in _HEADERS:raise ValueError('aws_sts_headers')
        headers[key] = value
    if size > 16384:raise ValueError('aws_sts_headers')
    return headers


def parse_signed_request(data, settings, *, now):
    """Return (region, forwarded headers, signature digest, signature expiry).

    Every check here runs before anything is sent to STS. The URL is never read
    from the request: the region selects a fixed endpoint from the allowlist.
    """
    if not isinstance(data, dict) or set(data) - {'region', 'method', 'headers', 'body', 'enrollment', 'url'}:
        raise ValueError('aws_sts_request')
    # `url` is accepted for Vault-style clients and deliberately ignored.
    region = data.get('region')
    if not isinstance(region, str) or region not in settings['regions']:raise Denied()
    if data.get('method') != 'POST' or data.get('body') != BODY:raise ValueError('aws_sts_request')
    headers = _header_map(data.get('headers'))
    match = _AUTHORIZATION.fullmatch(headers.get('authorization', ''))
    if not match:raise ValueError('aws_sts_authorization')
    _, scope_date, scope_region, signed, signature = match.groups()
    names = signed.split(';')
    if (names != sorted(set(names)) or any(n not in headers or n == 'authorization' for n in names)
            or not {'host', 'x-amz-date', SERVER_ID_HEADER} <= set(names)
            or ('x-amz-security-token' in headers and 'x-amz-security-token' not in names)):
        raise ValueError('aws_sts_signed_headers')
    if scope_region != region or headers['host'] != sts_host(region):raise ValueError('aws_sts_scope')
    # Constant-time comparison; a request signed for another service is refused.
    if not hmac.compare_digest(headers[SERVER_ID_HEADER].encode(), settings['server_id'].encode()):raise Denied()
    stamp = headers['x-amz-date']
    if not _AMZ_DATE.fullmatch(stamp) or stamp[:8] != scope_date:raise ValueError('aws_sts_date')
    signed_at = datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
    if abs(now - signed_at) > settings['max_clock_skew_seconds']:raise Denied()
    if 'content-type' in names and headers['content-type'].replace(' ', '').lower() not in (
            'application/x-www-form-urlencoded', 'application/x-www-form-urlencoded;charset=utf-8'):
        raise ValueError('aws_sts_content_type')
    if 'accept' in names and headers['accept'] != 'application/json':raise ValueError('aws_sts_accept')
    if NONCE_HEADER in headers and not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', headers[NONCE_HEADER]):
        raise ValueError('aws_sts_nonce')
    if 'content-length' in names and headers['content-length'] != str(len(BODY)):raise ValueError('aws_sts_length')
    if ('x-amz-content-sha256' in names and headers['x-amz-content-sha256']
            != hashlib.sha256(BODY.encode()).hexdigest()):raise ValueError('aws_sts_payload')
    # Forward exactly the signed headers. Unsigned ones are dropped, not trusted.
    forwarded = {n: headers[n] for n in names}
    forwarded['authorization'] = headers['authorization']
    forwarded.setdefault('accept', 'application/json')
    digest = hashlib.sha256(('aws-sts\0' + signature).encode()).hexdigest()
    expires = max(now, signed_at) + SIGNATURE_LIFETIME + settings['max_clock_skew_seconds']
    return region, forwarded, digest, expires


def parse_identity(value, settings, region):
    """Accept only an allowlisted account's assumed-role session. Returns a dict or raises Denied."""
    try:
        result = value['GetCallerIdentityResponse']['GetCallerIdentityResult']
        arn, account, user_id = result['Arn'], result['Account'], result['UserId']
    except (KeyError, TypeError):
        raise StsUnavailable('sts_response') from None
    if not all(isinstance(v, str) and len(v) <= 2048 for v in (arn, account, user_id)):
        raise StsUnavailable('sts_response')
    identity = {'arn': arn, 'account': account if _ACCOUNT.fullmatch(account) else None}
    match = _ASSUMED_ROLE.fullmatch(arn)
    if not match:return identity, 'principal_type'
    partition, arn_account, role, session = match.groups()
    identity.update(role=role, session=session)
    user = _USER_ID.fullmatch(user_id)
    if partition != _partition(region) or arn_account != account or not user or user.group(1) != session:
        return identity, 'inconsistent_identity'
    if account not in settings['allowed_account_ids']:return identity, 'account'
    if ((settings['require_identity_center'] and not role.startswith(IDENTITY_CENTER_ROLE_PREFIX))
            or not any(fnmatch.fnmatchcase(role, p) for p in settings['allowed_role_patterns'])):
        return identity, 'role'
    return identity, None


class AwsStsProvider:
    def __init__(self, settings, *, resource, http=None, sts_endpoint=None, clock=time.time):
        self.settings = validate_settings(settings, resource=resource)
        self.tenant = self.settings['tenant'];self.issuer = self.settings['issuer']
        if sts_endpoint is None:
            sts_endpoint = lambda region: 'https://' + sts_host(region) + '/'
        else:
            # Test-only constructor argument; runtime.json has no way to set it.
            # Even here only a loopback fake is accepted, never another host.
            original = sts_endpoint
            def sts_endpoint(region):
                url = original(region);parsed = urlsplit(url)
                if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1'):
                    raise ValueError('sts_endpoint_override_requires_loopback')
                return url
        self.sts_endpoint = sts_endpoint
        if http is None:
            import requests
            http = requests.Session();http.trust_env = False
        self.http = http;self.clock = clock

    def call_sts(self, region, headers):
        try:
            response = self.http.post(self.sts_endpoint(region), data=BODY.encode(), headers=headers,
                timeout=5, allow_redirects=False)
            content = response.content
        except Exception:
            raise StsUnavailable('sts_unreachable') from None
        # STS reports an invalid or expired signature with 403. That is a denial.
        if response.status_code in (400, 403):raise Denied()
        if response.status_code != 200 or len(content) > 65536:raise StsUnavailable('sts_status')
        try:return response.json()
        except ValueError:raise StsUnavailable('sts_response') from None

    def exchange(self, registry, data, request_id):
        from agenthub.postgres import connect
        now = self.clock()
        region, headers, digest, expires = parse_signed_request(data, self.settings, now=now)
        enrollment = data.get('enrollment')
        if not isinstance(enrollment, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', enrollment):
            raise ValueError('invalid_enrollment')
        with connect(registry.control_dsn) as db:
            if db.execute('SELECT 1 FROM cloud_aws_sts_replay WHERE digest=%s', (digest,)).fetchone():raise Denied()
        identity, refusal = parse_identity(self.call_sts(region, headers), self.settings, region)
        # Callers outside the allowlisted accounts leave no rows: anyone with any
        # AWS account can produce them, so recording them would be a write lever.
        if identity['account'] not in self.settings['allowed_account_ids']:raise Denied()
        store = registry.resolve(self.tenant)
        audit = {'tenant': self.tenant, 'actor': 'unauthenticated', 'request_id': request_id}
        def deny(reason):
            with store.open() as state, state.db:
                store._audit(state.db, audit, 'aws_sts_exchange', 'denied_' + reason, identity['arn'])
            raise Denied()
        if refusal:deny(refusal)
        external = {'issuer': self.issuer, 'subject': identity['session'], 'provider': IDENTITY_PROVIDER}
        try:binding = store.identity_bindings.resolve(external, self.tenant)
        except IdentityError:deny('unbound')
        with connect(registry.control_dsn) as db:
            db.execute('DELETE FROM cloud_aws_sts_replay WHERE expires_at<=%s', (now,))
            fresh = db.execute('INSERT INTO cloud_aws_sts_replay(digest,expires_at) VALUES(%s,%s) ON CONFLICT(digest) DO NOTHING',
                (digest, expires)).rowcount
        if not fresh:deny('replay')
        from agenthub.enterprise import Conflict
        try:
            token = store.enroll(self.tenant, binding['principal'], enrollment, sorted(self.settings['actions']),
                expires_in=self.settings['token_ttl_seconds'], external_identity=external)
        except Conflict:
            with store.open() as state, state.db:
                store._audit(state.db, dict(audit, actor=binding['principal']), 'aws_sts_exchange', 'denied_enrollment', identity['arn'])
            raise
        except Denied:deny('principal')
        ctx = store.authenticate(token, request_id)
        with store.open() as state, state.db:
            store._audit(state.db, ctx, 'aws_sts_exchange', 'issued', identity['arn'])
        status = store.credential_status(ctx)
        return {'credential': token, **status}
