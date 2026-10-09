"""Frozen backend MCP controls: real SDK transport, shared service operations."""
import json
from pathlib import Path
import unittest
from contextlib import nullcontext
from starlette.testclient import TestClient
from agenthub.cloud_api import create_app
from agenthub.enterprise import Denied

HEADERS={'Authorization':'Bearer synthetic','Accept':'application/json, text/event-stream',
    'MCP-Protocol-Version':'2025-11-25','X-AgentNetwork-Project':'maple'}

class Store:
    tenant_id='acme'
    def __init__(self):self.active=True;self.calls=[];self.checked=0
    def require_ready(self):pass
    def authenticate(self,token,request_id=None):
        if token!='synthetic' or not self.active:raise Denied()
        return {'tenant':'acme','actor':'alice','actions':['read'],'request_id':request_id}
    def delivery_read_lock(self):return nullcontext()
    def delivery_lock(self):return nullcontext()
    def status(self,ctx):return {'tenant':'acme','principal':'alice'}
    def search(self,ctx,value):
        self.calls.append(value)
        if value['project']!='maple':raise Denied()
        return {'results':[{'id':'d','revision':'r1','title':'Guide','lesson':'Use the guide.'}], 'answerable':True}
    def validate_search_delivery(self,ctx,value,result):self.checked+=1;return result

class BackendMCP(unittest.TestCase):
    def test_official_client_over_real_loopback_http(self):
        import asyncio
        import socket
        import threading
        import time
        import httpx2
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        store=self.store
        registry=type('Registry',(),{'store_for_token':lambda _,token:store})()
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(8)
        port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(create_app(registry),log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        thread.start()
        try:
            deadline=time.monotonic()+5
            while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(server.started)
            async def check():
                async with httpx2.AsyncClient(headers=HEADERS,timeout=10) as http:
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http) as streams:
                        async with ClientSession(*streams,read_timeout_seconds=10) as session:
                            await session.initialize()
                            self.assertEqual(len((await session.list_tools()).tools),13)
                            result=await session.call_tool('search_memory',{'query':'Where is the guide?'})
                            self.assertFalse(result.is_error)
                            self.assertEqual(json.loads(result.content[0].text)['records'][0]['revision'],'r1')
            asyncio.run(check())
        finally:
            server.should_exit=True;thread.join(timeout=5);sock.close()
        self.assertFalse(thread.is_alive())

    def setUp(self):
        self.fixture=json.loads((Path(__file__).parent/'fixtures/backend_mcp_v1.json').read_text())
        self.store=Store();store=self.store
        registry=type('Registry',(),{'store_for_token':lambda _,token:store})()
        self.client=self.enterContext(TestClient(create_app(registry,allowed_hosts=['testserver'])))

    def rpc(self,method,params=None,headers=None):
        return self.client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},headers=HEADERS if headers is None else headers)

    def test_real_protocol_initialization_and_catalog(self):
        response=self.rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('vaelius',response.json()['result']['serverInfo']['name'])
        catalog=self.rpc('tools/list').json()['result']['tools']
        self.assertEqual(len(catalog),13)
        fetch=next(t for t in catalog if t['name']=='fetch_source_document')
        self.assertNotIn('download_to',fetch['inputSchema']['properties'])
        self.assertEqual(self.store.calls,[])

    def test_auth_and_origin_apply_to_every_request(self):
        self.assertEqual(self.rpc('tools/list',headers={'Accept':HEADERS['Accept']}).status_code,401)
        self.assertEqual(self.rpc('tools/list',headers={**HEADERS,'Origin':'https://evil.invalid'}).status_code,403)
        self.assertEqual(self.rpc('tools/list',headers={**HEADERS,'Host':'evil.invalid'}).status_code,400)
        self.assertEqual(self.rpc('tools/list').status_code,200)
        self.store.active=False
        self.assertEqual(self.rpc('tools/list').status_code,401)

    def test_search_uses_current_shared_delivery_and_project(self):
        value=self.rpc('tools/call',{'name':'search_memory','arguments':{'query':'Where is the guide?'}})
        self.assertEqual(value.status_code,200,value.text)
        self.assertFalse(value.json()['result'].get('isError'),value.text)
        result=json.loads(value.json()['result']['content'][0]['text'])
        self.assertEqual(result['records'][0]['revision'],'r1')
        self.assertEqual(self.store.calls[0]['project'],'maple')
        self.assertEqual(self.store.checked,1)
        denied=self.rpc('tools/call',{'name':'search_memory','arguments':{'query':'guide','project':'other'}})
        self.assertTrue(denied.json()['result']['isError'])

    def test_invalid_nested_writes_and_unknown_tools_fail_without_content(self):
        for name,args in [('unknown',{}),('submit_memory_candidate',{'request_key':'same','title':'Note','evidence':'x'*30,'source_ids':'wrong'}),
                          ('fetch_source_document',{'source_id':'s','download_to':'/server/private'})]:
            result=self.rpc('tools/call',{'name':name,'arguments':args})
            self.assertTrue(result.json()['result']['isError'],result.text)
            self.assertNotIn('/server/private',result.text)
        self.assertEqual(self.store.calls,[])
