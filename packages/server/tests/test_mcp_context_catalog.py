"""Backend-owned workflow discovery over official Streamable HTTP MCP."""
import unittest
from starlette.testclient import TestClient
from agenthub.cloud_api import create_app
from agenthub.mcp_tools import TOOLS
from agenthub.enterprise import Denied


class Store:
    tenant_id = 'synthetic'
    def require_ready(self): pass
    def authenticate(self, token, request_id=None):
        if token != 'synthetic': raise Denied()
        return {'tenant': self.tenant_id, 'actor': 'alice', 'actions': ['read'], 'request_id': request_id}


class Registry:
    def __init__(self, store): self.store = store
    def store_for_token(self, token): return self.store

class ContextCatalogTests(unittest.TestCase):
    def test_current_workflow_is_always_advertised(self):
        with TestClient(create_app(Registry(Store()),allowed_hosts=['testserver'])) as client:
            response=client.post('/mcp',headers={'Authorization':'Bearer synthetic',
                'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
                'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},
                    'clientInfo':{'name':'test','version':'1'}}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Memory tools are available',response.json()['result']['instructions'])

    def test_context_read_is_optional_and_read_only(self):
        spec=next(t for t in TOOLS if t['name']=='fetch_memory')
        self.assertTrue(spec['annotations']['readOnlyHint'])
        self.assertEqual(spec['inputSchema']['properties']['include_context']['default'],False)
        self.assertNotIn('include_context',spec['inputSchema']['required'])
