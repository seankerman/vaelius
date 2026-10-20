"""AWS IAM Identity Center sign-in on the client. No server and no real AWS."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError

try:
    import botocore  # noqa: F401
    BOTOCORE = True
except ImportError:
    BOTOCORE = False

SCOPE = {'tenant': 'acme', 'principal': 'alice', 'actor': 'alice', 'enrollment': 'device', 'actions': ['read']}
ACCESS_KEY = 'ASIA' + 'SYNTHETIC' + '0' * 7
SECRET_KEY = 'synthetic-secret-access-key'
SESSION_TOKEN = 'synthetic-session-token'
AWS = {'profile': 'synthetic-sso', 'region': 'us-east-1', 'server_id': 'https://memory.example.org'}


def profile(td, *, access='old-access', expires_in=3600, renew_in=None):
    """A signed-in AWS profile whose credential expires in `expires_in` seconds."""
    from agentclient.credentials import save_metadata
    path = Path(td) / 'credential';path.write_text(access + '\n');path.chmod(0o600)
    backend = {'mode': 'enterprise_local', 'url': 'http://127.0.0.1:1', 'credential_file': str(path),
        'api_version': 'cloud-local-1', 'tenant': 'acme', 'enrollment_id': 'device',
        'credential_renewal': True, 'aws_sts': dict(AWS)}
    state = Path(td) / 'credential.renewal.json'
    save_metadata(state, backend['url'], access, dict(SCOPE, expires_at=time.time() + expires_in))
    if renew_in is not None:
        saved = json.loads(state.read_text());saved['renew_after'] = time.time() + renew_in
        state.write_text(json.dumps(saved))
    return backend, path


class Service:
    """Synthetic exchange route: any signed request yields a new credential."""
    def __init__(self, *, principal='alice', fail=None):
        self.calls = [];self.principal = principal;self.fail = fail;self.issued = 0
    def __call__(self, backend, token, route, data=None):
        self.calls.append((route, token, data))
        if route != '/enterprise/v3/auth/aws-sts' or token is not None:raise AssertionError('exchange is body-only')
        if self.fail:raise self.fail
        self.issued += 1
        return dict(SCOPE, principal=self.principal, actor=self.principal, credential='aws-access-' + str(self.issued),
            expires_at=time.time() + 3600)


def signed_out(*args, **kwargs):
    from agentclient.aws_sts import SignedOut
    raise SignedOut()


def fake_signed(aws, credentials=None):
    return {'region': aws['region'], 'method': 'POST', 'headers': {'synthetic': 'signed'}, 'body': 'synthetic'}


class Renewal(unittest.TestCase):
    def test_current_credential_is_used_without_signing(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            backend, path = profile(td)
            with patch('agentclient.aws_sts.signed_request', side_effect=AssertionError('no signing')):
                self.assertEqual(renew(backend)['status'], 'current')
            self.assertEqual(path.read_text().strip(), 'old-access')

    def test_near_expiry_re_exchanges_transparently_for_transport_and_mcp_headers(self):
        from agentclient.mcp_headers import headers
        from agentclient.transport import enterprise_client
        with tempfile.TemporaryDirectory() as td:
            backend, path = profile(td, expires_in=60, renew_in=-1)
            config = {'projects': {}, 'sessions': {}, 'knowledge_backend': backend}
            (Path(td) / 'config.json').write_text(json.dumps(config))
            service = Service()
            with patch('agentclient.aws_sts.signed_request', side_effect=fake_signed), \
                    patch('agentclient.aws_sts._request', side_effect=service):
                self.assertEqual(enterprise_client(config).token, 'aws-access-1')
                # The new credential is current: the next use signs nothing.
                self.assertEqual(headers(td)['Authorization'], 'Bearer aws-access-1')
            self.assertEqual(path.read_text(), 'aws-access-1');self.assertEqual(len(service.calls), 1)
            route, token, data = service.calls[0]
            self.assertIsNone(token);self.assertEqual(data['enrollment'], 'device')
            self.assertFalse((Path(td) / 'credential.refresh').exists())

    def test_expired_credential_and_signed_out_aws_fails_closed(self):
        from agentclient.aws_sts import SignedOut
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            backend, path = profile(td, expires_in=-1)
            with patch('agentclient.aws_sts.current_credentials', side_effect=signed_out), \
                    patch('agentclient.aws_sts._request') as request:
                with self.assertRaises(SignedOut) as raised:renew(backend)
                request.assert_not_called()
            self.assertEqual(str(raised.exception), 'aws_signed_out')
            self.assertEqual(path.read_text().strip(), 'old-access')

    def test_signed_out_aws_keeps_a_still_valid_credential_until_it_expires(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            backend, _ = profile(td, expires_in=120, renew_in=-1)
            with patch('agentclient.aws_sts.current_credentials', side_effect=signed_out):
                result = renew(backend)
            self.assertEqual((result['status'], result['renewal']), ('current', 'aws_signed_out'))

    def test_service_denial_and_identity_change_never_replace_the_credential(self):
        from agentclient.credentials import renew, LoginRequired
        with tempfile.TemporaryDirectory() as td:
            backend, path = profile(td, expires_in=-1)
            with patch('agentclient.aws_sts.signed_request', side_effect=fake_signed), \
                    patch('agentclient.aws_sts._request', side_effect=Service(fail=HTTPError('x', 404, 'not_found', {}, None))):
                with self.assertRaises(LoginRequired):renew(backend)
            backend, path = profile(td, expires_in=60, renew_in=-1)
            with patch('agentclient.aws_sts.signed_request', side_effect=fake_signed), \
                    patch('agentclient.aws_sts._request', side_effect=Service(principal='bob')):
                with self.assertRaisesRegex(ValueError, 'identity_changed'):renew(backend)
            self.assertEqual(path.read_text().strip(), 'old-access')

    def test_hook_capture_keeps_queuing_while_signed_out(self):
        from agentclient.capture_outbox import Outbox
        from agentclient.hooks import handle
        with tempfile.TemporaryDirectory() as td:
            backend, _ = profile(td, expires_in=-1)
            backend.update(capture_version='enterprise-local-2', capture_owner='hooks', connection_id='synthetic-capture')
            config = {'paused': False, 'projects': {}, 'sessions': {'synthetic': 'maple'}, 'knowledge_backend': backend}
            (Path(td) / 'config.json').write_text(json.dumps(config))
            with patch('agentclient.aws_sts.current_credentials', side_effect=signed_out), \
                    patch('agentclient.aws_sts._request') as request:
                for turn in ('1', '2'):
                    self.assertEqual(handle(td, {'hook_event_name': 'Stop', 'event_id': 'e' + turn,
                        'session_id': 'synthetic', 'turn_id': turn, 'timestamp': '2026-10-01T12:00:00Z',
                        'last_assistant_message': 'Synthetic capture ' + turn}), {})
                request.assert_not_called()
            box = Outbox(td)
            try:
                payloads = ' '.join(r[0] for r in box.db.execute('SELECT payload FROM pending'))
                # Both turns wait in the outbox; the gap diagnostic is queued beside them.
                self.assertIn('Synthetic capture 1', payloads);self.assertIn('Synthetic capture 2', payloads)
                self.assertNotIn('old-access', payloads)
            finally:box.close()

    def test_cli_reports_signed_out_without_a_traceback(self):
        from agentclient.cli import main
        with tempfile.TemporaryDirectory() as td:
            backend, _ = profile(td, expires_in=-1)
            (Path(td) / 'config.json').write_text(json.dumps({'knowledge_backend': backend}))
            with patch('agentclient.aws_sts.current_credentials', side_effect=signed_out):
                with self.assertRaises(SystemExit) as raised:main(['--home', td, 'credential-renew'])
                self.assertEqual(json.loads(raised.exception.code), {'status': 'signed_out', 'reason': 'aws_signed_out'})
                with self.assertRaises(SystemExit) as raised:main(['--home', td, 'status'])
                self.assertEqual(json.loads(raised.exception.code)['status'], 'signed_out')
            output = io.StringIO()
            with redirect_stdout(output):main(['--home', td, 'credential-status'])
            self.assertEqual(json.loads(output.getvalue())['status'], 'signed_out')
            self.assertNotIn('old-access', output.getvalue())

    def test_logout_revokes_the_access_credential(self):
        from agentclient.credentials import logout
        with tempfile.TemporaryDirectory() as td:
            backend, path = profile(td)
            with patch('agentclient.credentials._request', return_value={'revoked': True}) as request:
                self.assertTrue(logout(backend)['revoked'])
            self.assertEqual(request.call_args[0][2:], ('/enterprise/v3/auth/revoke', {'token': 'old-access'}))
            self.assertFalse(path.exists())


class Enrollment(unittest.TestCase):
    def test_command_line_options_are_explicit(self):
        from agentclient.cloud_enroll import main
        common = ['--home', '/synthetic/new', '--url', 'https://memory.example.org', '--tenant', 'acme', '--enrollment', 'device']
        for extra in (['--aws-profile', 'synthetic-sso'], ['--auth', 'aws-sts', '--callback-uri', 'http://127.0.0.1:59001/callback'],
                      ['--auth', 'aws-sts', '--broker', 'legacy'], []):
            with self.subTest(extra=extra), self.assertRaises(SystemExit), \
                    patch('sys.stderr', io.StringIO()):main(common + extra)

    def test_enrollment_stores_only_the_vaelius_credential(self):
        from agentclient.aws_sts import enroll
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / 'profile';service = Service()
            with patch('agentclient.aws_sts.signed_request', side_effect=fake_signed), \
                    patch('agentclient.aws_sts._request', side_effect=service):
                result = enroll(home, url='https://memory.example.org', tenant='acme', enrollment='device',
                    profile='synthetic-sso', region='us-west-2', credential_store='file')
            self.assertEqual((result['principal'], result['refresh_token_stored']), ('alice', False))
            self.assertNotIn('aws-access-1', json.dumps(result))
            config = json.loads((home / 'config.json').read_text());backend = config['knowledge_backend']
            self.assertEqual(backend['aws_sts'], {'profile': 'synthetic-sso', 'region': 'us-west-2',
                'server_id': 'https://memory.example.org'})
            self.assertEqual((backend['transport'], backend['credential_renewal']), ('https', True))
            self.assertEqual((home / 'credential').read_text().strip(), 'aws-access-1')
            self.assertEqual(sorted(p.name for p in home.iterdir()),
                ['config.json', 'credential', 'credential.lock', 'credential.renewal.json'])
            for item in home.iterdir():self.assertEqual(item.stat().st_mode & 0o077, 0)

    def test_signed_out_enrollment_creates_no_credential(self):
        from agentclient.aws_sts import enroll
        from agentclient.cloud_enroll import EnrollmentError
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / 'profile'
            with patch('agentclient.aws_sts.current_credentials', side_effect=signed_out):
                with self.assertRaisesRegex(EnrollmentError, 'aws_signed_out'):
                    enroll(home, url='https://memory.example.org', tenant='acme', enrollment='device',
                        region='us-east-1', credential_store='file')
            self.assertFalse((home / 'credential').exists());self.assertFalse((home / 'config.json').exists())

    def test_region_and_server_id_validation(self):
        from agentclient.aws_sts import aws_settings
        self.assertEqual(aws_settings(url='https://memory.example.org', region='eu-west-1')['server_id'],
            'https://memory.example.org')
        for change in ({'region': 'cn-north-1'}, {'region': 'https://evil.example'}, {'profile': 'bad profile'},
                       {'region': 'us-east-1', 'server_id': 'has space'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                aws_settings(url='https://memory.example.org', **change)


@unittest.skipUnless(BOTOCORE, 'botocore (vaelius-client[aws]) required')
class Signing(unittest.TestCase):
    def test_signature_covers_server_id_and_exact_regional_request(self):
        from botocore.credentials import Credentials
        from agentclient.aws_sts import signed_request
        value = signed_request(AWS, Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN))
        self.assertEqual((value['method'], value['body'], value['region']),
            ('POST', 'Action=GetCallerIdentity&Version=2011-06-15', 'us-east-1'))
        headers = {k.lower(): v for k, v in value['headers'].items()}
        self.assertEqual(headers['host'], 'sts.us-east-1.amazonaws.com')
        self.assertEqual(headers['x-vaelius-server-id'], 'https://memory.example.org')
        signed = headers['authorization'].split('SignedHeaders=')[1].split(',')[0].split(';')
        self.assertTrue({'host', 'x-amz-date', 'x-vaelius-server-id', 'x-vaelius-nonce', 'x-amz-security-token'} <= set(signed))
        self.assertIn('/us-east-1/sts/aws4_request', headers['authorization'])
        self.assertNotIn(SECRET_KEY, json.dumps(value));self.assertNotIn('url', value)
        self.assertNotEqual(signed_request(AWS, Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN))['headers']['Authorization'],
            value['headers']['Authorization'])

    def test_missing_aws_credentials_are_signed_out(self):
        from agentclient.aws_sts import current_credentials, SignedOut
        with tempfile.TemporaryDirectory() as td:
            environment = {'AWS_CONFIG_FILE': str(Path(td) / 'absent-config'),
                'AWS_SHARED_CREDENTIALS_FILE': str(Path(td) / 'absent-credentials'), 'AWS_EC2_METADATA_DISABLED': 'true'}
            with patch.dict(os.environ, environment):
                for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE',
                             'AWS_DEFAULT_PROFILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
                             'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN'):
                    os.environ.pop(name, None)
                with self.assertRaises(SignedOut):current_credentials(None)
                with self.assertRaises(SignedOut):current_credentials('synthetic-missing-profile')


if __name__ == '__main__':
    unittest.main()
