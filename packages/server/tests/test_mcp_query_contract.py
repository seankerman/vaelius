import hashlib, json
from pathlib import Path
import unittest

class QueryToolContractTests(unittest.TestCase):

    def fixture(self):
        root = Path(__file__).resolve().parent / 'fixtures'
        content = (root / 'cloud_query_tool_contract_v1.json').read_bytes()
        manifest = json.loads((root / 'cloud_query_tool_contract_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(content).hexdigest(), manifest['sha256'])
        return json.loads(content)

    def test_advertised_optional_filters_explain_current_and_historical_requests(self):
        from agenthub.mcp_tools import enterprise_tools
        fixture = self.fixture()
        tool = next((t for t in enterprise_tools() if t['name'] == 'search_memory'))
        self.assertEqual(tool['inputSchema']['required'], ['query'])
        for name, guidance in fixture['requirements'].items():
            if name in {'subject', 'domain', 'knowledge_type'}:
                continue
            description = tool['inputSchema']['properties'][name].get('description', '').casefold()
            for word in guidance:
                self.assertIn(word, description, name)

    def test_explicit_historical_and_subject_filters_are_preserved(self):
        from agenthub.mcp_tools import MemoryTools
        fixture = self.fixture()
        calls = []

        class Backend:

            def request(self, path, value):
                calls.append((path, value))
                return {'answerable': False, 'results': []}
        _transport = Backend()
        result = MemoryTools('demo', _transport).call('search_memory', {'query': fixture['query'], **{k: v for k, v in fixture['supplied_optional_filters'].items() if k not in {'subject', 'domain', 'knowledge_type'}}})
        self.assertFalse(result['answerable'])
        self.assertEqual(calls[0][0], '/enterprise/v3/search')
        for key, value in fixture['supplied_optional_filters'].items():
            if key in {'subject', 'domain', 'knowledge_type'}:
                continue
            self.assertEqual(calls[0][1][key], value)


class SearchScopeToolTests(unittest.TestCase):
    """search_memory: current project by default, other projects only on request, provenance kept."""

    def run_search(self, arguments, results=None):
        from agenthub.mcp_tools import MemoryTools
        calls = []

        class Backend:
            def request(self, path, value):
                calls.append((path, value))
                return {'answerable': bool(results), 'results': results or []}
        output = MemoryTools('maple', Backend()).call('search_memory', arguments)
        return output, calls[0][1]

    def test_scope_defaults_to_the_project_and_all_is_explicit(self):
        output, request = self.run_search({'query': 'Where is the dataset?'})
        self.assertEqual(request['project'], 'maple')
        self.assertNotIn('scope', request)
        self.assertEqual(output['scope'], 'project')
        output, request = self.run_search({'query': 'Where is the dataset?', 'scope': 'all'})
        self.assertEqual((request['scope'], output['scope']), ('all', 'all'))
        with self.assertRaises(ValueError):
            self.run_search({'query': 'Where is the dataset?', 'scope': 'everything'})

    def test_cross_project_provenance_reaches_the_tool_output(self):
        card = {'id': 'doc-1', 'revision': 'rev-1', 'title': 'Dataset', 'lesson': 'Moved.',
                'evidence_status': 'discovery_only', 'tier': 'organization',
                'origin': {'project': 'birch', 'sharing': 'organization'},
                'from': 'from birch · alice · 2026-10-01',
                'provenance': {'author': 'alice', 'project': 'birch', 'captured_at': '2026-10-01T00:00:00Z'}}
        output, _ = self.run_search({'query': 'Where is the dataset?'}, [card])
        self.assertEqual(output['records'], [card])
