"""AWS IAM Identity Center sign-in from the user's existing AWS session.

The client signs one sts:GetCallerIdentity request (SigV4) with the current
credentials of a named AWS profile and sends that signed request, never the AWS
secret key, to the Vaelius exchange route. The signature covers a header naming
this Vaelius service, so the request is useless to any other service. Only the
short-lived Vaelius access credential is stored; renewal signs again. When the
AWS session has expired the profile reports `aws_signed_out` and fails closed;
hooks keep queuing capture in the outbox until `aws sso login` restores it.

Signing uses botocore, installed by the optional extra `vaelius-client[aws]`.
Signed requests and tokens are never logged or printed.
"""
import fcntl
import json
import os
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from agentclient.credentials import (LoginRequired, open_store, _sidecar, _read, _binding,
    _request, _scope, save_metadata, _token)

SERVER_ID_HEADER = 'x-vaelius-server-id'
NONCE_HEADER = 'x-vaelius-nonce'
BODY = 'Action=GetCallerIdentity&Version=2011-06-15'
ROUTE = '/enterprise/v3/auth/aws-sts'
_REGION = re.compile(r'(?!cn-)[a-z]{2}(?:-gov)?-[a-z]+-[0-9]{1,2}')
_PROFILE = re.compile(r'[A-Za-z0-9_.@+=,-]{1,128}')
_IDENTITY = ('tenant', 'principal', 'actor', 'enrollment')


class SignedOut(LoginRequired):
    """The AWS profile has no current credentials; `aws sso login` restores them."""
    def __init__(self):
        ValueError.__init__(self, 'aws_signed_out')


def aws_settings(*, url, profile=None, region=None, server_id=None):
    """Explicit, secret-free settings stored in the client profile."""
    if profile is not None and not _PROFILE.fullmatch(profile):raise ValueError('invalid_aws_profile')
    if region is None:region = _profile_region(profile)
    if not isinstance(region, str) or not _REGION.fullmatch(region):raise ValueError('aws_region_required')
    # Default to the service URL itself: a request signed for this service names
    # the origin the client connects to, which another service cannot claim.
    parsed = urlsplit(url)
    server_id = server_id or parsed.scheme + '://' + parsed.netloc
    if not re.fullmatch(r'[\x21-\x7e]{1,256}', server_id):raise ValueError('invalid_aws_server_id')
    return {'profile': profile, 'region': region, 'server_id': server_id}


def _botocore():
    try:
        import botocore.session
        return botocore
    except ImportError:
        raise ValueError('aws_sign_in_requires_extra: pip install vaelius-client[aws]') from None


def _profile_region(profile):
    try:return _botocore().session.Session(profile=profile).get_config_variable('region')
    except ValueError:raise
    except Exception:return None


def current_credentials(profile):
    """Frozen credentials from the standard chain, or SignedOut. Never stored."""
    botocore = _botocore()
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        found = botocore.session.Session(profile=profile).get_credentials()
        frozen = found.get_frozen_credentials() if found is not None else None
    except (BotoCoreError, ClientError):
        # Missing profile, expired SSO token and refused role credentials alike.
        raise SignedOut() from None
    if frozen is None or not frozen.access_key or not frozen.secret_key:raise SignedOut()
    return frozen


def signed_request(aws, credentials=None):
    """One SigV4-signed GetCallerIdentity request for the configured region."""
    _botocore()
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    credentials = credentials or current_credentials(aws.get('profile'))
    host = 'sts.' + aws['region'] + '.amazonaws.com'
    request = AWSRequest(method='POST', url='https://' + host + '/', data=BODY, headers={
        'Host': host, 'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
        SERVER_ID_HEADER: aws['server_id'], NONCE_HEADER: secrets.token_urlsafe(16)})
    SigV4Auth(credentials, 'sts', aws['region']).add_auth(request)
    return {'region': aws['region'], 'method': 'POST', 'headers': dict(request.headers.items()), 'body': BODY}


def exchange(backend, *, credentials=None):
    """Return (Vaelius access token, credential metadata). Raises LoginRequired when refused."""
    payload = dict(signed_request(backend['aws_sts'], credentials), enrollment=backend['enrollment_id'])
    try:value = _request(backend, None, ROUTE, payload)
    except HTTPError as error:
        if error.code in (401, 403, 404, 409):raise LoginRequired() from None
        raise
    token = _token(value['credential'])
    metadata = {k: value[k] for k in (*_IDENTITY, 'actions', 'expires_at')}
    if metadata['tenant'] != backend['tenant'] or metadata['enrollment'] != backend['enrollment_id']:
        raise ValueError('credential_identity_changed')
    return token, metadata


