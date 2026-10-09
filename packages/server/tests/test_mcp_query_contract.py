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
