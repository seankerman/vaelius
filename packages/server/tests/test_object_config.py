"""Every service entry point uses the same source-object configuration."""
import tempfile
import unittest
from unittest.mock import patch

from agenthub.object_config import objects_from_settings
from agenthub.source_objects import FileSourceObjects


class ObjectConfiguration(unittest.TestCase):
    def test_file_adapter_uses_configured_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsInstance(objects_from_settings({
                'objects': {'kind': 'file', 'root': directory}}), FileSourceObjects)

    def test_s3_configuration_preserves_local_endpoint_controls(self):
        config = {'kind': 's3', 'endpoint': 'http://localhost:9000',
                  'bucket': 'synthetic', 'access_key': 'fixture-key',
                  'secret_key': 'fixture-secret', 'local_hosts': ['localhost']}
        with patch('agenthub.source_objects.S3SourceObjects') as adapter:
            self.assertIs(objects_from_settings({'objects': config}), adapter.return_value)
            adapter.assert_called_once_with('http://localhost:9000', 'synthetic',
                'fixture-key', 'fixture-secret', local_hosts=['localhost'])

    def test_unknown_kind_does_not_fall_through_to_s3(self):
        with patch('agenthub.source_objects.S3SourceObjects') as adapter:
            with self.assertRaisesRegex(ValueError, 'unsupported_object_adapter'):
                objects_from_settings({'objects': {'kind': 'misspelled'}})
            adapter.assert_not_called()