def renew(backend, *, force=False, credentials=None):
    """Re-exchange when the Vaelius credential nears expiry; nothing to rotate or replay."""
    store = open_store(backend);state = _sidecar(store, '.renewal.json')
    with os.fdopen(os.open(_sidecar(store, '.lock'), os.O_RDWR | os.O_CREAT, 0o600), 'r+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        access = store.read('access')
        saved = json.loads(_read(state)) if access is not None and state.exists() else {}
        if access is not None and saved.get('binding') != _binding(backend['url'], access.strip()):saved = {}
        if not force and saved.get('renew_after', 0) > time.time():
            return {'status': 'current', 'expires_at': saved['expires_at']}
        try:token, metadata = exchange(backend, credentials=credentials)
        except (SignedOut, URLError, TimeoutError, ConnectionError) as error:
            # The current credential stays usable until its own expiry.
            if saved.get('expires_at', 0) > time.time() + 5:
                return {'status': 'current', 'expires_at': saved['expires_at'],
                        'renewal': 'aws_signed_out' if isinstance(error, SignedOut) else 'deferred'}
            raise
        except HTTPError as error:
            if error.code >= 500 and saved.get('expires_at', 0) > time.time() + 5:
                return {'status': 'current', 'expires_at': saved['expires_at'], 'renewal': 'deferred'}
            raise
        expected = _scope(saved)
        # A different AWS identity can never continue someone else's profile.
        if expected and any(metadata[k] != expected[k] for k in _IDENTITY):
            raise ValueError('credential_identity_changed')
        store.write('access', token)
        save_metadata(state, backend['url'], token, metadata)
        return {'status': 'renewed', 'expires_at': metadata['expires_at']}


def status(backend):
    """Content-free sign-in status for the CLI; never contacts AWS or the service."""
    store = open_store(backend);state = _sidecar(store, '.renewal.json')
    access = store.read('access')
    saved = json.loads(_read(state)) if access is not None and state.exists() else {}
    current = bool(saved) and saved.get('binding') == _binding(backend['url'], access.strip()) and saved['expires_at'] > time.time()
    return {'status': 'signed_in' if current else 'signed_out', 'auth': 'aws-sts',
            'aws_profile': backend['aws_sts'].get('profile'), 'expires_at': saved.get('expires_at') if current else None}


def enroll(home, *, url, tenant, enrollment, profile=None, region=None, server_id=None,
           credential_store='auto', credentials=None):
    """Create a new profile signed in with the current AWS session; no browser."""
    from agentclient.cloud_enroll import prepare_home, _credential_store, EnrollmentError
    for identifier in (tenant, enrollment):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', identifier):
            raise EnrollmentError('explicit_enrollment_identifiers_required')
    aws = aws_settings(url=url, profile=profile, region=region, server_id=server_id)
    store_kind = _credential_store(credential_store)
    profile_path = prepare_home(home)
    backend = {'mode': 'enterprise_local', 'url': url, 'credential_file': str(profile_path / 'credential'),
        'credential_store': store_kind, 'transport': 'https' if urlsplit(url).scheme == 'https' else 'loopback',
        'capture_version': 'enterprise-local-2', 'capture_owner': 'transcript', 'api_version': 'cloud-local-1',
        'tenant': tenant, 'enrollment_id': enrollment, 'credential_renewal': True, 'aws_sts': aws}
    try:token, metadata = exchange(backend, credentials=credentials)
    except SignedOut:raise EnrollmentError('aws_signed_out') from None
    except LoginRequired:raise EnrollmentError('aws_sign_in_denied') from None
    config = {'paused': False, 'projects': {}, 'sessions': {}, 'desktop_transcripts': {},
        'capture_all_sessions': False, 'observer': {'enabled': False}, 'publication': {'enabled': False},
        'remote_publication': False, 'knowledge_backend': backend}
    try:
        fd = os.open(profile_path / 'credential.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600);os.close(fd)
        store = open_store(backend)
        store.create('access', token)
        save_metadata(_sidecar(store, '.renewal.json'), url, token, metadata)
        fd = os.open(profile_path / 'config.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(config, indent=2) + '\n');stream.flush();os.fsync(stream.fileno())
    except (OSError, ValueError):raise EnrollmentError('private_profile_write_failed') from None
    return {'profile': str(profile_path), 'credential_stored': True, 'credential_store': store_kind,
            'refresh_token_stored': False, 'auth': 'aws-sts', 'principal': metadata['principal'],
            'expires_at': metadata['expires_at'], 'projects_enrolled': 0, 'client_model_calls': 0,
            'local_corpus_opened': False, 'host_hooks_installed': False}
