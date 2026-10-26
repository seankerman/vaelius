"""AWS IAM Identity Center sign-in: signed-request checks and exchange.

No real AWS is contacted. A loopback fake STS verifies the SigV4 signature with
synthetic credentials and answers with a configured synthetic identity.
"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
import unittest
from unittest.mock import patch

from test_cloud_postgres import PostgresFixture, SERVICES
from agenthub.enterprise import Denied

try:
    import botocore  # noqa: F401  (signing and the fake STS verifier)
    BOTOCORE = True
except ImportError:
    BOTOCORE = False

ACCOUNT = '111122223333'
OTHER_ACCOUNT = '444455556666'
ROLE = 'AWSReservedSSO_Developer_0123456789abcdef'
RESOURCE = 'https://memory.example.org/mcp'
SERVER_ID = 'https://memory.example.org'
# Built at runtime so no AWS-key-shaped literal appears in the source tree.
ACCESS_KEY = 'ASIA' + 'SYNTHETIC' + '0' * 7
SECRET_KEY = 'synthetic-secret-access-key'
SESSION_TOKEN = 'synthetic-session-token-' + 'x' * 40


def settings(**changes):
    return dict({'tenant': 'acme', 'allowed_account_ids': [ACCOUNT], 'regions': ['us-east-1', 'us-west-2']}, **changes)


def identity(arn, account=ACCOUNT, user_id=None):
    session = arn.rsplit('/', 1)[-1]
    return {'GetCallerIdentityResponse': {'GetCallerIdentityResult': {
        'Arn': arn, 'Account': account, 'UserId': user_id or ('AROA' + 'SYNTHETICROLE00') + ':' + session},
        'ResponseMetadata': {'RequestId': 'synthetic'}}}


def assumed(session='alice', role=ROLE, account=ACCOUNT):
    return 'arn:aws:sts::' + account + ':assumed-role/' + role + '/' + session


def credentials():
    from botocore.credentials import Credentials
    return Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN)


def signed(region='us-east-1', server_id=SERVER_ID, **extra):
    from agentclient.aws_sts import signed_request
    value = signed_request({'region': region, 'server_id': server_id}, credentials())
    return dict(value, enrollment='synthetic-device', **extra)


def resign(payload, headers, *, signed_names=None):
    """Sign arbitrary headers the way an attacker holding credentials could."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    request = AWSRequest(method='POST', url='https://' + headers['Host'] + '/', data=payload['body'], headers=headers)
    signer = SigV4Auth(credentials(), 'sts', payload['region'])
    if signed_names is not None:
        signer.headers_to_sign = lambda req, names=signed_names: _only(req, names)
    signer.add_auth(request)
    return dict(payload, headers=dict(request.headers.items()))


def _only(request, names):
    from botocore.compat import HTTPHeaders
    result = HTTPHeaders()
    for name, value in request.headers.items():
        if name.lower() in names:result[name.lower()] = value
    return result


class FakeSts:
    """Loopback STS: checks the SigV4 signature, records calls, answers as configured."""
    def __init__(self):
        self.calls = [];self.answer = identity(assumed());self.status = 200;self.raw = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
                headers = {k.lower(): v for k, v in self.headers.items()}
                verified = owner.verify(headers, body)
                owner.calls.append({'path': self.path, 'headers': headers, 'body': body, 'verified': verified})
                status = owner.status if verified else 403
                raw = owner.raw if owner.raw is not None else json.dumps(owner.answer).encode()
                if not verified:raw = b'{"Error":{"Code":"SignatureDoesNotMatch"}}'
                self.send_response(status);self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    @staticmethod
    def verify(headers, body):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
        match = re.fullmatch(r'AWS4-HMAC-SHA256 Credential=([A-Z0-9]+)/([0-9]{8})/([a-z0-9-]+)/sts/aws4_request, '
            r'SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})', headers.get('authorization', ''))
        if not match or match.group(1) != ACCESS_KEY:return False
        names = match.group(4).split(';')
        if any(n not in headers for n in names):return False
        request = AWSRequest(method='POST', url='https://' + headers['host'] + '/', data=body,
            headers={n: headers[n] for n in names})
        request.context['timestamp'] = headers['x-amz-date']
        signer = SigV4Auth(Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN), 'sts', match.group(3))
        canonical = signer.canonical_request(request)
        expected = signer.signature(signer.string_to_sign(request, canonical), request)
        return hmac.compare_digest(expected, match.group(5))

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(5)


