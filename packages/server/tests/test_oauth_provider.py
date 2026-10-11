"""Frozen provider boundary and real Keycloak/installed client/MCP acceptance."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_cloud_postgres import PostgresFixture,SERVICES
from agenthub.enterprise import Denied


class ProviderSecurity(unittest.TestCase):
    def test_introspection_rejects_wrong_audience_client_refresh_and_expiry(self):
        from agenthub.oauth_provider import OAuthProvider
        provider=OAuthProvider.__new__(OAuthProvider)
        provider.issuer='https://identity.example';provider.client_id='api';provider.secret='synthetic'
        provider.client_ids=['plugin'];provider.actions={'read'}
        provider.metadata=lambda:{'introspection_endpoint':'https://identity.example/introspect'}
        from types import SimpleNamespace
        good={'active':True,'iss':provider.issuer,'aud':['https://memory.example/mcp'],
              'client_id':'plugin','sub':'subject','exp':time.time()+60,'scope':'vaelius:read','typ':'Bearer'}
        for change in ({'aud':['other']},{'client_id':'other'},{'active':False},{'typ':'Refresh'},
                       {'exp':time.time()-1},{'scope':'openid'},{'iss':'https://evil.example'}):
            provider.http=SimpleNamespace(post=lambda *a,**k:SimpleNamespace(status_code=200,content=b'{}',json=lambda:good|change))
            with self.subTest(change=change),self.assertRaises(Denied):provider.introspect('synthetic','https://memory.example/mcp')


@unittest.skipUnless(SERVICES and os.environ.get('VAELIUS_IDENTITY_FIXTURE'),'explicit disposable PostgreSQL and Keycloak required')
class ActualOAuth(PostgresFixture,unittest.TestCase):
    def setUp(self):
        import socket,threading,uuid
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        from agenthub.postgres import connect,migrate
        from agenthub.oauth_provider import OAuthRegistry
        from agenthub.cloud_api import create_app
        import uvicorn
        self.fixture=json.loads(Path(os.environ['VAELIUS_IDENTITY_FIXTURE']).read_text())
        self.assertTrue(self.fixture['disposable']);self.postgres_setup();self.addCleanup(self.postgres_teardown)
        self.control_schema='test_oauth_'+uuid.uuid4().hex[:12]
        admin=self.services['admin_dsn']
        with connect(admin) as db:db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.control_schema)))
        self.control=make_conninfo(admin,options='-c search_path='+self.control_schema+',public');migrate(self.control,control=True)
        def clean_control():
            with connect(admin) as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.control_schema)))
        self.addCleanup(clean_control)
        self.registry=OAuthRegistry(self.control,Path(self.temp.name)/'registry',authorization={
            'resource':self.fixture['resource'],'providers':[self.fixture['provider']]})
        self.registry.register('acme',self.dsn)
        self.store=self.registry.resolve('acme')
        self.url=self.fixture['resource'].removesuffix('/mcp')
        self.bind_users()
        port=int(self.url.rsplit(':',1)[1]);sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        sock.bind(('127.0.0.1',port));sock.listen(32)
        self.server=uvicorn.Server(uvicorn.Config(create_app(self.registry),log_level='error',access_log=False))
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[sock]},daemon=True);self.thread.start()
        def stop():self.server.should_exit=True;self.thread.join(10)
        self.addCleanup(stop)
        for _ in range(100):
            if self.server.started:break
            time.sleep(.02)
        self.assertTrue(self.server.started)

    def bind_users(self):
        import requests
        self.http=requests.Session();self.http.trust_env=False
        f=self.fixture
        response=self.http.post(f['base_url']+'/realms/master/protocol/openid-connect/token',data={
            'grant_type':'password','client_id':'admin-cli','username':'admin','password':f['admin_password']},timeout=10)
        response.raise_for_status();self.admin_header={'Authorization':'Bearer '+response.json()['access_token']}
        users=self.http.get(f['base_url']+'/admin/realms/vaelius/users',headers=self.admin_header,timeout=10).json()
        self.users={u['username']:u['id'] for u in users}
        for name,subject in self.users.items():self.store.identity_bindings.bind(f['issuer'],subject,'acme',name)

    def browser(self,username='alice'):
        from html.parser import HTMLParser
        from urllib.parse import urlsplit
        from urllib.request import urlopen
        class Form(HTMLParser):
            action=None
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='form' and attrs.get('id')=='kc-form-login':self.action=attrs['action']
        def login(url):
            import requests
            http=requests.Session();http.trust_env=False
            response=http.get(url,timeout=10,allow_redirects=False)
            # Browser secure-context localhost handling differs from requests.
            # This exception exists only in the disposable loopback login driver.
            for cookie in http.cookies:
                if urlsplit(url).hostname=='127.0.0.1':cookie.secure=False
            if response.status_code==200:
                parser=Form();parser.feed(response.text);self.assertIsNotNone(parser.action)
                response=http.post(parser.action,data={'username':username,'password':self.fixture['password']},
                    timeout=10,allow_redirects=False)
            self.assertIn(response.status_code,(302,303))
            callback=response.headers['Location']
            self.assertTrue(callback.startswith(self.fixture['callback_uri']+'?'))
            with urlopen(callback,timeout=5) as received:self.assertEqual(received.status,200)
        return login

    def login(self,home,*,reauthenticate=False,username='alice',enrollment='synthetic-device'):
        from agentclient.cloud_enroll import CallbackServer
        from agentclient.oauth import enroll
        with CallbackServer(self.fixture['callback_uri']) as callback:
            return enroll(home,url=self.url,tenant='acme',client_id='vaelius-plugin',enrollment=enrollment,
                callback=callback,credential_store='file',reauthenticate=reauthenticate,browser=self.browser(username),timeout=20)

    def test_code_pkce_refresh_original_and_mcp_delivery_then_revocation(self):
        from agentclient.credentials import access_token,renew,logout
        from agentclient.transport import enterprise_client
        from agenthub.source_index import SourceIndex
        home=Path(self.temp.name)/'client';self.login(home)
        cfg=json.loads((home/'config.json').read_text());backend=cfg['knowledge_backend']
        client=enterprise_client(cfg);self.assertEqual(client.request('/enterprise/v3/auth/credential')['principal'],'alice')
        store,ctx=self.registry.authenticate_token(access_token(backend))
        store.enroll_connection(ctx,'oauth-capture','synthetic','maple',['agent'])
        cfg['sessions']={'synthetic':'maple'};backend.update(connection_id='oauth-capture',capture_owner='hooks')
        (home/'config.json').write_text(json.dumps(cfg))
        from agentclient.hooks import handle
        from agentclient.capture_outbox import Outbox
        handle(home,{'hook_event_name':'Stop','event_id':'oauth-source','session_id':'synthetic','turn_id':'1',
            'timestamp':'2026-10-01T12:00:00Z','last_assistant_message':'Maple retains the CSV header for downstream reports.'})
        box=Outbox(home)
        try:box.drain(enterprise_client(cfg,timeout=5),max_events=10,max_seconds=10)
        finally:box.close()
        with store.open() as state:source=state.db.execute('SELECT id FROM enterprise_sources LIMIT 1').fetchone()['id']
        SourceIndex(store).run(max_sources=10,max_seconds=10)
        bob_home=Path(self.temp.name)/'bob';self.login(bob_home,username='bob',enrollment='bob-device')
        bob_backend=json.loads((bob_home/'config.json').read_text())['knowledge_backend']
        _,bob_ctx=self.registry.authenticate_token(access_token(bob_backend))
        self.assertEqual(store.search(bob_ctx,{'version':'enterprise-local-1','query':'Maple CSV header','project':'maple'})['results'],[])
        original=access_token(backend);self.assertEqual(renew(backend,force=True)['status'],'renewed')
        self.assertNotEqual(access_token(backend),original)
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        headers={'Authorization':'Bearer '+access_token(backend),'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}
        with TestClient(create_app(self.registry,allowed_hosts=['testserver'])) as app:
            metadata=app.get('/.well-known/oauth-protected-resource/mcp').json()
            self.assertEqual(metadata['resource'],self.fixture['resource'])
            denied=app.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers={k:v for k,v in headers.items() if k!='Authorization'})
            self.assertEqual(denied.status_code,401);self.assertIn('resource_metadata=',denied.headers['www-authenticate'])
            response=app.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
                'name':'search_memory','arguments':{'query':'Maple CSV header','project':'maple'}}})
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.json()['result'].get('isError'));self.assertIn('CSV header',response.text)
        self.assertTrue(logout(backend)['revoked'])
        with self.assertRaises(Denied):self.registry.authenticate_token(headers['Authorization'][7:])
        with store.open() as state:self.assertIsNotNone(state.db.execute('SELECT 1 FROM enterprise_sources WHERE id=?',(source,)).fetchone())

    def test_reauthentication_preserves_profile_and_capture_enrollment(self):
        home=Path(self.temp.name)/'client';self.login(home)
        cfg=json.loads((home/'config.json').read_text());cfg['projects']={'/synthetic':'maple'}
        cfg['sessions']={'synthetic-session':'maple'};(home/'config.json').write_text(json.dumps(cfg))
        outbox=home/'private-outbox-marker';outbox.write_text('synthetic pending payload')
        result=self.login(home,reauthenticate=True)
        self.assertTrue(result['reauthenticated']);current=json.loads((home/'config.json').read_text())
        self.assertEqual(current['projects'],cfg['projects']);self.assertEqual(current['sessions'],cfg['sessions'])
        self.assertEqual(outbox.read_text(),'synthetic pending payload')
        with self.assertRaises(Exception):self.login(home,reauthenticate=True,username='bob')

    def test_expired_legacy_profile_migrates_with_its_existing_connection(self):
        from agentclient.credentials import access_token
        home=Path(self.temp.name)/'legacy';home.mkdir(mode=0o700)
        token=self.store.enroll('acme','alice','synthetic-device',['read','ingest'])
        oldctx=self.store.authenticate(token)
        self.store.enroll_connection(oldctx,'legacy-capture','synthetic','maple',['agent'])
        credential=home/'credential';credential.write_text(token);credential.chmod(0o600)
        config={'projects':{'/synthetic':'maple'},'sessions':{},'knowledge_backend':{
            'mode':'enterprise_local','url':self.url,'tenant':'acme','enrollment_id':'synthetic-device',
            'credential_file':str(credential),'api_version':'cloud-local-1'}}
        path=home/'config.json';path.write_text(json.dumps(config));path.chmod(0o600)
        from agenthub.enterprise import _digest
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE enterprise_credentials SET expires_at=? WHERE digest=?',(time.time()-1,_digest(token)))
        self.login(home,reauthenticate=True)
        current=json.loads(path.read_text());_,ctx=self.registry.authenticate_token(access_token(current['knowledge_backend']))
        self.assertEqual(current['projects'],config['projects'])
        with self.store.open() as state:self.assertEqual(self.store._connection(state.db,ctx,'legacy-capture')['enrollment'],'synthetic-device')

    def test_auth_rollback_fences_provider_tokens_and_keeps_service_credentials(self):
        from agentclient.credentials import access_token
        from agenthub.oauth_provider import prepare_legacy_rollback
        home=Path(self.temp.name)/'client';self.login(home)
        token=access_token(json.loads((home/'config.json').read_text())['knowledge_backend'])
        receipt=prepare_legacy_rollback(self.store)
        self.assertGreaterEqual(receipt['provider_credentials_fenced'],1)
        with self.assertRaises(Denied):self.registry.authenticate_token(token)
        self.assertEqual(self.store.authenticate(self.tokens['alice'])['actor'],'alice')

    def test_offboarding_binding_and_membership_are_current(self):
        from agentclient.credentials import access_token
        home=Path(self.temp.name)/'client';self.login(home)
        backend=json.loads((home/'config.json').read_text())['knowledge_backend'];token=access_token(backend)
        _,ctx=self.registry.authenticate_token(token)
        user_url=self.fixture['base_url']+'/admin/realms/vaelius/users/'+self.users['alice']
        self.http.put(user_url,headers=self.admin_header,json={'enabled':False},timeout=10).raise_for_status()
        self.addCleanup(lambda:self.http.put(user_url,headers=self.admin_header,json={'enabled':True},timeout=10).raise_for_status())
        with self.assertRaises(Denied):self.registry.authenticate_token(token)
        self.http.put(user_url,headers=self.admin_header,json={'enabled':True},timeout=10).raise_for_status()
        self.store.identity_bindings.bind(self.fixture['issuer'],self.users['alice'],'acme','alice',active=False)
        with self.assertRaises(Exception):self.registry.authenticate_token(token)
        self.store.identity_bindings.bind(self.fixture['issuer'],self.users['alice'],'acme','alice',active=True)
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE enterprise_principals SET active=0 WHERE tenant='acme' AND id='alice'")
        with self.assertRaises(Denied):self.registry.authenticate_token(token)
        with self.store.open() as state,self.assertRaises(Denied):self.store.current_identity(state.db,ctx)

    def test_sql_count_is_bounded_and_delivery_rechecks_offboarding(self):
        from agentclient.credentials import access_token
        from agenthub.postgres import PostgresConnection
        home=Path(self.temp.name)/'client';self.login(home)
        backend=json.loads((home/'config.json').read_text())['knowledge_backend'];token=access_token(backend)
        self.registry.authenticate_token(token)
        original=PostgresConnection.execute;counts=[];counter=[0];times=[]
        def execute(db,*args,**kwargs):counter[0]+=1;return original(db,*args,**kwargs)
        with patch.object(PostgresConnection,'execute',execute):
            for _ in range(5):
                counter[0]=0;start=time.perf_counter();self.registry.authenticate_token(token)
                times.append((time.perf_counter()-start)*1000);counts.append(counter[0])
        self.assertEqual(len(set(counts)),1);self.assertLessEqual(max(counts),16)
        if os.environ.get('VAELIUS_AUTH_TIMING_REPORT'):
            import statistics
            from agenthub.backend_ops import build_identity
            target=Path(os.environ['VAELIUS_AUTH_TIMING_REPORT'])
            target.write_text(json.dumps({'build_ids':build_identity()['build_ids'],
                'samples':len(times),'median_authentication_ms':round(statistics.median(times),3),
                'sql_calls_min':min(counts),'sql_calls_max':max(counts),'model_calls':0,
                'scope':'warm local Keycloak introspection plus canonical identity; no retrieval/model call'},indent=2)+'\n')
            target.chmod(0o600)
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        original_search=self.store.search
        def search(ctx,value):
            result=original_search(ctx,value)
            self.store.identity_bindings.bind(self.fixture['issuer'],self.users['alice'],'acme','alice',active=False)
            return result
        # API re-authenticates after ranking, rather than trusting an earlier ctx.
        with patch.object(self.store,'search',side_effect=search),TestClient(create_app(self.registry,allowed_hosts=['testserver'])) as app:
            response=app.post('/enterprise/v3/search',headers={'Authorization':'Bearer '+token},json={
                'version':'enterprise-local-1','query':'Maple CSV header','project':'maple'})
            self.assertEqual(response.status_code,401);self.assertNotIn('results',response.json())
