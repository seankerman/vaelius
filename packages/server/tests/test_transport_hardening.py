"""Transport hardening: server-side metering identity, private health, MCP
DNS-rebinding protection, rotating mounted secrets and declared production TLS."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agenthub.cloud_api import create_app
from agenthub.enterprise import Denied
from test_cloud_postgres import PostgresFixture, SERVICES

ACTIONS={'reader':['read'],'auditor':['read','audit'],'admin':['read','settings']}
BUILD={'agentclient':'synthetic-client-build','agenthub':'synthetic-hub-build'}


class Store:
    tenant_id='acme';dsn='synthetic-metered'
    def __init__(self):self.request_ids=[]
    def require_ready(self):pass
    def authenticate(self,token,request_id=None):
        if token not in ACTIONS:raise Denied()
        self.request_ids.append(request_id)
        return {'tenant':'acme','actor':token,'principal':token,'actions':ACTIONS[token],'request_id':request_id}
    def delivery_lock(self):return nullcontext()
    def status(self,ctx):return {'tenant':'acme'}
    def search(self,ctx,value):return {'results':[],'answerable':False}


class Registry:
    def __init__(self,store=None):self.store=store or Store()
    def store_for_token(self,token):return self.store


class RecordingMeter:
    idents=[]
    def __init__(self,store):pass
    def metric(self,ident,kind,amount,*,details=None):RecordingMeter.idents.append((ident,kind))


class MeteringIdentity(unittest.TestCase):
    def test_client_request_id_never_keys_metering_or_context(self):
        store=Store();RecordingMeter.idents=[]
        client=TestClient(create_app(Registry(store),allowed_hosts=['testserver']))
        with patch('agenthub.cloud_ops.Meter',RecordingMeter):
            for token in ('reader','reader','auditor'):
                response=client.post('/enterprise/v3/search',json={'query':'synthetic'},
                    headers={'Authorization':'Bearer '+token,'X-Request-ID':'CLIENT_CHOSEN'})
                self.assertEqual(response.status_code,200)
        requests=[ident for ident,kind in RecordingMeter.idents if kind=='retrieval_requests']
        self.assertEqual(len(requests),3)
        self.assertEqual(len(set(requests)),3) # Reuse cannot collapse requests into one.
        self.assertFalse(any('CLIENT_CHOSEN' in ident for ident,_ in RecordingMeter.idents))
        self.assertNotIn('CLIENT_CHOSEN',store.request_ids)


@unittest.skipUnless(SERVICES,'explicit real PostgreSQL services manifest required')
class MeteringPostgres(PostgresFixture,unittest.TestCase):
    def setUp(self):self.postgres_setup()
    def tearDown(self):self.postgres_teardown()

    def test_reused_request_id_neither_undercounts_nor_conflicts_across_principals(self):
        from agentclient.enterprise_contract import VERSION
        from agenthub.cloud_ops import Meter
        store=self.store
        registry=type('Registry',(),{'store_for_token':lambda _,token:store})()
        client=TestClient(create_app(registry,allowed_hosts=['testserver']))
        for name in ('alice','alice','bob'):
            response=client.post('/enterprise/v3/search',
                json={'version':VERSION,'query':'synthetic metering question','project':'maple'},
                headers={'Authorization':'Bearer '+self.tokens[name],'X-Request-ID':'reused-by-client'})
            self.assertEqual(response.status_code,200,response.text)
        # Before server-side identities, the replay and the other principal's
        # request collapsed into the first row: 1 counted instead of 3.
        self.assertEqual(Meter(store).status()['metrics']['retrieval_requests'],3)


class PrivateHealth(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(create_app(Registry(),allowed_hosts=['testserver'],build_ids=BUILD))

    def test_health_is_liveness_only(self):
        response=self.client.get('/health')
        self.assertEqual(response.json(),{'service':'Vaelius','models':'idle'})
        self.assertNotIn('synthetic-hub-build',response.text)

    def test_build_identity_requires_audit_or_settings(self):
        path='/enterprise/v3/service-status'
        self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.get(path,headers={'Authorization':'Bearer reader'}).status_code,404)
        for token in ('auditor','admin'):
            response=self.client.get(path,headers={'Authorization':'Bearer '+token})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['build_ids'],BUILD)


class SecureHeaders(unittest.TestCase):
    def test_every_response_is_private_and_unsniffable(self):
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver']))
        for response in (client.get('/health'),client.get('/enterprise/v1/status'),
                         client.get('/enterprise/v1/status',headers={'Authorization':'Bearer reader'}),
                         client.get('/health',headers={'Host':'evil.invalid'})):
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(response.headers['x-content-type-options'],'nosniff')
            self.assertEqual(response.headers['x-frame-options'],'DENY')
            self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
            self.assertNotIn('strict-transport-security',response.headers)
            self.assertNotIn('no-store, no-store',response.headers['cache-control'])

    def test_https_only_refuses_plain_http_except_probes(self):
        app=create_app(Registry(),allowed_hosts=['testserver'],https_only=True)
        plain=TestClient(app)
        self.assertEqual(plain.get('/health').status_code,200)
        refused=plain.get('/enterprise/v1/status',headers={'Authorization':'Bearer reader'})
        self.assertEqual(refused.status_code,403)
        self.assertEqual(refused.json(),{'error':'https_required'})
        secure=TestClient(app,base_url='https://testserver')
        response=secure.get('/enterprise/v1/status',headers={'Authorization':'Bearer reader'})
        self.assertEqual(response.status_code,200)
        self.assertIn('max-age=',response.headers['strict-transport-security'])

    def test_only_declared_proxy_can_mark_requests_https(self):
        import httpx
        import uvicorn
        from agenthub.cloud_runtime import server_options
        def serve(proxies):
            app=create_app(Registry(),allowed_hosts=['127.0.0.1'],https_only=True)
            options=server_options({'tls':'proxy','trusted_proxies':proxies},'127.0.0.1')
            sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(8)
            server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,**options))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            deadline=time.monotonic()+5
            while not server.started and time.monotonic()<deadline:time.sleep(.01)
            self.addCleanup(lambda:(setattr(server,'should_exit',True),thread.join(timeout=5),sock.close()))
            return 'http://127.0.0.1:%d/enterprise/v1/status'%sock.getsockname()[1]
        headers={'Authorization':'Bearer reader','X-Forwarded-Proto':'https'}
        with httpx.Client(trust_env=False) as http:
            self.assertEqual(http.get(serve(['127.0.0.1/32']),headers=headers).status_code,200)
            # The same header from an address outside trusted_proxies is ignored.
            self.assertEqual(http.get(serve(['192.0.2.0/24']),headers=headers).status_code,403)


class McpDnsRebinding(unittest.TestCase):
    HEADERS={'Authorization':'Bearer reader','Accept':'application/json, text/event-stream',
             'MCP-Protocol-Version':'2025-11-25'}

    def mcp_only(self,security=None):
        # Mount the MCP endpoint WITHOUT TrustedHostMiddleware: the SDK check is
        # the only Host validation left, as when a deployment re-mounts it.
        from agenthub import mcp_server
        store=Store()
        def authenticate(request):return store,store.authenticate(request.headers['authorization'][7:])
        def deliver(*args):raise AssertionError('not called')
        if security is None:
            endpoint,lifespan=mcp_server.build_mcp(authenticate,deliver,allowed_hosts=['testserver'],allowed_origins=[])
        else:
            with patch.object(mcp_server,'transport_security',return_value=security):
                endpoint,lifespan=mcp_server.build_mcp(authenticate,deliver,allowed_hosts=['testserver'],allowed_origins=[])
        app=Starlette(routes=[Route('/mcp',endpoint,methods=['GET','POST','DELETE'])],lifespan=lifespan)
        return self.enterContext(TestClient(app))

    def call(self,client,**headers):
        return client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}},
            headers={**self.HEADERS,**headers})

    def test_sdk_rejects_rebound_host_without_trusted_host_middleware(self):
        client=self.mcp_only()
        self.assertEqual(self.call(client).status_code,200)
        self.assertEqual(self.call(client,Host='testserver:8443').status_code,200)
        self.assertEqual(self.call(client,Host='evil.invalid').status_code,421)

    def test_disabled_sdk_protection_would_admit_rebound_host(self):
        # Proves the regression above depends on the SDK setting, not on guarded().
        from mcp.server.transport_security import TransportSecuritySettings
        client=self.mcp_only(TransportSecuritySettings(enable_dns_rebinding_protection=False))
        self.assertEqual(self.call(client,Host='evil.invalid').status_code,200)

    def test_allowlist_is_exact_and_fails_closed(self):
        from agenthub.mcp_server import transport_security
        security=transport_security(['127.0.0.1','::1','memory.example.test'],['https://app.example.test'])
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn('127.0.0.1:*',security.allowed_hosts)
        self.assertIn('[::1]:*',security.allowed_hosts)
        self.assertIn('https://memory.example.test',security.allowed_origins)
        self.assertIn('https://app.example.test',security.allowed_origins)
        for hosts in ([],['*'],['*.example.test'],[''],[None]):
            with self.subTest(hosts=hosts),self.assertRaises(ValueError):transport_security(hosts,[])
        with self.assertRaises(ValueError):create_app(Registry(),allowed_hosts=['*'])


class RotatingSecret(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now=[0.0]

    def write(self,name,value,mode=0o600):
        path=self.root/name;path.write_text(value+'\n');path.chmod(mode);return path

    def secret(self,path):
        from agenthub.secret_files import MountedSecret
        return MountedSecret(path,check_seconds=30,clock=lambda:self.now[0])

    def test_kubernetes_style_symlink_swap_is_picked_up_after_bounded_interval(self):
        self.write('v1','synthetic-secret-one');self.write('v2','synthetic-secret-two')
        link=self.root/'client-secret';link.symlink_to('v1')
        secret=self.secret(link)
        self.assertEqual(secret.value(),'synthetic-secret-one')
        swap=self.root/'swap';swap.symlink_to('v2');os.replace(swap,link)
        self.now[0]=29
        with patch('agenthub.secret_files.read_secret',side_effect=AssertionError('read before interval')):
            self.assertEqual(secret.value(),'synthetic-secret-one')
        self.now[0]=31
        self.assertEqual(secret.value(),'synthetic-secret-two')
        self.assertNotIn('synthetic-secret',repr(secret))

    def test_unchanged_file_is_not_reread_and_unsafe_rotation_keeps_last_value(self):
        path=self.write('client-secret','synthetic-secret-one')
        secret=self.secret(path)
        self.now[0]=100
        with patch('agenthub.secret_files.read_secret',side_effect=AssertionError('reread unchanged')):
            self.assertEqual(secret.value(),'synthetic-secret-one')
        replacement=self.write('next','synthetic-secret-two',mode=0o644);os.replace(replacement,path)
        self.now[0]=200
        self.assertEqual(secret.value(),'synthetic-secret-one')
        path.chmod(0o600);self.now[0]=300
        self.assertEqual(secret.value(),'synthetic-secret-two')

    def test_startup_fails_closed(self):
        with self.assertRaises(OSError):self.secret(self.root/'missing')
        with self.assertRaisesRegex(ValueError,'provider_credential_permissions'):
            self.secret(self.write('open','synthetic',mode=0o644))

    def test_introspection_uses_rotated_secret(self):
        from types import SimpleNamespace
        from agenthub.oauth_provider import OAuthProvider
        path=self.write('client-secret','synthetic-secret-one')
        sent=[]
        def post(url,**kwargs):
            sent.append(kwargs['auth'][1]);return SimpleNamespace(status_code=401,content=b'{}')
        from agenthub.secret_files import MountedSecret
        provider=OAuthProvider({'issuer':'https://identity.example.test','tenant':'acme','client_ids':['plugin'],
            'introspection_client_id':'api','client_secret_file':str(path)},http=SimpleNamespace(post=post))
        self.assertIsInstance(provider._secret,MountedSecret)
        provider._secret._clock=lambda:self.now[0];provider._secret._checked=0
        provider.metadata=lambda:{'introspection_endpoint':'https://identity.example.test/introspect'}
        with self.assertRaises(Denied):provider.introspect('synthetic','https://memory.example.test/mcp')
        os.replace(self.write('next','synthetic-secret-two'),path);self.now[0]=60
        with self.assertRaises(Denied):provider.introspect('synthetic','https://memory.example.test/mcp')
        self.assertEqual(sent,['synthetic-secret-one','synthetic-secret-two'])


class ProductionDeployment(unittest.TestCase):
    HOSTS={'allowed_hosts':['memory.example.test']}

    def check(self,deployment,**extra):
        from agenthub.cloud_runtime import deployment_settings
        return deployment_settings({**self.HOSTS,'deployment':deployment,**extra})

    def test_production_requires_declared_transport_and_exact_hosts(self):
        from agenthub.cloud_runtime import deployment_settings
        self.assertEqual(deployment_settings({})['mode'],'development')
        refused=[({'mode':'production'},{}),
                 ({'mode':'production','tls':'proxy'},{}),
                 ({'mode':'production','tls':'proxy','trusted_proxies':['*']},{}),
                 ({'mode':'production','tls':'proxy','trusted_proxies':['0.0.0.0/0']},{}),
                 ({'mode':'production','tls':'direct'},{}),
                 ({'mode':'production','tls':'proxy','trusted_proxies':['10.0.0.0/16']},{'allowed_hosts':['*']}),
                 ({'mode':'production','tls':'none'},{}),
                 ({'mode':'staging'},{})]
        for deployment,extra in refused:
            with self.subTest(deployment=deployment,extra=extra),self.assertRaises(ValueError):
                self.check(deployment,**extra)
        with self.assertRaises(ValueError):
            deployment_settings({'deployment':{'mode':'production','tls':'proxy','trusted_proxies':['10.0.0.0/16']}})
        accepted=self.check({'mode':'production','tls':'proxy','trusted_proxies':['10.0.0.0/16','192.0.2.7']})
        self.assertEqual(accepted['mode'],'production')

    def test_transport_options(self):
        from agenthub.cloud_runtime import server_options
        self.assertEqual(server_options({'tls':None},'127.0.0.1'),{'proxy_headers':False,'server_header':False})
        proxy=server_options({'tls':'proxy','trusted_proxies':['10.0.0.0/16']},'0.0.0.0')
        self.assertEqual((proxy['proxy_headers'],proxy['forwarded_allow_ips']),(True,['10.0.0.0/16']))
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root/'tls.crt').write_text('synthetic');key=root/'tls.key';key.write_text('synthetic')
        key.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'deployment_tls_key_permissions'):
            self.check({'mode':'production','tls':'direct','certfile':str(root/'tls.crt'),'keyfile':str(key)})
        key.chmod(0o600)
        direct=server_options(self.check({'mode':'production','tls':'direct','certfile':str(root/'tls.crt'),
            'keyfile':str(key)}),'0.0.0.0')
        self.assertEqual(direct['ssl_keyfile'],str(key))

    def test_production_profile_without_transport_never_listens(self):
        from agenthub import cloud_runtime
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        runtime=root/'runtime.json'
        runtime.write_text(json.dumps({'provider_mode':'off','control_dsn':'postgresql://synthetic.invalid/x',
            'allowed_hosts':['memory.example.test'],'deployment':{'mode':'production'}}))
        runtime.chmod(0o600)
        with patch('sys.argv',['vaelius','--profile',str(root),'--bind','0.0.0.0']),\
                patch('uvicorn.run') as run,patch.object(cloud_runtime,'app_from_profile') as build,\
                self.assertRaisesRegex(ValueError,'production_requires_tls_or_trusted_proxy'):
            cloud_runtime.main()
        run.assert_not_called();build.assert_not_called()


if __name__=='__main__':unittest.main()
