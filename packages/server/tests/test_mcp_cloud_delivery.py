import json
import unittest

class CloudDeliveryTests(unittest.TestCase):

    def backend(self):

        class Backend:

            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append((path, value))
                if path.endswith('preferences'):
                    return {'values': {'test_runner': 'pytest'}, 'private_owner': 'alice', 'preferences': [{'key': 'test_runner', 'value': 'pytest', 'scope': 'user', 'project': None, 'task': None, 'document_id': 'private-pref', 'revision_id': 'r2'}]}
                if path.endswith('search'):
                    return {'answerable': False, 'results': [{'id': 'candidate', 'revision': 'r1', 'title': 'Nearby evidence', 'lesson': 'Uncertain', 'evidence_status': 'unverified'}]}
                return {'status': 'recorded'}
        return Backend()

    def test_mcp_preserves_backend_none_with_candidates(self):
        from agenthub.mcp_tools import MemoryTools
        _transport = self.backend()
        result = MemoryTools('demo', _transport).call('search_memory', {'query': 'approved budget'})
        self.assertFalse(result['answerable'])

    def test_preference_tool_uses_bound_user_and_project(self):
        from agenthub.mcp_tools import MemoryTools, enterprise_tools
        self.assertIn('get_user_preferences', {t['name'] for t in enterprise_tools()})
        backend = self.backend()
        _transport = backend
        result = MemoryTools('demo', _transport).call('get_user_preferences', {})
        self.assertEqual(result['values'], {'test_runner': 'pytest'})
        self.assertEqual(backend.calls[0][1]['project'], 'demo')

    def test_original_discovery_exposes_source_id_without_document_bytes(self):
        from agenthub.mcp_tools import MemoryTools, enterprise_tools
        self.assertIn('find_source_documents', {t['name'] for t in enterprise_tools()})
        backend = self.backend()
        documents = [{'source_id': 'original-7', 'version': '2', 'filename': 'design.md', 'title': 'Project design', 'parser_status': 'parsed'}]
        original = backend.request

        def request(path, value=None):
            if path.endswith('source-documents/list'):
                backend.calls.append((path, value))
                return {'documents': documents}
            return original(path, value)
        backend.request = request
        _transport = backend
        result = MemoryTools('demo', _transport).call('find_source_documents', {'title': 'Project design', 'limit': 5})
        self.assertEqual(result['documents'], documents)
        self.assertEqual(backend.calls[0][1], {'title': 'Project design', 'limit': 5})
        self.assertLessEqual(len(json.dumps(result)), 4000)
