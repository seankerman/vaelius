import unittest
from starlette.testclient import TestClient
from agenthub.cloud_api import create_app
from agenthub.enterprise import Denied
from contextlib import nullcontext

class Store:
    tenant_id='acme'
    def require_ready(self):pass
    def authenticate(self,token,request_id=None):
        if token!='synthetic':raise Denied()
        return {'tenant':'acme','actor':'alice','actions':['read'],'request_id':request_id}
    def delivery_lock(self):return nullcontext()
    def status(self,ctx):return {'tenant':'acme','principal':'alice'}
    def search(self,ctx,value):return {'results':[],'answerable':False}
class Registry:
    def store_for_token(self,token):return Store()

class ApiTests(unittest.TestCase):
    def setUp(self):self.client=TestClient(create_app(Registry(),allowed_hosts=['testserver']))
    def test_no_provider_on_health_and_startup(self):
        self.assertEqual(self.client.get('/health').json(),{'service':'Vaelius','models':'idle'})
    def test_auth_and_host_and_origin(self):
        self.assertEqual(self.client.get('/enterprise/v1/status').status_code,404)
        headers={'Authorization':'Bearer synthetic'}
        self.assertEqual(self.client.get('/enterprise/v1/status',headers=headers).status_code,200)
        self.assertEqual(self.client.get('/enterprise/v1/status',headers={**headers,'Origin':'https://evil.invalid'}).status_code,403)
        self.assertEqual(self.client.get('/enterprise/v1/status',headers={**headers,'Host':'evil.invalid'}).status_code,400)
    def test_bound_and_private_error(self):
        headers={'Authorization':'Bearer synthetic','Content-Type':'application/json'}
        self.assertEqual(self.client.post('/enterprise/v1/search',content=b'x'*262145,headers=headers).status_code,413)
        response=self.client.post('/enterprise/v1/search',content='{"query":"PRIVATE_CANARY',headers=headers)
        self.assertEqual(response.status_code,400);self.assertNotIn('PRIVATE_CANARY',response.text)
    def test_backend_abstention_retained(self):
        result=self.client.post('/enterprise/v1/search',json={'query':'unknown'},headers={'Authorization':'Bearer synthetic'}).json()
        self.assertFalse(result['answerable'])