class Settings(unittest.TestCase):
    def test_defaults_are_strict_and_disabled_unless_configured(self):
        from agenthub.aws_sts import validate_settings
        value = validate_settings(settings(), resource=RESOURCE)
        self.assertEqual(value['server_id'], SERVER_ID)
        self.assertEqual(value['allowed_role_patterns'], ['AWSReservedSSO_*'])
        self.assertTrue(value['require_identity_center']);self.assertEqual(value['token_ttl_seconds'], 3600)
        for change in ({'allowed_account_ids': ['1234']}, {'allowed_account_ids': []},
                       {'regions': ['cn-north-1']}, {'regions': ['https://evil.example']},
                       {'token_ttl_seconds': 86400 * 2}, {'token_ttl_seconds': 10},
                       {'allowed_role_patterns': ['Developer*']}, {'actions': ['read', 'root']},
                       {'sts_endpoint': 'http://127.0.0.1:1/'}, {'tenant': '../acme'},
                       {'max_clock_skew_seconds': 3600}, {'require_identity_center': 'yes'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_settings(settings(**change), resource=RESOURCE)
        self.assertEqual(validate_settings(settings(require_identity_center=False,
            allowed_role_patterns=['ci-reader-*']), resource=RESOURCE)['allowed_role_patterns'], ['ci-reader-*'])

    def test_runtime_profile_cannot_redirect_sts(self):
        from agenthub.aws_sts import AwsStsProvider
        provider = AwsStsProvider(settings(), resource=RESOURCE, http=object())
        self.assertEqual(provider.sts_endpoint('us-west-2'), 'https://sts.us-west-2.amazonaws.com/')
        override = AwsStsProvider(settings(), resource=RESOURCE, http=object(),
            sts_endpoint=lambda region: 'https://evil.example/')
        with self.assertRaises(ValueError):override.sts_endpoint('us-east-1')

    def test_registry_configuration_coexists_and_requires_some_provider(self):
        from agenthub.oauth_provider import OAuthRegistry
        registry = OAuthRegistry('dbname=unused', '/nonexistent', authorization={'resource': RESOURCE, 'aws_sts': settings()})
        self.assertEqual(registry.providers, []);self.assertIsNotNone(registry.aws_sts)
        with self.assertRaises(ValueError):OAuthRegistry('dbname=unused', '/nonexistent', authorization={'resource': RESOURCE})
        with self.assertRaises(ValueError):
            OAuthRegistry('dbname=unused', '/nonexistent', authorization={'resource': RESOURCE,
                'aws_sts': settings(), 'aws_sts_endpoint': 'http://127.0.0.1:1/'})


@unittest.skipUnless(BOTOCORE, 'botocore (vaelius-client[aws]) required for signing')
class SignedRequestChecks(unittest.TestCase):
    def setUp(self):
        from agenthub.aws_sts import validate_settings
        self.settings = validate_settings(settings(), resource=RESOURCE)

    def parse(self, payload, now=None):
        from agenthub.aws_sts import parse_signed_request
        return parse_signed_request(payload, self.settings, now=now or time.time())

    def test_valid_request_forwards_only_signed_headers(self):
        payload = signed();payload['headers']['User-Agent'] = 'unsigned-agent'
        region, headers, digest, expires = self.parse(payload)
        self.assertEqual(region, 'us-east-1')
        self.assertEqual(set(headers), {'authorization', 'content-type', 'host', 'x-amz-date',
            'x-amz-security-token', 'x-vaelius-server-id', 'x-vaelius-nonce', 'accept'})
        self.assertEqual(headers['accept'], 'application/json')
        self.assertNotIn(SECRET_KEY, json.dumps(payload))
        self.assertGreater(expires, time.time() + 900);self.assertEqual(len(digest), 64)

    def test_server_id_missing_mismatched_or_unsigned_is_refused(self):
        with self.assertRaises(Denied):self.parse(signed(server_id='https://other-service.example.org'))
        payload = signed();headers = {k: v for k, v in payload['headers'].items() if k.lower() not in
            ('authorization', 'x-amz-date', 'x-amz-security-token')}
        headers.pop('x-vaelius-server-id')
        with self.assertRaises(ValueError):self.parse(resign(payload, headers))
        unsigned = resign(payload, dict(headers, **{'x-vaelius-server-id': SERVER_ID}),
            signed_names={'host', 'content-type', 'x-amz-date', 'x-amz-security-token'})
        self.assertIn('x-vaelius-server-id', {k.lower() for k in unsigned['headers']})
        with self.assertRaisesRegex(ValueError, 'signed_headers'):self.parse(unsigned)

    def test_body_action_method_and_region_tampering_is_refused(self):
        for change in ({'body': 'Action=GetSessionToken&Version=2011-06-15'},
                       {'body': 'Action=GetCallerIdentity&Version=2011-06-15&Extra=1'},
                       {'method': 'GET'}, {'headers': 'not-a-map'}):
            with self.subTest(change=change), self.assertRaises(ValueError):self.parse(dict(signed(), **change))
        with self.assertRaises(Denied):self.parse(signed(region='eu-west-1'))
        # Claiming an allowed region does not change the region that was signed.
        with self.assertRaisesRegex(ValueError, 'scope'):self.parse(dict(signed(region='us-east-1'), region='us-west-2'))

    def test_extra_headers_skew_and_unsigned_session_token_are_refused(self):
        payload = signed()
        with self.assertRaises(ValueError):
            self.parse(dict(payload, headers=dict(payload['headers'], **{'X-Forwarded-Host': 'evil.example'})))
        with self.assertRaises(ValueError):
            self.parse(dict(payload, headers=dict(payload['headers'], **{'Host': 'sts.amazonaws.com.evil.example'})))
        with self.assertRaises(Denied):self.parse(payload, now=time.time() + 600)
        headers = {k: v for k, v in payload['headers'].items() if k.lower() not in ('authorization', 'x-amz-date')}
        unsigned = resign(payload, headers, signed_names={'host', 'content-type', 'x-amz-date', 'x-vaelius-server-id'})
        with self.assertRaisesRegex(ValueError, 'signed_headers'):self.parse(unsigned)

    def test_client_supplied_url_is_never_used(self):
        from agenthub.aws_sts import AwsStsProvider
        payload = signed(url='http://169.254.169.254/latest/meta-data/')
        provider = AwsStsProvider(settings(), resource=RESOURCE, http=object())
        region, _, _, _ = self.parse(payload)
        self.assertEqual(provider.sts_endpoint(region), 'https://sts.us-east-1.amazonaws.com/')


class IdentityChecks(unittest.TestCase):
    def check(self, arn, account=ACCOUNT, user_id=None, region='us-east-1', **changes):
        from agenthub.aws_sts import parse_identity, validate_settings
        return parse_identity(identity(arn, account, user_id), validate_settings(settings(**changes), resource=RESOURCE), region)[1]

    def test_only_allowlisted_identity_center_sessions_are_accepted(self):
        self.assertIsNone(self.check(assumed()))
        self.assertEqual(self.check('arn:aws:iam::' + ACCOUNT + ':user/alice', user_id='AIDA' + 'SYNTHETICUSER00'), 'principal_type')
        self.assertEqual(self.check('arn:aws:iam::' + ACCOUNT + ':root', user_id=ACCOUNT), 'principal_type')
        self.assertEqual(self.check('arn:aws:sts::' + ACCOUNT + ':federated-user/alice'), 'principal_type')
        self.assertEqual(self.check(assumed(account=OTHER_ACCOUNT), account=OTHER_ACCOUNT), 'account')
        self.assertEqual(self.check(assumed(role='ci-deploy')), 'role')
        self.assertEqual(self.check(assumed(), allowed_role_patterns=['AWSReservedSSO_Admin_*']), 'role')
        self.assertEqual(self.check(assumed(role='ci-deploy'), require_identity_center=False,
            allowed_role_patterns=['AWSReservedSSO_*']), 'role')
        self.assertEqual(self.check(assumed(), region='us-gov-west-1', regions=['us-gov-west-1']), 'inconsistent_identity')
        self.assertEqual(self.check(assumed(), account=OTHER_ACCOUNT), 'inconsistent_identity')
        self.assertEqual(self.check(assumed(), user_id='AROA' + 'SYNTHETICROLE00:mallory'), 'inconsistent_identity')

    def test_malformed_response_is_unavailable_not_identity(self):
        from agenthub.aws_sts import parse_identity, validate_settings, StsUnavailable
        for value in ({}, {'GetCallerIdentityResponse': {}}, {'GetCallerIdentityResponse': {'GetCallerIdentityResult': {'Arn': 7}}}):
            with self.subTest(value=value), self.assertRaises(StsUnavailable):
                parse_identity(value, validate_settings(settings(), resource=RESOURCE), 'us-east-1')


@unittest.skipUnless(SERVICES and BOTOCORE, 'explicit disposable PostgreSQL and botocore required')
class Exchange(PostgresFixture, unittest.TestCase):
    def setUp(self):
        import uuid
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from agenthub.postgres import connect, migrate
        self.postgres_setup();self.addCleanup(self.postgres_teardown)
        self.control_schema = 'test_aws_' + uuid.uuid4().hex[:12]
        admin = self.services['admin_dsn']
        with connect(admin) as db:db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.control_schema)))
        self.control = make_conninfo(admin, options='-c search_path=' + self.control_schema + ',public')
        migrate(self.control, control=True)
        def clean():
            with connect(admin) as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.control_schema)))
        self.addCleanup(clean)
        self.sts = FakeSts();self.addCleanup(self.sts.close)
        self.registry = self.make_registry()
        self.registry.register('acme', self.dsn)
        self.store = self.registry.resolve('acme')
        self.store.identity_bindings.bind('aws-sts:identity-center', 'alice', 'acme', 'alice', allowed_providers=['aws-sts'])
        self.store.identity_bindings.bind('aws-sts:identity-center', 'bob', 'acme', 'bob', allowed_providers=['aws-sts'])

    def make_registry(self, **authorization):
        from agenthub.oauth_provider import OAuthRegistry
        return OAuthRegistry(self.control, Path(self.temp.name) / 'registry',
            authorization=dict({'resource': RESOURCE, 'aws_sts': settings()}, **authorization),
            aws_sts_endpoint=lambda region: self.sts.url)

    def app(self, registry=None):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        return TestClient(create_app(registry or self.registry, allowed_hosts=['testserver']))

    def exchange(self, payload, registry=None):
        with self.app(registry) as app:return app.post('/enterprise/v3/auth/aws-sts', json=payload)

    def audit(self):
        with self.store.open() as state:
            return [dict(r) for r in state.db.execute(
                "SELECT * FROM enterprise_audit WHERE operation='aws_sts_exchange' ORDER BY id")]

    def credential_count(self):
        with self.store.open() as state:
            return state.db.execute('SELECT count(*) n FROM enterprise_credentials').fetchone()['n']

    def test_valid_exchange_issues_short_lived_digest_only_credential(self):
        payload = signed();response = self.exchange(payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        value = response.json();token = value['credential']
        self.assertEqual((value['tenant'], value['principal'], value['enrollment']), ('acme', 'alice', 'synthetic-device'))
        self.assertEqual(value['actions'], ['feedback', 'ingest', 'read', 'share', 'source_read', 'withdraw'])
        self.assertAlmostEqual(value['expires_at'], time.time() + 3600, delta=30)
        self.assertEqual(len(self.sts.calls), 1);call = self.sts.calls[0]
        self.assertTrue(call['verified']);self.assertEqual(call['body'], 'Action=GetCallerIdentity&Version=2011-06-15')
        self.assertEqual(call['headers']['host'], 'sts.us-east-1.amazonaws.com')
        self.assertEqual(call['headers']['accept'], 'application/json')
        with self.app() as app:
            status = app.get('/enterprise/v3/auth/credential', headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(status.status_code, 200);self.assertEqual(status.json()['principal'], 'alice')
            self.assertEqual(app.get('/enterprise/v1/status', headers={'Authorization': 'Bearer ' + token}).status_code, 200)
        from agenthub.enterprise import _digest
        with self.store.open() as state:
            row = dict(state.db.execute('SELECT * FROM enterprise_credentials WHERE digest=?', (_digest(token),)).fetchone())
            dumped = json.dumps([dict(r) for r in state.db.execute('SELECT * FROM enterprise_credentials')], default=str)
        self.assertEqual((row['identity_issuer'], row['identity_subject'], row['identity_provider'], row['oauth_provider']),
            ('aws-sts:identity-center', 'alice', 'aws-sts', None))
        self.assertNotIn(token, dumped)
        audit = self.audit();self.assertEqual([a['disposition'] for a in audit], ['issued'])
        self.assertEqual(audit[0]['actor'], 'alice');self.assertEqual(audit[0]['target_id'], assumed())
        signature = payload['headers']['Authorization'].rsplit('=', 1)[1]
        for secret in (token, SECRET_KEY, SESSION_TOKEN, signature):self.assertNotIn(secret, json.dumps(audit))
        with connect_control(self.control) as db:
            stored = [r['digest'] for r in db.execute('SELECT digest FROM cloud_aws_sts_replay')]
        self.assertEqual(stored, [hashlib.sha256(('aws-sts\0' + signature).encode()).hexdigest()])

    def test_replayed_signed_request_is_refused(self):
        payload = signed()
        self.assertEqual(self.exchange(payload).status_code, 200)
        self.assertEqual(self.exchange(payload).status_code, 404)
        changed = dict(payload, headers=dict(payload['headers'], **{'User-Agent': 'different-unsigned-value'}))
        self.assertEqual(self.exchange(changed).status_code, 404)
        self.assertEqual(self.credential_count(), 3 + 1)
        self.assertEqual(len(self.sts.calls), 1)

    def test_concurrent_replay_after_sts_is_refused_by_the_digest_insert(self):
        payload = signed()
        from agenthub.aws_sts import AwsStsProvider
        original = AwsStsProvider.call_sts
        def raced(provider, region, headers):
            value = original(provider, region, headers)
            if len(self.sts.calls) == 1:  # the first caller lands its digest first
                self.assertEqual(self.exchange(payload).status_code, 200)
            return value
        with patch.object(AwsStsProvider, 'call_sts', raced):
            self.assertEqual(self.exchange(payload).status_code, 404)
        self.assertEqual([a['disposition'] for a in self.audit()], ['issued', 'denied_replay'])

    def test_server_id_and_client_url_cannot_redirect_or_cross_services(self):
        other = FakeSts();self.addCleanup(other.close)
        response = self.exchange(signed(url=other.url))
        self.assertEqual(response.status_code, 200);self.assertEqual(other.calls, []);self.assertEqual(len(self.sts.calls), 1)
        self.assertEqual(self.exchange(signed(server_id='https://other-service.example.org')).status_code, 404)
        self.assertEqual(len(self.sts.calls), 1)

    def test_disallowed_account_role_and_principal_types_are_denied(self):
        cases = [(assumed(account=OTHER_ACCOUNT), OTHER_ACCOUNT, None),
                 (assumed(role='ci-deploy'), ACCOUNT, 'denied_role'),
                 ('arn:aws:iam::' + ACCOUNT + ':user/alice', ACCOUNT, 'denied_principal_type'),
                 ('arn:aws:iam::' + ACCOUNT + ':root', ACCOUNT, 'denied_principal_type'),
                 ('arn:aws:sts::' + ACCOUNT + ':federated-user/alice', ACCOUNT, 'denied_principal_type')]
        for arn, account, disposition in cases:
            with self.subTest(arn=arn):
                before = len(self.audit());self.sts.answer = identity(arn, account)
                self.assertEqual(self.exchange(signed()).status_code, 404)
                audit = self.audit()[before:]
                # Foreign accounts leave no rows; allowlisted-account denials are recorded.
                self.assertEqual([a['disposition'] for a in audit], [disposition] if disposition else [])
        self.assertEqual(self.credential_count(), 3)
        self.assertEqual(self.exchange(signed(region='eu-west-1')).status_code, 404)

    def test_sts_failures_deny_without_issuing(self):
        for status, raw, expected in ((500, None, 503), (200, b'<xml>not json</xml>', 503),
                                      (200, b'{"GetCallerIdentityResponse":{}}', 503), (302, None, 503)):
            with self.subTest(status=status, raw=raw):
                self.sts.status, self.sts.raw = status, raw
                self.assertEqual(self.exchange(signed()).status_code, expected)
        self.sts.status, self.sts.raw = 200, None
        tampered = signed();tampered['headers']['X-Amz-Security-Token'] = SESSION_TOKEN + 'tampered'
        self.assertEqual(self.exchange(tampered).status_code, 404)
        self.assertFalse(self.sts.calls[-1]['verified'])
        self.assertEqual(self.credential_count(), 3)

    def test_unbound_subject_and_inactive_principal_or_binding_are_denied(self):
        self.sts.answer = identity(assumed(session='mallory'))
        self.assertEqual(self.exchange(signed()).status_code, 404)
        self.sts.answer = identity(assumed())
        token = self.exchange(signed()).json()['credential']
        self.store.identity_bindings.bind('aws-sts:identity-center', 'alice', 'acme', 'alice',
            allowed_providers=['aws-sts'], active=False)
        with self.assertRaises(Denied):self.registry.authenticate_token(token)
        self.assertEqual(self.exchange(signed()).status_code, 404)
        self.store.identity_bindings.bind('aws-sts:identity-center', 'alice', 'acme', 'alice', allowed_providers=['aws-sts'])
        self.registry.authenticate_token(token)
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE enterprise_principals SET active=0 WHERE tenant='acme' AND id='alice'")
        with self.assertRaises(Denied):self.registry.authenticate_token(token)
        self.assertEqual(self.exchange(signed()).status_code, 404)
        # A binding that only allows another provider does not accept AWS sign-in.
        self.store.identity_bindings.bind('aws-sts:identity-center', 'bob', 'acme', 'bob', allowed_providers=['native'])
        self.sts.answer = identity(assumed(session='bob'))
        self.assertEqual(self.exchange(signed()).status_code, 404)
        self.assertEqual([a['disposition'] for a in self.audit()],
            ['denied_unbound', 'issued', 'denied_unbound', 'denied_principal', 'denied_unbound'])

    def test_enrollment_of_another_principal_cannot_be_taken_over(self):
        self.assertEqual(self.exchange(signed()).status_code, 200)
        self.sts.answer = identity(assumed(session='bob'))
        self.assertEqual(self.exchange(signed()).status_code, 409)
        self.assertEqual(self.exchange(dict(signed(), enrollment='bob-device')).status_code, 200)
        self.assertEqual([a['disposition'] for a in self.audit()], ['issued', 'denied_enrollment', 'issued'])

    def test_route_absent_unless_configured_and_oauth_legacy_paths_coexist(self):
        from agenthub.oauth_provider import OAuthRegistry
        from agenthub.postgres import TenantRegistry
        plain = TenantRegistry(self.control, Path(self.temp.name) / 'plain');plain.register('acme', self.dsn)
        self.assertEqual(self.exchange(signed(), plain).status_code, 404);self.assertEqual(self.sts.calls, [])
        secret = Path(self.temp.name) / 'introspection.secret';secret.write_text('synthetic-secret\n');secret.chmod(0o600)
        both = self.make_registry(providers=[{'issuer': 'https://identity.example.org/realms/company', 'tenant': 'acme',
            'client_ids': ['vaelius-plugin'], 'introspection_client_id': 'vaelius-api', 'client_secret_file': str(secret)}])
        self.store.identity_bindings.bind('https://identity.example.org/realms/company', 'oauth-subject-1', 'acme', 'bob')
        aws = self.exchange(signed(), both).json()['credential']
        oauth = 'synthetic-provider-access-token'
        claims = {'active': True, 'sub': 'oauth-subject-1', 'client_id': 'vaelius-plugin', 'exp': time.time() + 300}
        with patch.object(both.providers[0], 'introspect', return_value=(claims, {'read'})) as introspect:
            _, oauth_ctx = both.authenticate_token(oauth)
            _, aws_ctx = both.authenticate_token(aws)
        self.assertEqual((oauth_ctx['principal'], oauth_ctx['actions']), ('bob', {'read'}))
        self.assertEqual(aws_ctx['principal'], 'alice');self.assertEqual(introspect.call_count, 1)
        both.bind_credential(self.tokens['admin'], 'acme')
        self.assertEqual(both.authenticate_token(self.tokens['admin'])[1]['principal'], 'admin')
        with self.app(both) as app:
            self.assertEqual(app.post('/enterprise/v3/auth/begin', json={'tenant': 'acme', 'broker': 'x'}).status_code, 404)

    def test_installed_client_signs_in_renews_transparently_and_fails_closed(self):
        import socket
        import uvicorn
        from agentclient.aws_sts import enroll, status
        from agentclient.capture_outbox import Outbox
        from agentclient.credentials import access_token, LoginRequired
        from agentclient.hooks import handle
        from agentclient.mcp_headers import headers
        from agentclient.transport import enterprise_client
        from agenthub.cloud_api import create_app
        sock = socket.socket();sock.bind(('127.0.0.1', 0));sock.listen(32);port = sock.getsockname()[1]
        url = 'http://127.0.0.1:' + str(port)
        registry = self.make_registry(resource=url + '/mcp');registry.register('acme', self.dsn)
        server = uvicorn.Server(uvicorn.Config(create_app(registry), log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True);thread.start()
        def stop():server.should_exit = True;thread.join(10)
        self.addCleanup(stop)
        for _ in range(200):
            if server.started:break
            time.sleep(.02)
        environment = {'AWS_ACCESS_KEY_ID': ACCESS_KEY, 'AWS_SECRET_ACCESS_KEY': SECRET_KEY,
            'AWS_SESSION_TOKEN': SESSION_TOKEN, 'AWS_EC2_METADATA_DISABLED': 'true',
            'AWS_CONFIG_FILE': str(Path(self.temp.name) / 'absent-config'),
            'AWS_SHARED_CREDENTIALS_FILE': str(Path(self.temp.name) / 'absent-credentials')}
        home = Path(self.temp.name) / 'client'
        with patch.dict(os.environ, environment, clear=False):
            for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
                         'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN'):
                os.environ.pop(name, None)
            result = enroll(home, url=url, tenant='acme', enrollment='synthetic-device', region='us-east-1',
                credential_store='file')
            self.assertEqual(result['principal'], 'alice')
            config = json.loads((home / 'config.json').read_text());backend = config['knowledge_backend']
            self.assertEqual(backend['aws_sts'], {'profile': None, 'region': 'us-east-1', 'server_id': url})
            self.assertNotIn(SECRET_KEY, (home / 'config.json').read_text())
            self.assertNotIn(SESSION_TOKEN, ''.join(p.read_text() for p in home.iterdir() if p.is_file()))
            first = access_token(backend);self.assertNotIn(first, json.dumps(result))
            self.assertEqual(enterprise_client(config).request('/enterprise/v3/auth/credential')['principal'], 'alice')
            store, ctx = registry.authenticate_token(first)
            store.enroll_connection(ctx, 'aws-capture', 'synthetic', 'maple', ['agent'])
            # The Vaelius credential expires; the next use signs again transparently.
            from agenthub.enterprise import _digest
            with self.store.open() as state, state.db:
                state.db.execute('UPDATE enterprise_credentials SET expires_at=? WHERE digest=?', (time.time() - 1, _digest(first)))
            renewal = home / 'credential.renewal.json';saved = json.loads(renewal.read_text())
            saved.update(expires_at=time.time() - 1, renew_after=time.time() - 1);renewal.write_text(json.dumps(saved))
            with self.assertRaises(Denied):registry.authenticate_token(first)
            mcp = headers(home)
            second = mcp['Authorization'][7:]
            self.assertNotEqual(second, first);self.assertEqual(registry.authenticate_token(second)[1]['principal'], 'alice')
            self.assertEqual(len(self.sts.calls), 2)
            # Signed out of AWS with an expired credential: fail closed, keep queuing.
            for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'):os.environ.pop(name)
            with self.store.open() as state, state.db:
                state.db.execute('UPDATE enterprise_credentials SET expires_at=? WHERE digest=?', (time.time() - 1, _digest(second)))
            saved = json.loads(renewal.read_text())
            saved.update(expires_at=time.time() - 1, renew_after=time.time() - 1);renewal.write_text(json.dumps(saved))
            with self.assertRaisesRegex(LoginRequired, 'aws_signed_out'):enterprise_client(config)
            self.assertEqual(status(backend)['status'], 'signed_out')
            config['sessions'] = {'synthetic': 'maple'};backend.update(connection_id='aws-capture', capture_owner='hooks')
            (home / 'config.json').write_text(json.dumps(config))
            started = time.monotonic()
            self.assertEqual(handle(home, {'hook_event_name': 'Stop', 'event_id': 'aws-source', 'session_id': 'synthetic',
                'turn_id': '1', 'timestamp': '2026-10-01T12:00:00Z',
                'last_assistant_message': 'Maple keeps the CSV header for reports.'}), {})
            self.assertLess(time.monotonic() - started, 5)
            box = Outbox(home)
            try:self.assertEqual(box.status()['pending_events'], 1)
            finally:box.close()
            self.assertEqual(len(self.sts.calls), 2)
            # Signing in to AWS again resumes capture without any Vaelius login.
            os.environ.update(AWS_ACCESS_KEY_ID=ACCESS_KEY, AWS_SECRET_ACCESS_KEY=SECRET_KEY, AWS_SESSION_TOKEN=SESSION_TOKEN)
            box = Outbox(home)
            try:
                box.drain(enterprise_client(config, timeout=5), max_events=10, max_seconds=10)
                self.assertEqual(box.status()['pending_events'], 0)
            finally:box.close()
            self.assertEqual(status(backend)['status'], 'signed_in')


def connect_control(dsn):
    from agenthub.postgres import connect
    return connect(dsn)


if __name__ == '__main__':
    unittest.main()
