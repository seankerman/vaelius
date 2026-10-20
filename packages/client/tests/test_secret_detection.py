"""Shared credential detector: synthetic positives, negatives, cost and placement.

Synthetic credentials are assembled at run time from obviously fake fragments so
this file itself never contains a complete token that a repository scanner (or a
push-protection service) would treat as a live credential.
"""
import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from agentclient import cleaning
from agentclient.capture_outbox import Outbox
from agentclient.enterprise_capture import normalize_capture, redact
from agentclient.general_contract import canonical, split_event
from agentclient.secret_detection import (REDACTED_SECRET, RULE_IDS, RULE_SOURCES, contains_secret,
    findings, redact_text)


def join(*parts):
    return ''.join(parts)


def jwt():
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return join(encode({'alg': 'HS256', 'typ': 'JWT'}), '.', encode({'sub': 'synthetic-fixture'}), '.', 'Zm9vYmFyZml4dHVyZQ')


# One synthetic value per rule: (rule id, text containing it, the secret part).
AWS_KEY = join('AKIA', 'IOSFODNN7EXAMPLE')
AWS_SECRET = join('wJalrXUtnFEMI/K7MDENG/', 'bPxRfiCYEXAMPLEKEY')
POSITIVES = [
    ('aws-access-token', join('export AWS_ACCESS_KEY_ID=', AWS_KEY), AWS_KEY),
    ('aws-access-token', join('temporary ', 'ASIA', 'SYNTHETICEXAMPL1 issued'), join('ASIA', 'SYNTHETICEXAMPL1')),
    ('credential-assignment', join('aws_secret_access_key = ', AWS_SECRET), AWS_SECRET),
    ('high-entropy-assignment', join('"awsSecretAccessKey": "', AWS_SECRET, '"'), AWS_SECRET),
    ('github-token', join('remote token gh', 'p_', 'Fake0Example1Token2Value3Abc4Def5Ghi6'), join('gh', 'p_', 'Fake0Example1Token2Value3Abc4Def5Ghi6')),
    ('github-token', join('oauth gh', 'o_', 'Fake0Example1Token2Value3Abc4Def5Ghi6'), join('gh', 'o_', 'Fake0Example1Token2Value3Abc4Def5Ghi6')),
    ('github-token', join('app gh', 's_', 'Fake0Example1Token2Value3Abc4Def5Ghi6'), join('gh', 's_', 'Fake0Example1Token2Value3Abc4Def5Ghi6')),
    ('github-token', join('pat github', '_pat_', '11FAKE0EXAMPLE_', 'a' * 20, '1'), join('github', '_pat_', '11FAKE0EXAMPLE_', 'a' * 20, '1')),
    ('slack-token', join('bot xo', 'xb-', '000000000000-000000000000-FakeExampleToken0'), join('xo', 'xb-', '000000000000-000000000000-FakeExampleToken0')),
    ('slack-token', join('user xo', 'xp-', '000000000000-0000000000-FakeExample'), join('xo', 'xp-', '000000000000-0000000000-FakeExample')),
    ('slack-token', join('app xa', 'pp-1-', 'A0FAKE0EXAMPLE-0000-abcdef'), join('xa', 'pp-1-', 'A0FAKE0EXAMPLE-0000-abcdef')),
    ('gcp-api-key', join('key=AI', 'za', 'SyFake0Example1Value2Abc3Def4Ghi5Jk'), join('AI', 'za', 'SyFake0Example1Value2Abc3Def4Ghi5Jk')),
    ('stripe-access-token', join('charge with sk', '_live_', 'FakeExample0000000000000000'), join('sk', '_live_', 'FakeExample0000000000000000')),
    ('stripe-access-token', join('restricted rk', '_live_', 'FakeExample0000000000000000'), join('rk', '_live_', 'FakeExample0000000000000000')),
    ('sk-api-key', join('OPENAI_KEY is sk', '-proj-', 'FakeExample0123456789abcdef'), join('sk', '-proj-', 'FakeExample0123456789abcdef')),
    ('sk-api-key', join('ANTHROPIC sk', '-ant-api03-', 'FakeExample0123456789-abcdef'), join('sk', '-ant-api03-', 'FakeExample0123456789-abcdef')),
    ('jwt', join('id ', jwt(), ' end'), jwt()),
    ('private-key', join('-----BEGIN ', 'RSA PRIVATE KEY-----\nMIIFakeExample0\nAAAA\n-----END ', 'RSA PRIVATE KEY-----'), 'MIIFakeExample0'),
    ('private-key', join('-----BEGIN ', 'OPENSSH PRIVATE KEY-----\nb3BlbnNzaFakeExample\ntruncated output'), 'b3BlbnNzaFakeExample'),
    ('url-credentials', 'postgresql://app_user:FakeExample-pass@db.example.invalid:5432/app', 'FakeExample-pass'),
    ('url-credentials', 'mongodb+srv://svc:p@ss@word@cluster.example.invalid/db', 'p@ss@word'),
    ('credential-assignment', 'Server=db.example.invalid;Database=app;User Id=svc;Password=FakeExample9;', 'FakeExample9'),
    ('authorization-header', 'Authorization: Basic ZmFrZTpleGFtcGxl', 'ZmFrZTpleGFtcGxl'),
    ('bearer-token', join('curl -H "Authorization: Bear', 'er FakeExampleBearer0"'), 'FakeExampleBearer0'),
    ('credential-assignment', 'api_key: fake-example-value', 'fake-example-value'),
    ('credential-assignment', 'DB_PASSWORD="two word fixture"', 'two word fixture'),
    ('credential-assignment', 'client_secret=FakeExampleSecret', 'FakeExampleSecret'),
    ('credential-assignment', 'session_token: FakeExampleSession', 'FakeExampleSession'),
    ('high-entropy-assignment', join('webhookSigningKey = "', 'Fk9xQ2mZr7Lw3Tb8Vn1Yc6Hd"'), 'Fk9xQ2mZr7Lw3Tb8Vn1Yc6Hd'),
]

