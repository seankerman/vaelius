"""Raw captured content protection: server re-redaction, least-privilege originals,
object-store hardening and the operator scanner. Synthetic values only.

Credentials are assembled at run time from obviously fake fragments so this file
never contains a complete token that a repository scanner would report.
"""
from contextlib import contextmanager
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agentclient.general_contract import split_event
from agentclient.secret_detection import REDACTED_SECRET
from agenthub.enterprise import Denied
from test_cloud_postgres import PostgresFixture, SERVICES


def join(*parts):
    return ''.join(parts)


SECRETS = {
    'github': join('gh', 'p_', 'Fake0Example1Token2Value3Abc4Def5Ghi6'),
    'aws': join('AKIA', 'IOSFODNN7EXAMPLE'),
    'slack': join('xo', 'xb-', '000000000000-000000000000-FakeExampleToken0'),
    'stripe': join('sk', '_live_', 'FakeExample0000000000000000'),
    'password': 'FakeExamplePassword9',
}
TEXT = ('Deploy notes for the Orchard dataset at /data/drafts/orchard.csv. token ' + SECRETS['github'] +
        ' key ' + SECRETS['aws'] + ' bot ' + SECRETS['slack'] + ' charge ' + SECRETS['stripe'] +
        ' and DB_PASSWORD=' + SECRETS['password'])


def bypassed_event(event_id='bypassed', text=TEXT, revision='1'):
    """A client that skipped redaction (old detector or modified client)."""
    event = normalize_capture({'hook_event_name': 'UserPromptSubmit', 'event_id': event_id,
        'session_id': 'orchard-history', 'turn_id': event_id, 'prompt': 'safe placeholder',
        'timestamp': '2026-09-01T10:00:00Z', 'revision': revision}, 'maple', 'protected-agent')
    event['blocks'] = [{'type': 'text', 'value': text},
                       {'type': 'tool_arguments', 'value': {'env': 'STRIPE=' + SECRETS['stripe']}}]
    return event


class IntakeRedactionUnitTests(unittest.TestCase):
    def test_manifest_records_fields_and_rules_never_values(self):
        from agenthub.intake_redaction import redact_capture
        safe, added = redact_capture(bypassed_event())
        rendered = json.dumps(safe)
        for secret in SECRETS.values():
            self.assertNotIn(secret, rendered)
        self.assertEqual(safe['disposition'], 'redacted')
        self.assertTrue(added and all(item['reason'].startswith('server_') for item in added))
        self.assertTrue(all(set(item) == {'field', 'reason'} for item in safe['redactions']))
        self.assertEqual(redact_capture(safe), (safe, []))

    def test_credentials_in_identity_fields_are_rejected_not_rewritten(self):
        from agenthub.intake_redaction import redact_capture
        event = bypassed_event()
        event['external_id'] = 'id-' + SECRETS['github']
        with self.assertRaisesRegex(ValueError, '^credential_in_source_identity$'):
            redact_capture(event)


    def test_scan_cli_rejects_apply_without_rewrite(self):
        from agenthub.secret_scan import main
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(SystemExit):
            main(['--profile', folder, '--tenant', 'acme', '--apply'])


