"""Frozen replay regressions now owned by the backend MCP catalogue."""
import unittest
from unittest.mock import Mock
from agenthub.mcp_tools import MemoryTools, enterprise_tools

class RetrievalInterface(unittest.TestCase):
    def test_search_does_not_offer_guessable_metadata_filters(self):
        spec=next(t for t in enterprise_tools() if t['name']=='search_memory')
        self.assertFalse({'domain','subject','knowledge_type'} & set(spec['inputSchema']['properties']))
        self.assertIn('as_of',spec['inputSchema']['properties'])

    def test_default_fetch_reads_cited_evidence_before_surrounding_context(self):
        backend=Mock()
        backend.request.return_value={'id':'doc','revision':'r1','sources':[{'text':'Saved in data/cedar.csv'}],'next_offset':None}
        result=MemoryTools('demo',backend).call('fetch_memory',{'id':'doc','revision':'r1'})
        self.assertEqual(backend.request.call_args.args[0],'/enterprise/v3/document-evidence')
        self.assertEqual(result['sources'][0]['text'],'Saved in data/cedar.csv')
        self.assertEqual(backend.request.call_count,1)
