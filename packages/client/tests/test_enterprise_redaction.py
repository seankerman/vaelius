"""Frozen synthetic security cases; no live credentials or private source data."""
import json
from tempfile import TemporaryDirectory
import unittest

from agentclient.capture_outbox import Outbox
from agentclient.cleaning import clean_private
from agentclient.enterprise_capture import normalize_capture, redact
from agentclient.general_contract import canonical, digest, split_event


class EnterpriseRedactionTests(unittest.TestCase):
    def test_recognizable_credentials_in_text(self):
        cases = [
            ('password="two word fixture"', 'two word fixture'),
            ('{"password": "tiny"}', 'tiny'),
            ('PASSWORD=x', '=x'),
            ('Authorization: Basic ZmFrZTpmYWtl', 'ZmFrZTpmYWtl'),
            ('Cookie: session=fixture-session; theme=dark', 'fixture-session'),
            ('refresh_token=fixture-refresh', 'fixture-refresh'),
            ('postgresql://alice:fixture-password@localhost/db', 'fixture-password'),
            ('https://example.invalid/?X-Amz-Signature=fixture-signature&format=json', 'fixture-signature'),
            ('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.fixtureSignature', 'eyJhbGci'),  # gitleaks:allow -- deliberately fake redaction input
            ('-----BEGIN PRIVATE KEY-----\nfixture private bytes\n-----END PRIVATE KEY-----', 'fixture private bytes'),
            ('Bearer fixture-bearer-token', 'fixture-bearer-token'),
        ]
        for value, secret in cases:
            with self.subTest(value=value):
                safe, reasons = clean_private(value)
                self.assertNotIn(secret, safe)
                self.assertIn('possible_secret', reasons)
                self.assertEqual(clean_private(safe)[0], safe)

    def test_nested_fields_and_serialized_payloads(self):
        value = {'tool': {'headers': {'Authorization': 'opaque', 'X-Api-Key': 'x'},
            'env': {'AWS_SECRET_ACCESS_KEY': 'fixture secret', 'DB_PASSWORD': 'a b'},
            'args': [{'clientSecret': 'small'}, {'session_token': 12345}],
            'output': '{"password":"fixture-serialized"}'}}
        safe, manifest = redact(value)
        rendered = json.dumps(safe)
        for secret in ['opaque', 'fixture secret', 'a b', 'small', '12345', 'fixture-serialized']:
            self.assertNotIn(secret, rendered)
        self.assertEqual(safe['tool']['headers']['X-Api-Key'], '[REDACTED_SECRET]')
        self.assertEqual(redact(safe)[0], safe)
        self.assertTrue(manifest)
        self.assertNotIn('fixture secret', json.dumps(manifest))

    def test_useful_facts_and_references_survive(self):
        value = {'email': 'alice@example.invalid', 'path': '/Users/alice/data/étude.csv',
            'token_count': 42, 'password_policy': 'at least twelve characters',
            'secret_name': 'team/database', 'sha256': 'a' * 64,
            'attachment': {'reference': 'source-object:fixture', 'media_type': 'image/png'},
            'encoded_media': 'data:image/png;base64,aW1hZ2UtZml4dHVyZQ==',
            'text': 'Basic setup: we fixed the password parser; API key rotation is documented.'}
        self.assertEqual(redact(value), (value, []))

    def test_redaction_before_hash_parts_and_outbox(self):
        event = normalize_capture({'hook_event_name': 'PostToolUse', 'event_id': 'fixture',
            'session_id': 'session', 'tool_input': {'password': 'fixture-private'},
            'tool_response': {'text': '前 café /Users/alice/data/étude.csv'},
            'timestamp': 123}, 'project', 'connection')
        self.assertEqual(event['disposition'], 'redacted')
        self.assertNotIn('fixture-private', canonical(event).decode())
        self.assertEqual(digest(event), split_event(event)[0]['digest'])
        with TemporaryDirectory() as folder:
            outbox = Outbox(folder)
            try:
                outbox.put(event)
                self.assertNotIn('fixture-private', outbox.db.execute('SELECT payload FROM pending').fetchone()[0])
                changed = dict(event, blocks=[{'type': 'text', 'value': '{"password":"fixture-private"}'}])
                with self.assertRaisesRegex(ValueError, '^unredacted_outbox_input$'):
                    outbox.put(changed)
            finally:
                outbox.close()
        body = event['blocks'][1]['value']['text']
        start = body.index('/Users')
        self.assertEqual(body[start:], '/Users/alice/data/étude.csv')

    def test_secret_dictionary_keys_do_not_leak_to_manifest(self):
        value = {'https://alice:fixture-password@example.invalid/path': 'value'}
        safe, manifest = redact(value)
        self.assertNotIn('fixture-password', json.dumps([safe, manifest]))


if __name__ == '__main__': unittest.main()
