"""Synthetic intake rejection before persistence; no model or private corpus."""
import io
import unittest
from unittest.mock import Mock

from agentclient.enterprise_capture import normalize_capture
from agenthub.document_ingest import DocumentStore
from agenthub.general_sources import GeneralSourcesMixin


class RedactionIntakeTests(unittest.TestCase):
    def test_general_source_rejects_nested_credentials_before_database(self):
        store = Mock()
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit', 'prompt': 'safe'}, 'project', 'connection')
        event['blocks'] = [{'type': 'tool_arguments', 'value': {'env': {'DATABASE_PASSWORD': 'fixture-password'}}}]
        with self.assertRaisesRegex(ValueError, '^unredacted_enterprise_source$'):
            GeneralSourcesMixin.ingest_general(store, {}, event)
        store.open.assert_not_called()

    def test_document_credentials_rejected_before_database_or_object_writes(self):
        for filename, raw in [('guide.md', b'{"password":"fixture-secret"}'),
                              ('guide.html', b'<script>password="fixture-secret"</script><p>safe</p>')]:
            with self.subTest(filename=filename):
                store = Mock(); objects = Mock()
                service = DocumentStore(store, objects)
                service._connection = Mock(return_value={'source_types': '["document"]', 'project': 'project'})
                with self.assertRaisesRegex(ValueError, '^secret_document_rejected$'):
                    service.ingest({'tenant': 'tenant'}, 'connection', 'id', '1', filename, io.BytesIO(raw))
                store.open.assert_not_called()
                objects.put.assert_not_called()

    def test_document_secret_metadata_rejected_before_objects(self):
        store = Mock(); objects = Mock()
        service = DocumentStore(store, objects)
        service._connection = Mock(return_value={'source_types': '["document"]', 'project': 'project'})
        with self.assertRaisesRegex(ValueError, '^secret_document_metadata_rejected$'):
            service.ingest({'tenant': 'tenant'}, 'connection', 'id', '1', 'guide.md', io.BytesIO(b'safe'),
                source_url='https://alice:fixture-pass@example.invalid/guide')
        objects.put.assert_not_called()


if __name__ == '__main__': unittest.main()