class LeastPrivilegeDefaultsTests(unittest.TestCase):
    def test_default_enrollment_omits_source_read(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        captured = {}

        class Registry:
            def complete_login(self, tenant, broker_name, broker, **values):
                captured.update(values)
                return {'access_token': 'synthetic-access', 'expires_at': 1, 'refresh_token': None,
                        'refresh_expires_at': None, 'session_expires_at': None}

        client = TestClient(create_app(Registry(), allowed_hosts=['testserver'], brokers={'fixture': object()}))
        response = client.post('/enterprise/v3/auth/complete', json={'tenant': 'orchard', 'broker': 'fixture',
            'state': 's' * 32, 'code': 'synthetic', 'callback_uri': 'http://127.0.0.1:1/cb', 'enrollment': 'laptop'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('read', captured['actions'])
        self.assertNotIn('source_read', captured['actions'])
        # An operator's explicit enrollment action list is still honored.
        client = TestClient(create_app(Registry(), allowed_hosts=['testserver'], brokers={'fixture': object()},
            enrollment_actions=['read', 'source_read']))
        client.post('/enterprise/v3/auth/complete', json={'tenant': 'orchard', 'broker': 'fixture',
            'state': 's' * 32, 'code': 'synthetic', 'callback_uri': 'http://127.0.0.1:1/cb', 'enrollment': 'laptop'})
        self.assertEqual(captured['actions'], ['read', 'source_read'])

    def test_default_oauth_scope_mapping_omits_source_read(self):
        from agenthub.oauth_provider import OAuthProvider

        class Response:
            status_code = 200; content = b'{}'
            def __init__(self, value): self.value = value
            def json(self): return self.value

        class Http:
            def get(self, url, **kwargs):
                return Response({'issuer': 'https://identity.example.invalid/realms/x',
                    'code_challenge_methods_supported': ['S256'],
                    **{name: 'https://identity.example.invalid/realms/x/' + name for name in (
                        'authorization_endpoint', 'token_endpoint', 'revocation_endpoint', 'introspection_endpoint')}})
            def post(self, url, **kwargs):
                return Response({'active': True, 'aud': ['https://memory.example.invalid/mcp'], 'client_id': 'plugin',
                    'sub': 'synthetic-subject', 'exp': 4102444800,
                    'scope': 'openid vaelius:read vaelius:source_read vaelius:ingest'})

        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / 'introspection.secret'
            secret.write_text('synthetic-introspection-secret\n'); secret.chmod(0o600)
            settings = {'issuer': 'https://identity.example.invalid/realms/x', 'tenant': 'orchard',
                'client_ids': ['plugin'], 'introspection_client_id': 'api', 'client_secret_file': str(secret)}
            provider = OAuthProvider(settings, http=Http())
            self.assertNotIn('source_read', provider.actions)
            _, actions = provider.introspect('synthetic-token', 'https://memory.example.invalid/mcp')
            self.assertEqual(actions, {'read', 'ingest'})
            explicit = OAuthProvider(dict(settings, actions=['read', 'source_read']), http=Http())
            self.assertEqual(explicit.introspect('synthetic-token', 'https://memory.example.invalid/mcp')[1],
                {'read', 'source_read'})


class ObjectStorageHardeningTests(unittest.TestCase):
    def test_local_originals_are_owner_only(self):
        from agenthub.source_objects import FileSourceObjects, object_key
        import hashlib
        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'objects'
                root.mkdir(mode=0o755)
                objects = FileSourceObjects(root)
                body = b'synthetic original'
                key = object_key('tenant', 'source', '1', hashlib.sha256(body).hexdigest())
                objects.put(key, io.BytesIO(body))
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                files = list(root.iterdir())
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        finally:
            os.umask(previous)

    def test_s3_endpoints_require_tls_except_loopback_emulator(self):
        from agenthub.source_objects import S3SourceObjects
        with self.assertRaisesRegex(ValueError, '^tls_endpoint_required$'):
            S3SourceObjects('http://objects.example.invalid:9000', 'bucket', 'fixture', 'fixture',
                local_hosts=('objects.example.invalid',))
        S3SourceObjects('https://objects.example.invalid:9000', 'bucket', 'fixture', 'fixture',
            local_hosts=('objects.example.invalid',))
        S3SourceObjects('http://127.0.0.1:9000', 'bucket', 'fixture', 'fixture')
        environment = {'AWS_ENDPOINT_URL_S3': 'http://objects.example.invalid:9000',
            'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture'}
        with patch.dict(os.environ, environment):
            with self.assertRaisesRegex(ValueError, '^tls_endpoint_required$'):
                S3SourceObjects(None, 'bucket', None, None, workload_identity=True)

    def test_kms_configuration_is_validated_and_plumbed(self):
        from agenthub.object_config import objects_from_settings
        with patch('agenthub.source_objects.S3SourceObjects') as adapter:
            objects_from_settings({'objects': {'kind': 's3_aws', 'bucket': 'synthetic', 'region': 'us-east-1',
                'kms_key_id': 'arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab'}})
            self.assertEqual(adapter.call_args.kwargs['kms_key_id'],
                'arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab')
        from agenthub.source_objects import S3SourceObjects
        with self.assertRaisesRegex(ValueError, '^invalid_kms_key_id$'):
            S3SourceObjects('http://127.0.0.1:9000', 'bucket', 'fixture', 'fixture', kms_key_id='bad key')

    @contextmanager
    def emulator(self):
        try:
            from moto.server import ThreadedMotoServer
        except ImportError:
            raise unittest.SkipTest('optional moto server fixture not installed')
        server = ThreadedMotoServer(ip_address='127.0.0.1', port=0, verbose=False)
        server.start()
        try:
            host, port = server.get_host_and_port()
            yield f'http://{host}:{port}'
        finally:
            server.stop()

    def put(self, adapter, body):
        import hashlib
        from agenthub.source_objects import object_key
        key = object_key('tenant', 'source', '1', hashlib.sha256(body).hexdigest())
        return key, adapter.put(key, io.BytesIO(body))

    def test_sse_kms_encrypts_with_configured_key_and_fails_closed(self):
        import boto3
        from agenthub.source_objects import ObjectEncryptionUnavailable, S3SourceObjects
        with self.emulator() as endpoint:
            kms = boto3.client('kms', endpoint_url=endpoint, region_name='us-east-1',
                aws_access_key_id='fixture', aws_secret_access_key='fixture')
            key_id = kms.create_key(Description='synthetic')['KeyMetadata']['KeyId']
            adapter = S3SourceObjects(endpoint, 'protected', 'fixture', 'fixture', kms_key_id=key_id)
            adapter.client.create_bucket(Bucket='protected')
            self.assertEqual(adapter.verify_encryption()['encryption'], 'aws:kms')
            key, _ = self.put(adapter, b'synthetic encrypted original')
            head = adapter.client.head_object(Bucket='protected', Key=key)
            self.assertEqual(head['ServerSideEncryption'], 'aws:kms')
            self.assertTrue(head['SSEKMSKeyId'].endswith(key_id))
            # Unknown and disabled keys fail closed before any object is written.
            missing = S3SourceObjects(endpoint, 'protected', 'fixture', 'fixture',
                kms_key_id='00000000-0000-0000-0000-000000000000')
            with self.assertRaisesRegex(ObjectEncryptionUnavailable, 'kms_key_unavailable'):
                self.put(missing, b'never written one')
            kms.disable_key(KeyId=key_id)
            disabled = S3SourceObjects(endpoint, 'protected', 'fixture', 'fixture', kms_key_id=key_id)
            with self.assertRaisesRegex(ObjectEncryptionUnavailable, 'kms_key_unavailable'):
                self.put(disabled, b'never written two')
            listed = adapter.client.list_objects_v2(Bucket='protected').get('Contents', [])
            self.assertEqual([item['Key'] for item in listed], [key])

    def test_unconfirmed_encryption_removes_the_new_object(self):
        import boto3
        from agenthub.source_objects import ObjectEncryptionUnavailable, S3SourceObjects
        with self.emulator() as endpoint:
            kms = boto3.client('kms', endpoint_url=endpoint, region_name='us-east-1',
                aws_access_key_id='fixture', aws_secret_access_key='fixture')
            key_id = kms.create_key(Description='synthetic')['KeyMetadata']['KeyId']
            adapter = S3SourceObjects(endpoint, 'unconfirmed', 'fixture', 'fixture', kms_key_id=key_id)
            adapter.client.create_bucket(Bucket='unconfirmed')
            original = adapter.client.put_object

            def plaintext_store(**arguments):
                # A store that silently ignores SSE parameters.
                for name in ('ServerSideEncryption', 'SSEKMSKeyId', 'BucketKeyEnabled'):
                    arguments.pop(name, None)
                return original(**arguments)
            with patch.object(adapter.client, 'put_object', side_effect=plaintext_store):
                with self.assertRaisesRegex(ObjectEncryptionUnavailable, 'object_encryption_unverified'):
                    self.put(adapter, b'synthetic unconfirmed original')
            self.assertEqual(adapter.client.list_objects_v2(Bucket='unconfirmed').get('KeyCount', 0), 0)

    def test_object_bodies_are_never_logged(self):
        from agenthub.source_objects import S3SourceObjects
        marker = join('synthetic-body-', 'marker-', 'FakeExample0')
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Collect(level=logging.DEBUG); root = logging.getLogger()
        previous = root.level; root.addHandler(handler); root.setLevel(logging.DEBUG)
        try:
            with self.emulator() as endpoint:
                adapter = S3SourceObjects(endpoint, 'logging', 'fixture', 'fixture')
                adapter.client.create_bucket(Bucket='logging')
                key, _ = self.put(adapter, marker.encode())
                with adapter.open(key) as stream:
                    self.assertEqual(stream.read(), marker.encode())
        finally:
            root.removeHandler(handler); root.setLevel(previous)
        self.assertTrue(records)
        self.assertFalse([message for message in records if marker in message])


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class RawContentPostgresTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        from agenthub.cloud_runtime import CloudStore
        from agenthub.conversation_segments import ConversationSegments
        from agenthub.source_objects import FileSourceObjects
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')
        self.store.retrieval_corpus = 'sources'
        self.objects_root = Path(self.temp.name) / 'objects'
        self.objects = FileSourceObjects(self.objects_root)
        self.store.conversation_segments = ConversationSegments(self.store, self.objects)
        self.store.enroll_connection(self.ctx, 'protected-agent', 'codex', 'maple', ['agent'], visibility='team')
        self.store.create_principal('acme', 'carol')

    def tearDown(self):
        self.postgres_teardown()

    def credential(self, principal, actions):
        return self.store.authenticate(self.store.enroll('acme', principal, principal + '-' + '-'.join(actions), actions))

    def leaks(self, secrets=SECRETS.values()):
        """Every table in this schema, plus retained objects, as text and hex."""
        from psycopg import sql
        from agenthub.postgres import connect
        found = []
        with connect(self.admin_dsn) as db:
            tables = [row[0] for row in db.execute('''SELECT table_name FROM information_schema.tables
                WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name''', (self.schema,)).fetchall()]
            for table in tables:
                for row in db.execute(sql.SQL('SELECT t::text FROM {}.{} t').format(
                        sql.Identifier(self.schema), sql.Identifier(table))).fetchall():
                    text = row[0] or ''
                    found += [table for secret in secrets if secret in text or secret.encode().hex() in text]
        if self.objects_root.exists():
            for path in self.objects_root.iterdir():
                data = path.read_bytes()
                found += ['object:' + path.name for secret in secrets if secret.encode() in data]
        return found

    def send(self, event, ctx=None):
        result = None
        for part in split_event(event):
            result = self.store.ingest_part(ctx or self.ctx, part)
        return result

    def index(self):
        from agenthub.source_index import SourceIndex
        with patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('no model')):
            return SourceIndex(self.store).run(max_sources=20, max_seconds=20)

    def search(self, ctx):
        return self.store.search(ctx, {'version': 'enterprise-local-1', 'query': 'Where is the Orchard dataset?',
            'project': 'maple'})

    # -- goal 1: redaction before any server persistence -------------------------
    def test_bypassed_client_secrets_never_reach_rows_objects_or_logs(self):
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Collect(level=logging.DEBUG); root = logging.getLogger()
        previous = root.level; root.addHandler(handler); root.setLevel(logging.DEBUG)
        try:
            result = self.send(bypassed_event())
            self.index()
        finally:
            root.removeHandler(handler); root.setLevel(previous)
        self.assertTrue(result['complete'])
        self.assertEqual(result['disposition'], 'redacted')
        self.assertEqual(self.leaks(), [])
        self.assertFalse([m for m in records for s in SECRETS.values() if s in m])
        payload = self.store.general_source(self.ctx, result['source_id'])['payload']
        self.assertIn(REDACTED_SECRET, json.dumps(payload))
        server = [item for item in payload['redactions'] if item['reason'].startswith('server_')]
        self.assertTrue(server)
        rendered = json.dumps(payload['redactions'])
        self.assertFalse([s for s in SECRETS.values() if s in rendered])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_parts').fetchone()[0], 0)

    def test_multipart_staging_is_removed_and_reassembly_is_redacted(self):
        event = bypassed_event('large', text='x' * 95000 + ' ' + TEXT)
        parts = split_event(event)
        self.assertGreater(len(parts), 1)
        self.assertFalse(self.store.ingest_part(self.ctx, parts[0])['complete'])
        for part in parts[1:]:
            result = self.store.ingest_part(self.ctx, part)
        self.assertTrue(result['complete'])
        self.assertEqual(self.leaks(), [])

    def unprotected(self, event_id):
        """Ingest with server re-redaction removed (and no object adapter, whose
        own fixed-point check would otherwise still refuse the payload)."""
        segments, self.store.conversation_segments = self.store.conversation_segments, None
        try:
            with patch('agenthub.general_sources.redact_capture', side_effect=lambda value: (value, [])):
                return self.send(bypassed_event(event_id))['source_id']
        finally:
            self.store.conversation_segments = segments

    def test_leak_check_fails_when_server_redaction_is_removed(self):
        """Proves the persistence test above can fail: disable re-redaction."""
        self.unprotected('unprotected')
        self.assertTrue(self.leaks())

    # -- goal 3: least-privilege raw originals -------------------------------
    def test_owner_reads_raw_other_principal_needs_explicit_grant(self):
        from agenthub import raw_access
        source = self.send(bypassed_event())['source_id']
        owner = self.ctx
        bob = self.store.authenticate(self.tokens['bob'])  # team member holding source_read
        self.assertNotIn(SECRETS['github'], json.dumps(self.store.general_source(owner, source)))
        self.store.source(owner, source)
        with self.assertRaises(Denied):
            self.store.general_source(bob, source)
        with self.assertRaises(Denied):
            self.store.source(bob, source)
        # Only the owner may grant; a grant names a principal or a project.
        with self.assertRaises(Denied):
            raw_access.grant(self.store, bob, 'source', source, 'principal', 'bob')
        raw_access.grant(self.store, owner, 'source', source, 'principal', 'bob')
        self.assertEqual(self.store.general_source(bob, source)['source_id'], source)
        raw_access.revoke(self.store, owner, 'source', source, 'principal', 'bob')
        with self.assertRaises(Denied):
            self.store.general_source(bob, source)
        raw_access.grant(self.store, owner, 'connection', 'protected-agent', 'project', 'maple')
        self.assertEqual(self.store.general_source(bob, source)['source_id'], source)
        carol = self.credential('carol', ['read', 'source_read'])
        with self.assertRaises(Denied):
            self.store.general_source(carol, source)  # not a project member
        self.store.set_membership('acme', 'maple', 'bob', False)
        with self.assertRaises(Denied):
            self.store.general_source(bob, source)
        # Granted principals still need source_read on their credential.
        self.store.set_membership('acme', 'maple', 'bob', True)
        with self.assertRaises(Denied):
            self.store.general_source(self.credential('bob', ['read']), source)
        with self.store.open() as state:
            audit = [row[0] for row in state.db.execute(
                "SELECT disposition FROM enterprise_audit WHERE operation='raw_grant' ORDER BY created").fetchall()]
        self.assertEqual(audit, ['granted', 'revoked', 'granted'])

    def test_original_check_sql_count_is_bounded(self):
        """Owner checks add no query; a grantee check adds exactly one grant lookup."""
        from agenthub import raw_access
        from agenthub.postgres import PostgresConnection
        source = self.send(bypassed_event())['source_id']
        raw_access.grant(self.store, self.ctx, 'source', source, 'principal', 'bob')
        bob = self.store.authenticate(self.tokens['bob'])
        original = PostgresConnection.execute
        counts = {}
        for name, ctx in (('owner', self.ctx), ('grantee', bob)):
            with self.store.open() as state:
                row = state.db.execute('SELECT * FROM enterprise_sources WHERE id=?', (source,)).fetchone()
                statements = []
                def counted(db, sql, params=None):
                    statements.append(sql); return original(db, sql, params)
                with patch.object(PostgresConnection, 'execute', counted):
                    self.assertTrue(self.store._original_allowed(state.db, ctx, row))
                counts[name] = statements
        self.assertFalse([sql for sql in counts['owner'] if 'raw_source_grants' in sql])
        self.assertEqual(len(counts['grantee']) - len(counts['owner']), 1)

    def test_default_credential_searches_redacted_passages_but_not_originals(self):
        from agenthub.source_evidence import document_evidence
        from agenthub.source_context import document_context
        source = self.send(bypassed_event())['source_id']
        self.index()
        default = self.credential('alice', ['ingest', 'read', 'correct', 'withdraw'])
        results = self.search(default)['results']
        self.assertTrue(results)
        self.assertIn(REDACTED_SECRET, json.dumps(results))
        card = results[0]
        evidence = document_evidence(self.store, default, card['id'], revision=card['revision'])
        self.assertEqual(evidence['sources'][0]['source_id'], source)
        self.assertIn(REDACTED_SECRET, evidence['sources'][0]['text'])
        self.assertTrue(document_context(self.store, default, card['id'], revision=card['revision'])['sources'])
        rendered = json.dumps([results, evidence])
        self.assertFalse([s for s in SECRETS.values() if s in rendered])
        with self.assertRaises(Denied):
            self.store.general_source(default, source)
        with self.assertRaises(Denied):
            self.store.source(default, source)
        # Passages remain owner-only: a team member sees none of them.
        self.assertEqual(self.search(self.store.authenticate(self.tokens['bob']))['results'], [])

    def test_document_originals_are_owner_only_by_default(self):
        from agenthub import raw_access
        from agenthub.document_ingest import DocumentStore
        self.store.enroll_connection(self.ctx, 'team-documents', 'docs', 'maple', ['document'],
            visibility='team', reader_ids=['alice', 'bob'])
        documents = DocumentStore(self.store, self.objects)
        body = b'Orchard guide: the dataset lives in /data/released/orchard.csv.\n'
        source = documents.ingest(self.ctx, 'team-documents', 'guide', '1', 'guide.md', io.BytesIO(body))['source_id']
        bob = self.store.authenticate(self.tokens['bob'])
        self.assertEqual(documents.describe(bob, source)['source_id'], source)  # metadata stays visible
        self.assertEqual([row['source_id'] for row in documents.list(bob)], [source])
        with self.assertRaises(Denied):
            documents.fetch(bob, source)
        with self.assertRaises(Denied):
            documents.authorize_original(bob, source)
        stream, _ = documents.fetch(self.ctx, source)
        with stream:
            self.assertEqual(stream.read(), body)
        with self.assertRaises(Denied):
            documents.fetch(self.credential('alice', ['ingest', 'read']), source)
        raw_access.grant(self.store, self.ctx, 'connection', 'team-documents', 'principal', 'bob')
        stream, _ = documents.fetch(bob, source)
        with stream:
            self.assertEqual(stream.read(), body)
        self.store.connection_policy(self.ctx, 'team-documents', reader_ids=['alice'])
        with self.assertRaises(Denied):
            documents.fetch(bob, source)  # owner narrowing still applies over a grant

    # -- goal 5: operator scan ------------------------------------------------
    def legacy(self, event_id):
        """Simulate content retained before server re-redaction existed."""
        return self.unprotected(event_id)

    def test_scan_counts_by_rule_and_owner_without_values(self):
        from agenthub.secret_scan import scan
        legacy = self.legacy('legacy')
        clean = self.send(bypassed_event('current'))['source_id']
        report = scan(self.store, objects=self.objects)
        rendered = json.dumps(report)
        self.assertFalse([s for s in SECRETS.values() if s in rendered])
        self.assertNotIn(legacy, rendered); self.assertNotIn(clean, rendered)
        self.assertEqual(report['locations']['source_payloads']['rows_scanned'], 2)
        self.assertEqual(report['locations']['source_payloads']['rows_with_findings'], 1)
        self.assertEqual(report['locations']['segment_objects'], {'rows_scanned': 1, 'rows_with_findings': 0, 'rules': {}})
        self.assertGreaterEqual(report['locations']['source_bodies']['rows_with_findings'], 1)
        for rule in ('github-token', 'aws-access-token', 'slack-token', 'stripe-access-token', 'credential-assignment'):
            self.assertGreaterEqual(report['rules'].get(rule, 0), 1, rule)
        self.assertEqual(set(report['by_owner']), {'alice'})
        self.assertFalse(report['values_reported'])

    def test_rewrite_is_dry_run_by_default_then_uses_revision_path(self):
        from agenthub.secret_scan import rewrite_redacted, scan
        legacy = self.legacy('legacy')

        def counts():
            with self.store.open() as state:
                return [state.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                        for t in ('backend_source_revisions', 'enterprise_sources', 'memories')]
        before = counts()
        plan = rewrite_redacted(self.store, self.ctx)
        self.assertEqual((plan['planned'], plan['rewritten'], plan['applied']), (1, 0, False))
        self.assertEqual(counts(), before)
        with self.assertRaises(Denied):
            rewrite_redacted(self.store, self.credential('alice', ['read']), apply=True)
        done = rewrite_redacted(self.store, self.ctx, apply=True)
        self.assertEqual((done['rewritten'], done['superseded_revisions_retained']), (1, 1))
        with self.store.open() as state:
            head = state.db.execute('''SELECT source_id,revision FROM backend_source_heads
                WHERE connection='protected-agent' AND external_id='legacy' ''').fetchone()
            retired = state.db.execute('SELECT active FROM enterprise_sources WHERE id=?', (legacy,)).fetchone()[0]
        self.assertEqual(head['revision'], '2'); self.assertEqual(retired, 0)
        current = self.store.general_source(self.ctx, head['source_id'])['payload']
        self.assertFalse([s for s in SECRETS.values() if s in json.dumps(current)])
        # No silent destructive delete: the superseded revision is still retained.
        self.assertGreaterEqual(scan(self.store)['locations']['source_payloads']['rows_with_findings'], 1)
        self.assertEqual(rewrite_redacted(self.store, self.ctx)['planned'], 0)


if __name__ == '__main__':
    unittest.main()
