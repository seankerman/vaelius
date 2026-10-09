import unittest
from agenthub.mcp_tools import MemoryTools

class SourceEvidenceTransport(unittest.TestCase):

    def test_expansion_is_explicit_revision_bound_and_client_model_free(self):
        calls = []

        class Backend:

            def request(self, path, value=None):
                calls.append((path, value))
                if path.endswith('document-evidence'):
                    if value['revision']!='r1':
                        return {'id':'doc','status':'invalidated','current_revision':'r1'}
                    return {'id': 'doc', 'revision': 'r1', 'sources': [{'text': 'Cited original.'}]}
                return {'id': 'doc', 'revision': 'r1', 'claim': {'lesson': 'Curated summary.'}}
        _transport = Backend()
        tool = MemoryTools('maple', _transport)
        self.assertIn('claim', tool.call('fetch_memory', {'id': 'doc', 'include_sources': False}))
        self.assertEqual(len(calls), 1)
        value = tool.call('fetch_memory', {'id': 'doc', 'revision': 'r1', 'include_sources': True, 'source_offset': 3})
        self.assertEqual(value['sources'][0]['text'], 'Cited original.')
        self.assertEqual(calls[-1][1], {'id': 'doc', 'revision': 'r1', 'offset': 3})
        self.assertEqual(tool.call('fetch_memory', {'id': 'doc', 'revision': 'old', 'include_sources': True})['status'], 'invalidated')
        with self.assertRaises(ValueError):
            tool.call('fetch_memory', {'id': 'doc', 'include_sources': True, 'source_offset': 3})