# Code and prose that must survive untouched.
NEGATIVES = [
    'Basic setup: we fixed the password parser; API key rotation is documented.',
    'token = get_token(request)',
    'password = os.environ["DB_PASSWORD"]',
    'api_key=${API_KEY}',
    'secret: {{ secret_name }}',
    'password=None',
    'PWD=/Users/example/projects/vaelius\nOLDPWD=/home/example',
    'cache_key = "' + 'ab12' * 16 + '"',
    join('commit_key: ', '4b825dc642cb6eb9a060e54bf8d69288fbee4904'),
    join('idempotency_key = "', '123e4567-e89b-12d3-a456-426614174000"'),
    'publicKey: "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAx"',
    'sort_key: created_at_and_identifier_2024',
    'auth_module = vaelius.cloud.identity.provider',
    'monkey patching the token_count = 4096 limit',
    'Use scikit-learn or sk-learn-compatible-estimators for this.',
    'The ask-question-tool returned AKIA as a word in prose.',
    'Visit https://docs.example.invalid/guide and email alice@example.invalid.',
    'sha256: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',
    'data:image/png;base64,aW1hZ2UtZml4dHVyZQ==',
    'eyJ is the base64 prefix of a JSON object, not a token.',
    'git@github.example.invalid:org/repo.git and https://user@example.invalid/x',
]


