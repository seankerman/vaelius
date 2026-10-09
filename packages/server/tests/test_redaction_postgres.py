"""Security regression fixtures through canonical PostgreSQL intake."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from agentclient.enterprise_capture import normalize_capture
from agentclient.general_contract import split_event
from agenthub.document_ingest import DocumentStore
from agenthub.source_objects import FileSourceObjects
from vaelius_test_support.fixtures.enterprise import EnterpriseStore


class RedactionPostgresTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = EnterpriseStore(Path(self.temp.name) / 'hub')
        self.store.create_organization('orchard')
        self.store.create_principal('orchard', 'alice')
        self.store.create_project('orchard', 'maple')
        self.store.set_membership('orchard', 'maple', 'alice', True)
        token = self.store.enroll('orchard', 'alice', 'device', ['ingest', 'read', 'source_read', 'policy'])
        self.ctx = self.store.authenticate(token)
        self.store.enroll_connection(self.ctx, 'capture', 'codex', 'maple', ['agent', 'document'])

    def test_bypassed_client_multipart_secret_is_rejected_and_staging_removed(self):
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit', 'event_id': 'source',
            'prompt': 'safe'}, 'maple', 'capture')
        event['blocks'][0]['value'] = 'x' * 95000 + '{"password":"fixture-secret"}'
        parts = split_event(event)
        self.assertFalse(self.store.ingest_part(self.ctx, parts[0])['complete'])
        with self.assertRaisesRegex(ValueError, '^invalid_or_unredacted_enterprise_source$'):
            self.store.ingest_part(self.ctx, parts[1])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_parts').fetchone()[0], 0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_source_revisions').fetchone()[0], 0)
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_jobs').fetchone()[0], 0)

    def test_normalized_redaction_round_trip_and_duplicate(self):
        event = normalize_capture({'hook_event_name': 'PostToolUse', 'event_id': 'source',
            'tool_input': {'DB_PASSWORD': 'fixture-secret'},
            'tool_response': {'text': '前 café /Users/alice/data/étude.csv'}}, 'maple', 'capture')
        result = self.store.ingest_general(self.ctx, event)
        loaded = self.store.general_source(self.ctx, result['source_id'])['payload']
        self.assertEqual(loaded, event)
        self.assertNotIn('fixture-secret', json.dumps(loaded))
        self.assertEqual(self.store.ingest_general(self.ctx, event)['disposition'], 'duplicate')

    def test_secret_original_rejected_safe_original_keeps_bytes(self):
        objects = FileSourceObjects(Path(self.temp.name) / 'objects')
        documents = DocumentStore(self.store, objects)
        with self.assertRaisesRegex(ValueError, '^secret_document_rejected$'):
            documents.ingest(self.ctx, 'capture', 'unsafe', '1', 'guide.md', io.BytesIO(b'{"password":"fixture-secret"}'))
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_object_uploads').fetchone()[0], 0)
        body = '前 café dataset /Users/alice/data/étude.csv\nContact alice@example.invalid.\n'.encode()
        result = documents.ingest(self.ctx, 'capture', 'safe', '1', 'guide.md', io.BytesIO(body))
        with self.store.open() as state:
            row = state.db.execute('SELECT object_key,sha256 FROM backend_document_versions WHERE source_id=?',
                (result['source_id'],)).fetchone()
        with objects.open(row['object_key']) as original:
            self.assertEqual(original.read(), body)


if __name__ == '__main__': unittest.main()