class SecretDetectionTests(unittest.TestCase):
    def test_each_rule_detects_its_synthetic_format(self):
        covered = set()
        for rule, text, secret in POSITIVES:
            with self.subTest(rule=rule, text=text[:24]):
                safe, rules = redact_text(text)
                self.assertIn(rule, rules)
                self.assertNotIn(secret, safe)
                self.assertIn(REDACTED_SECRET, safe)
                # Idempotent: stored values are fixed points of the detector.
                self.assertEqual(redact_text(safe), (safe, []))
                covered.add(rule)
        self.assertEqual(covered, set(RULE_IDS))

    def test_code_and_prose_negatives_are_untouched(self):
        for text in NEGATIVES:
            with self.subTest(text=text[:40]):
                self.assertEqual(redact_text(text), (text, []))
                self.assertFalse(contains_secret(text))

    def test_names_and_structure_are_preserved_around_redaction(self):
        self.assertEqual(redact_text('{"password": "tiny"}')[0], '{"password": "' + REDACTED_SECRET + '"}')
        self.assertEqual(redact_text('postgresql://u:FakeExample1@db.example.invalid/app')[0],
            'postgresql://' + REDACTED_SECRET + '@db.example.invalid/app')
        self.assertEqual(redact_text('Authorization: Bearer FakeExampleBearer0')[0], 'Authorization: ' + REDACTED_SECRET)

    def test_findings_and_manifest_never_carry_values(self):
        text = ' '.join(item[1] for item in POSITIVES)
        rendered = json.dumps([vars(item) for item in findings(text)])
        _, manifest = redact({'blocks': [text], 'env': {'DB_PASSWORD': 'FakeExampleField'}})
        for _, _, secret in POSITIVES:
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, json.dumps(manifest))
        self.assertNotIn('FakeExampleField', json.dumps(manifest))
        self.assertTrue(all(set(entry) == {'field', 'reason'} for entry in manifest))
        self.assertTrue(any(entry['reason'].startswith('credential_pattern:') for entry in manifest))

    def test_gitleaks_adapted_rules_are_attributed(self):
        self.assertTrue({'aws-access-token', 'github-token', 'slack-token', 'gcp-api-key',
            'stripe-access-token', 'jwt', 'private-key'} <= {k for k, v in RULE_SOURCES.items() if v == 'gitleaks'})
        import agentclient.secret_detection as module
        self.assertIn('Zachary Rice', module.__doc__)
        self.assertIn('MIT License', module.__doc__)

    def test_legacy_cleaning_interface_uses_shared_detector(self):
        text = join('gh', 'p_', 'Fake0Example1Token2Value3Abc4Def5Ghi6')
        self.assertIsNotNone(cleaning.SECRET.search(text))
        self.assertEqual(cleaning.SECRET.sub(REDACTED_SECRET, text), REDACTED_SECRET)
        self.assertEqual(cleaning.clean_private(text), (REDACTED_SECRET, ['possible_secret']))
        self.assertIsNone(cleaning.SECRET.search('no credential here'))

    def test_adversarial_inputs_stay_linear(self):
        """No catastrophic backtracking: 1 MB adversarial inputs finish quickly."""
        size = 1_000_000
        inputs = {
            'repeated_key_headers': (join('-----BEGIN ', 'PRIVATE KEY-----\n') * (size // 28)),
            'assignment_prefixes': ('password=' * (size // 9)),
            'entropy_names': ('secretKey= ' * (size // 11)),
            'long_identifier': ('a' * size),
            'jwt_prefixes': ('eyJabcdefghij.' * (size // 14)),
            'url_userinfo': ('https://u:' + 'p' * 2000 + ' ') * (size // 2011),
            'bearer_runs': ('Bearer ' * (size // 7)),
            'quoted_unterminated': ('token="' + 'x' * 5000) * (size // 5007),
        }
        for name, text in inputs.items():
            with self.subTest(name=name):
                start = time.perf_counter(); redact_text(text)
                elapsed = time.perf_counter() - start
                # Catastrophic patterns take minutes here; the bound is generous for CI.
                self.assertLess(elapsed, 10.0, name)

    def test_large_realistic_payload_cost(self):
        line = 'INFO request id=42 path=/v1/items took 12ms user=alice@example.invalid\n'
        text = line * (4_000_000 // len(line)) + 'api_key=FakeExampleValue\n'
        start = time.perf_counter(); safe, rules = redact_text(text)
        self.assertLess(time.perf_counter() - start, 10.0)
        self.assertEqual(rules, ['credential-assignment'])
        self.assertNotIn('FakeExampleValue', safe)


class CapturePersistenceTests(unittest.TestCase):
    """New formats never reach the client outbox, the wire digest or gap logs."""
    def event(self, secret):
        return normalize_capture({'hook_event_name': 'PostToolUse', 'event_id': 'fixture',
            'session_id': 'session', 'tool_input': {'command': 'deploy --key ' + secret},
            'tool_response': {'stdout': 'configured ' + secret}, 'timestamp': 123}, 'project', 'connection')

    def test_secrets_never_reach_outbox_or_transport(self):
        for rule, text, secret in POSITIVES:
            with self.subTest(rule=rule, secret=secret[:6]):
                event = self.event(text)
                self.assertEqual(event['disposition'], 'redacted')
                wire = b''.join(base64.b64decode(part['data']) for part in split_event(event))
                self.assertNotIn(secret, canonical(event).decode())
                self.assertNotIn(secret.encode(), wire)
                with TemporaryDirectory() as folder:
                    outbox = Outbox(folder)
                    try:
                        outbox.put(event)
                    finally:
                        outbox.close()
                    for path in Path(folder).iterdir():
                        self.assertNotIn(secret.encode(), path.read_bytes(), path.name)
                        self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)

    def test_outbox_rejects_unredacted_input(self):
        event = self.event('safe')
        event['blocks'][0]['value'] = {'command': join('use sk', '_live_', 'FakeExample0000000000000000')}
        with TemporaryDirectory() as folder:
            outbox = Outbox(folder)
            try:
                with self.assertRaisesRegex(ValueError, '^unredacted_outbox_input$'):
                    outbox.put(event)
                self.assertEqual(outbox.status()['pending_events'], 0)
            finally:
                outbox.close()

    def test_gap_log_is_metadata_only(self):
        from agentclient.hooks import _enterprise_gap
        secret = join('gh', 'p_', 'Fake0Example1Token2Value3Abc4Def5Ghi6')
        with TemporaryDirectory() as folder:
            _enterprise_gap(folder, {'session': 'session', 'kind': 'PostToolUse', 'project': 'p',
                'turn': 't', 'prompt': secret, 'body': secret}, 'structured_capture_backpressure_or_invalid')
            raw = (Path(folder) / 'enterprise-capture-gaps.jsonl').read_bytes()
            self.assertNotIn(secret.encode(), raw)
            self.assertEqual(set(json.loads(raw)), {'at', 'session_hash', 'kind', 'reason', 'project', 'session', 'turn'})
            self.assertEqual(os.stat(Path(folder) / 'enterprise-capture-gaps.jsonl').st_mode & 0o077, 0)


if __name__ == '__main__':
    unittest.main()
