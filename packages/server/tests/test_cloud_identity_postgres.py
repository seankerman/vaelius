"""Real PostgreSQL/HTTP identity and preference lifecycle rehearsal.

Creates only named disposable synthetic databases on the explicitly configured
local profile. Provider calls and real directory/customer accounts are absent.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get('CLOUD_TEST_SERVICES'), 'explicit local PostgreSQL fixture required')
class IdentityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        from agenthub.postgres import connect,migrate,TenantRegistry
        cls.services=json.loads(Path(os.environ['CLOUD_TEST_SERVICES']).read_text())
        cls.admin=cls.services['admin_dsn'];cls.created=[];cls.application_roles=[]
        (Path(os.environ['CLOUD_TEST_SERVICES']).parent/'identity-tests').mkdir(exist_ok=True,mode=0o700)
        cls.tmp=tempfile.TemporaryDirectory(prefix='identity-',dir=Path(os.environ['CLOUD_TEST_SERVICES']).parent/'identity-tests')
        cls.home=Path(cls.tmp.name)
        for kind in ('control','tenant','other_tenant'):
            name='cloud_identity_test_'+kind+'_'+uuid.uuid4().hex[:10]
            with connect(cls.admin,autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            cls.created.append(name)
            config=conninfo_to_dict(cls.admin);config['dbname']=name
            dsn=make_conninfo(**config);setattr(cls,kind+'_dsn',dsn)
            migrate(dsn,control=kind=='control')
            if kind != 'control':
                from agenthub.cloud_profile import _grant
                import secrets
                role = 'identity_app_' + uuid.uuid4().hex[:12]
                password = secrets.token_urlsafe(32)
                with connect(cls.admin,autocommit=True) as db:
                    db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(role),sql.Literal(password)))
                cls.application_roles.append(role)
                with connect(dsn) as db:
                    db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(name),sql.Identifier(role)))
                _grant(dsn,role)
                setattr(cls,kind+'_dsn',make_conninfo(dsn,user=role,password=password))
        cls.registry=TenantRegistry(cls.control_dsn,cls.home/'stores')
        cls.registry.register('orchard',cls.tenant_dsn)
        cls.store=cls.registry.resolve('orchard');cls.store.create_organization('orchard')
        cls.registry.register('harbor',cls.other_tenant_dsn)
        cls.other_store=cls.registry.resolve('harbor');cls.other_store.create_organization('harbor')

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql
        from agenthub.postgres import connect
        for name in reversed(cls.created):
            with connect(cls.admin,autocommit=True) as db:
                db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        with connect(cls.admin,autocommit=True) as db:
            for role in cls.application_roles:
                db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        cls.tmp.cleanup()

    def setUp(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        self.prefix=uuid.uuid4().hex[:8];self.alice='alice_'+self.prefix;self.bob='bob_'+self.prefix
        self.admin_user='admin_'+self.prefix;self.service='service_'+self.prefix
        self.modern='modern_'+self.prefix;self.legacy='legacy_'+self.prefix
        for user in (self.alice,self.bob,self.admin_user,self.service):
            self.store.create_principal('orchard',user,settings_admin=user==self.admin_user)
        for project in (self.modern,self.legacy):
            self.store.create_project('orchard',project)
            for user in (self.alice,self.bob):self.store.set_membership('orchard',project,user,True)
        actions=['ingest','read','source_read','correct','withdraw','policy','settings']
        self.tokens={user:self.store.enroll('orchard',user,user,actions if user!=self.admin_user else ['read','settings'])
            for user in (self.alice,self.bob,self.admin_user)}
        self.ctx={user:self.store.authenticate(token) for user,token in self.tokens.items()}
        self.client=TestClient(create_app(self.registry,allowed_hosts=['testserver']))

    def post(self,user,path,value):
        return self.client.post('/enterprise/v3/'+path,json=value,
            headers={'Authorization':'Bearer '+self.tokens[user]})

    def source(self,user,text,*,visibility='private',session='prior-session'):
        from agentclient.enterprise_contract import VERSION
        value={'version':VERSION,'external_id':uuid.uuid4().hex,'project':self.modern,
            'session':session,'turn':uuid.uuid4().hex,'kind':'UserPromptSubmit','body':text,
            'visibility':visibility,'occurred_at':time.time()}
        return self.store.ingest(self.ctx[user],value)['source_id']

    def admit(self,user,value='pytest',*,scope='user',project=None,source=None):
        text=('For the '+project+' project, always use '+value+'.' if project
              else 'Please remember: I prefer '+value+' for my projects.')
        source=source or self.source(user,text)
        candidate={'key':'test_runner','value':value,'scope':scope,'quote':text}
        if project:candidate['project']=project
        response=self.post(user,'preferences/curate',{'source_id':source,'candidate':candidate})
        self.assertEqual(response.status_code,200,response.json())
        return response.json(),source

    def test_rotation_expiry_and_inflight_current_identity(self):
        from agenthub.enterprise import Denied,_digest
        oldctx=self.ctx[self.alice];old=self.tokens[self.alice]
        rotated=self.post(self.alice,'auth/rotate',{}).json()
        self.assertIsInstance(rotated,str)
        with self.assertRaises(Denied):self.store.authenticate(old)
        with self.store.open() as state,self.assertRaises(Denied):self.store.current_identity(state.db,oldctx)
        self.assertEqual(self.store.authenticate(rotated)['actor'],self.alice)
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE enterprise_credentials SET expires_at=%s WHERE digest=%s',(time.time()-1,_digest(rotated)))
        with self.assertRaises(Denied):self.store.authenticate(rotated)

    def test_delegation_narrowing_and_settings_no_private_access(self):
        from agenthub.enterprise import Denied
        result,source=self.admit(self.alice)
        self.assertEqual(self.client.get('/enterprise/v1/documents/'+result['document_id'],
            headers={'Authorization':'Bearer '+self.tokens[self.admin_user]}).status_code,404)
        with self.assertRaises(Denied):self.store.enroll('orchard',self.service,'service-'+self.prefix,['read'],acting_for=self.alice)
        self.store.set_delegation('orchard',self.service,self.alice,['read'],[self.modern],True)
        token=self.store.enroll('orchard',self.service,'service-'+self.prefix,['read'],acting_for=self.alice)
        ctx=self.store.authenticate(token)
        self.assertEqual(self.store.preferences(ctx,project=self.modern)['values'],{'test_runner':'pytest'})
        with self.assertRaises(Denied):self.store.preferences(ctx,project=self.legacy)
        self.store.set_delegation('orchard',self.service,self.alice,['read'],[self.modern],False)
        with self.assertRaises(Denied):self.store.authenticate(token)

    def test_opposing_private_defaults_scope_correction_withdrawal(self):
        alice,_=self.admit(self.alice);bob,_=self.admit(self.bob,'unittest')
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern}).json()['values'],{'test_runner':'pytest'})
        self.assertEqual(self.post(self.bob,'preferences',{'project':self.modern}).json()['values'],{'test_runner':'unittest'})
        self.admit(self.alice,'unittest',scope='project',project=self.legacy)
        only_applicable=self.post(self.alice,'preferences',{'project':self.modern}).json()['preferences']
        self.assertTrue(all(row['scope']=='user' for row in only_applicable))
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.legacy}).json()['values'],{'test_runner':'unittest'})
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.legacy,'explicit':{'test_runner':'nose'}}).json()['values'],{'test_runner':'nose'})
        corrected,_=self.admit(self.alice,'jest')
        self.assertEqual(corrected['document_id'],alice['document_id'])
        with self.store.open() as state:
            rows=state.db.execute('SELECT * FROM cloud_preferences WHERE document_id=%s ORDER BY valid_from',(alice['document_id'],)).fetchall()
            self.assertEqual(len(rows),2);self.assertIsNotNone(rows[0]['valid_until']);self.assertIsNone(rows[1]['valid_until'])
        self.assertEqual(self.post(self.alice,'preferences/withdraw',{'document_id':alice['document_id']}).status_code,200)
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern}).json()['values'],{})
        self.assertEqual(self.post(self.bob,'preferences',{'project':self.modern}).json()['values'],{'test_runner':'unittest'})

    def test_shared_original_private_derivative_and_no_speaker_spoofing(self):
        source=self.source(self.alice,'Please remember: I prefer pytest for my projects.',visibility='team')
        pref,_=self.admit(self.alice,source=source)
        self.assertEqual(self.client.get('/enterprise/v1/documents/'+pref['document_id'],
            headers={'Authorization':'Bearer '+self.tokens[self.bob]}).status_code,404)
        with self.store.open() as state:
            row=state.db.execute('SELECT * FROM enterprise_sources WHERE id=%s',(source,)).fetchone()
            self.assertEqual(row['visibility'],'team');self.assertTrue(self.store._visible_source(state.db,self.ctx[self.bob],row))
        response=self.post(self.bob,'preferences/curate',{'source_id':source,'candidate':{
            'key':'test_runner','value':'pytest','scope':'user','quote':'Please remember: I prefer pytest for my projects.'}})
        self.assertEqual(response.status_code,404)

    def test_pending_observer_preference_never_delivers_shared_derived_claim(self):
        fixture=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_preference_race_v1.json').read_text())
        source=self.source(self.alice,fixture['source'],visibility=fixture['visibility'])
        selected=self.store.accept_reviewed_note(self.ctx[self.alice],source,'User preference',fixture['canonical_claim'])
        with self.store.open() as state,state.db:
            marked=self.store.mark_observed_preference_candidates(state.db,self.ctx[self.alice],
                [source],[selected['document_id']])
            self.assertEqual(marked,1)
        for person in (self.alice,self.bob):
            response=self.client.get('/enterprise/v1/documents/'+selected['document_id'],
                headers={'Authorization':'Bearer '+self.tokens[person]})
            self.assertEqual(response.status_code,404)
        result=self.store.curate_observed_preferences(self.ctx[self.alice],source,[selected['document_id']])
        self.assertEqual(len(result),1)
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern}).json()['values'],{'test_runner':'pytest'})
        self.assertEqual(self.post(self.bob,'preferences',{'project':self.modern}).json()['values'],{})

    def test_actual_worker_promotes_ingest_only_captured_preference_privately(self):
        self.store.curation_enabled=True  # This test explicitly enables preference extraction.
        from agenthub.backend_worker import Worker
        from agentclient.enterprise_capture import normalize_capture
        from agentclient.enterprise_contract import VERSION
        capture=self.store.enroll('orchard',self.alice,'capture-'+self.prefix,['ingest'])
        capture_ctx=self.store.authenticate(capture);connection='capture-conn-'+self.prefix
        self.store.enroll_connection(capture_ctx,connection,'agent',self.modern,['agent'],visibility='team',
            reader_ids=[self.alice,self.bob])
        for kind in ('UserPromptSubmit','Stop'):
            event=normalize_capture({'hook_event_name':kind,'event_id':kind+self.prefix,
                'session_id':'ordinary-'+self.prefix,'turn_id':'1',
                'prompt':'Please remember: I prefer pytest for my projects.',
                'last_assistant_message':'I will remember your preference for pytest.'},self.modern,connection)
            self.store.ingest_general(capture_ctx,event)
        config={'paused':False,'knowledge_backend':{'mode':'enterprise_local'},
            'accounting_home':str(self.home/'provider-free-accounting'),
            'observer':{'enabled':True,'model':'fixture','min_interval_seconds':0},
            'episode_curation':{'enabled':True,'policy':'durable_memory','settle_seconds':0,
                'generation_id':'pref-worker-'+self.prefix}}
        def runner(home,cfg,instruction,payload,schema,**kwargs):
            if cfg['_purpose']=='episode_resolve':
                return {'candidate_key':payload['candidate']['candidate_key'],'operation':'CREATE',
                    'target_artifact_id':'','reason':'new_claim'},{}
            event=next(e for e in payload['episode']['events'] if e['kind']=='UserPromptSubmit')
            return {'records':[{'title':'User preference for pytest','text':'The user prefers pytest for their projects.',
                'subject':'pytest','facets':['decision'],'actors':['user'],'artifact':None,'rationale':None,
                'state':'reported','occurred_date':'','event_id':event['event_id'],
                'evidence_span_ids':[event['spans'][0]['span_id']]}],
                'episode_summary':{'intent':None,'open_work':[]}},{},'00000000-0000-4000-8000-000000000001'
        result=Worker(self.store,config,runner=runner,live=False).run(max_jobs=1,max_calls=4,max_seconds=15)
        self.assertEqual(result['completed'],1,result)
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern}).json()['values'],{'test_runner':'pytest'})
        self.assertEqual(self.post(self.bob,'preferences',{'project':self.modern}).json()['values'],{})
        with self.store.open() as state:
            pending=state.db.execute("SELECT count(*) FROM cloud_preference_candidates WHERE status='pending'").fetchone()[0]
            self.assertEqual(pending,0)
            promoted=state.db.execute("SELECT document_id FROM cloud_preference_candidates WHERE owner=%s AND status='promoted'",(self.alice,)).fetchall()
            self.assertEqual(len(promoted),1)
        for user in (self.alice,self.bob):
            response=self.client.get('/enterprise/v1/documents/'+promoted[0]['document_id'],
                headers={'Authorization':'Bearer '+self.tokens[user]})
            self.assertEqual(response.status_code,404)
        query=self.client.post('/enterprise/v3/search',json={'version':VERSION,'query':'pytest','project':self.modern},
            headers={'Authorization':'Bearer '+self.tokens[self.bob]})
        self.assertEqual(query.status_code,200,query.json());self.assertEqual(query.json()['results'],[])
        with self.store.open() as state:
            source=state.db.execute('''SELECT p.source_id FROM cloud_preferences p WHERE p.owner=%s
                AND p.valid_until IS NULL''',(self.alice,)).fetchone()['source_id']
            job=state.db.execute('SELECT id FROM backend_jobs').fetchone()['id']
        response=self.client.post('/enterprise/v1/lifecycle',json={'version':VERSION,'target_id':source,
            'expected_revision':'1','idempotency_key':'delete-pref-'+self.prefix,'reason':'synthetic deletion',
            'operation':'delete'},headers={'Authorization':'Bearer '+self.tokens[self.alice]})
        self.assertEqual(response.status_code,200,response.json())
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern}).json()['values'],{})
        with self.store.open() as state:
            saved=state.db.execute('SELECT result FROM backend_provider_returns WHERE job_id=%s',(job,)).fetchall()
            # Physical removal and an empty retained accounting shell both purge
            # the private provider return; neither may retain replayable content.
            self.assertTrue(all(r['result']=='{}' for r in saved))
            markers=state.db.execute('SELECT candidate_json FROM cloud_preference_candidates WHERE source_id=%s',(source,)).fetchall()
            self.assertTrue(all(r['candidate_json']=='[]' for r in markers))
        with self.assertRaises(ValueError):Worker(self.store,config,runner=runner,live=False).recover(job,retain_validated=True)

    def test_same_session_suppression_then_compaction_restores(self):
        from agentclient.enterprise_contract import VERSION
        session='receiver-'+self.prefix
        source=self.source(self.alice,'Please remember: I prefer pytest for my projects.',session=session)
        self.admit(self.alice,source=source)
        args={'project':self.modern,'session':session,'mode':'automatic'}
        self.assertEqual(self.post(self.alice,'preferences',args).json()['values'],{})
        event={'session_id':session,'hook_event_name':'PostCompact','turn_id':'boundary-'+self.prefix,'trigger':'auto'}
        response=self.client.post('/enterprise/v1/boundary',json={'version':VERSION,'event':event},
            headers={'Authorization':'Bearer '+self.tokens[self.alice]})
        self.assertEqual(response.status_code,200,response.json())
        self.assertEqual(self.post(self.alice,'preferences',args).json()['values'],{'test_runner':'pytest'})

    def test_scim_group_removal_idempotency_conflicts_and_ambiguous_order(self):
        from agenthub.enterprise import Conflict,Denied
        ctx=self.ctx[self.admin_user]
        external='directory-'+self.prefix;group='group-'+self.prefix
        self.store.bind_directory_user(ctx,external,self.bob)
        self.store.bind_directory_group(ctx,group,self.modern)
        self.store.set_membership('orchard',self.modern,self.bob,False)
        self.store.apply_directory_event(ctx,resource='Users',value={'id':external,'active':True},sequence=1,key='u-'+self.prefix)
        event={'resource':'Groups','value':{'id':group,'members':[{'value':external}]},'sequence':1,'key':'g-'+self.prefix}
        self.assertEqual(self.store.apply_directory_event(ctx,**event)['disposition'],'applied')
        with self.store.open() as state:self.assertTrue(self.store._project_member(state.db,self.ctx[self.bob],self.modern))
        self.assertEqual(self.store.apply_directory_event(ctx,**event)['disposition'],'duplicate')
        with self.assertRaises(Conflict):self.store.apply_directory_event(ctx,**dict(event,value={'id':group,'members':[]}))
        self.store.apply_directory_event(ctx,resource='Groups',value={'id':group,'members':[]},sequence=2,key='gr-'+self.prefix)
        with self.store.open() as state:self.assertFalse(self.store._project_member(state.db,self.ctx[self.bob],self.modern))
        held=self.store.apply_directory_event(ctx,resource='Users',value={'id':external,'active':True},sequence=1,key='out-'+self.prefix)
        self.assertTrue(held['requires_reconcile'])
        with self.assertRaises(Denied):self.store.authenticate(self.tokens[self.bob])
        self.store.apply_directory_event(ctx,resource='Users',value={'id':external,'active':True},sequence=3,key='rec-'+self.prefix,reconcile=True)
        self.assertEqual(self.store.authenticate(self.tokens[self.bob])['actor'],self.bob)

    def test_scim_http_core_resource_replacement_revocation_and_unsupported_patch(self):
        from agenthub.enterprise import Denied
        external='http-directory-'+self.prefix;ctx=self.ctx[self.admin_user]
        self.store.bind_directory_user(ctx,external,self.bob)
        path='/enterprise/v3/scim/Users/'+external
        headers={'Authorization':'Bearer '+self.tokens[self.admin_user],
            'X-Directory-Sequence':'1','X-Idempotency-Key':'http-user-'+self.prefix}
        value={'schemas':['urn:ietf:params:scim:schemas:core:2.0:User'],
            'id':external,'userName':'synthetic@example.invalid','active':True}
        created=self.client.post(path,json=value,headers=headers)
        self.assertEqual(created.status_code,200,created.json())
        get=self.client.get(path,headers=headers)
        self.assertEqual(get.status_code,200,get.json());self.assertTrue(get.json()['active'])
        patched=self.client.patch(path,json={'Operations':[{'op':'replace','path':'active','value':False}]},headers=headers)
        self.assertEqual(patched.status_code,400,patched.json())
        replaced=self.client.put(path,json=dict(value,active=False),headers=dict(headers,
            **{'X-Directory-Sequence':'2','X-Idempotency-Key':'http-disable-'+self.prefix}))
        self.assertEqual(replaced.status_code,200,replaced.json())
        with self.assertRaises(Denied):self.store.authenticate(self.tokens[self.bob])
        removed=self.client.delete(path,headers=dict(headers,
            **{'X-Directory-Sequence':'3','X-Idempotency-Key':'http-delete-'+self.prefix}))
        self.assertEqual(removed.status_code,200,removed.json())

    def test_acl_staleness_and_cached_ctx_denial(self):
        result,source=self.admit(self.alice)
        self.store.set_source_acl_freshness(self.ctx[self.alice],source,valid_seconds=0,state='stale')
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern}).json()['values'],{})
        self.assertEqual(self.client.get('/enterprise/v1/documents/'+result['document_id'],
            headers={'Authorization':'Bearer '+self.tokens[self.alice]}).status_code,404)

    def test_organization_requirement_overrides_private_and_explicit_instruction(self):
        from agenthub.enterprise import Denied
        self.admit(self.alice)
        with self.assertRaises(Denied):self.store.set_required_preference(self.ctx[self.alice],'test_runner','unittest')
        self.store.set_required_preference(self.ctx[self.admin_user],'test_runner','unittest',project=self.modern)
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.modern,
            'explicit':{'test_runner':'nose'},'required':{'test_runner':'pytest'}}).json()['values'],{'test_runner':'unittest'})
        self.assertEqual(self.post(self.alice,'preferences',{'project':self.legacy}).json()['values'],{'test_runner':'pytest'})
        self.store.set_required_preference(self.ctx[self.admin_user],'test_runner',None,project=self.modern)

    def test_control_binding_disable_sso_and_issuer_subject_namespaces(self):
        from agenthub.cloud_identity import IdentityError
        from agenthub.enterprise import Denied
        bindings=self.store.identity_bindings
        identity={'issuer':'http://127.0.0.1:12345/realms/native','subject':'same-'+self.prefix,'provider':'native'}
        bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice)
        token=self.store.enroll('orchard',self.alice,'login-'+self.prefix,['read'],external_identity=identity)
        self.assertEqual(self.store.authenticate(token)['actor'],self.alice)
        bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice,active=False)
        with self.assertRaises(Denied):self.store.authenticate(token)
        bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice,
            federation_required=True,allowed_providers=['company','native'])
        with self.assertRaises(IdentityError):bindings.resolve(identity,'orchard')
        self.assertEqual(bindings.resolve(dict(identity,provider='company'),'orchard')['principal'],self.alice)
        other=dict(identity,issuer='http://127.0.0.1:12345/realms/company')
        bindings.bind(other['issuer'],other['subject'],'orchard',self.bob)
        self.assertEqual(bindings.resolve(other,'orchard')['principal'],self.bob)

    def test_org_federation_policy_rejects_native_binding_and_existing_token(self):
        from agenthub.enterprise import Denied
        from agenthub.cloud_identity import IdentityError
        bindings=self.store.identity_bindings
        identity={'issuer':'http://127.0.0.1:12345/realms/native','subject':'org-'+self.prefix,'provider':'native'}
        bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice,allowed_providers=['native','company'])
        token=self.store.enroll('orchard',self.alice,'native-policy-'+self.prefix,['read'],external_identity=identity)
        ctx=self.store.authenticate(token)
        bindings.set_tenant_policy('orchard',federation_required=True,allowed_providers=['company'])
        try:
            with self.assertRaises(IdentityError):bindings.resolve(identity,'orchard')
            with self.assertRaises(Denied):self.store.authenticate(token)
            with self.store.open() as state,self.assertRaises(Denied):self.store.current_identity(state.db,ctx)
            self.assertEqual(bindings.resolve(dict(identity,provider='company'),'orchard')['principal'],self.alice)
        finally:bindings.set_tenant_policy('orchard',allowed_providers=['native','company'])

    def test_two_tenant_binding_routes_and_disabled_tenant_inflight_denial(self):
        from agenthub.enterprise import Denied
        from agenthub.cloud_identity import IdentityError
        self.other_store.create_principal('harbor',self.alice)
        identity={'issuer':'http://127.0.0.1:12345/realms/shared','subject':'same-'+self.prefix,'provider':'native'}
        self.store.identity_bindings.bind(identity['issuer'],identity['subject'],'orchard',self.bob)
        with self.assertRaises(IdentityError):self.store.identity_bindings.resolve(identity,'harbor')
        self.other_store.identity_bindings.bind(identity['issuer'],identity['subject'],'harbor',self.alice)
        token=self.other_store.enroll('harbor',self.alice,'tenant-'+self.prefix,['read'],external_identity=identity)
        routed=self.registry.store_for_token(token)
        self.assertEqual(routed.tenant_id,'harbor')
        ctx=routed.authenticate(token);self.assertEqual(ctx['actor'],self.alice)
        self.registry.set_active('harbor',False)
        try:
            with self.assertRaises(Denied):routed.authenticate(token)
            with routed.open() as state,self.assertRaises(Denied):routed.current_identity(state.db,ctx)
        finally:self.registry.set_active('harbor',True)

    def test_refresh_routes_through_registry_and_rechecks_binding_and_tenant(self):
        import secrets
        identity={'issuer':'http://127.0.0.1:12345/realms/refresh','subject':'refresh-'+self.prefix,'provider':'native'}
        self.store.identity_bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice)
        issued=self.store.enroll_with_refresh('orchard',self.alice,'refresh-'+self.prefix,['read'],external_identity=identity)
        def renew(token):
            access,refresh=secrets.token_urlsafe(48),secrets.token_urlsafe(48)
            response=self.client.post('/enterprise/v3/auth/renew',json={'refresh_token':token,
                'replacement_access_token':access,'replacement_refresh_token':refresh})
            return response,access,refresh
        response,access,refresh=renew(issued['refresh_token'])
        self.assertEqual(response.status_code,200,response.json())
        # Both replacements are routed to the tenant through the control plane.
        self.assertEqual(self.registry.store_for_token(access).tenant_id,'orchard')
        self.assertEqual(self.registry.store_for_token(refresh).tenant_id,'orchard')
        self.assertEqual(self.client.get('/enterprise/v1/status',headers={'Authorization':'Bearer '+access}).status_code,200)
        self.store.identity_bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice,active=False)
        self.assertEqual(renew(refresh)[0].status_code,404)
        self.store.identity_bindings.bind(identity['issuer'],identity['subject'],'orchard',self.alice)
        self.registry.set_active('orchard',False)
        try:self.assertEqual(renew(refresh)[0].status_code,404)
        finally:self.registry.set_active('orchard',True)
        response,access,_=renew(refresh)
        self.assertEqual(response.status_code,200,response.json())
        revoked=self.client.post('/enterprise/v3/auth/revoke',json={},headers={'Authorization':'Bearer '+access})
        self.assertEqual((revoked.status_code,revoked.json()),(200,{'revoked':True}))
        self.assertEqual(self.client.get('/enterprise/v1/status',headers={'Authorization':'Bearer '+access}).status_code,404)

    @unittest.skipUnless(os.environ.get('CLOUD_BROKER_SETTINGS'),'explicit synthetic local Keycloak fixture required')
    def test_actual_keycloak_native_federation_pkce_http_enrollment_and_replay(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        from agenthub.broker_config import broker_from_settings,login_local_fixture
        from agenthub.cloud_identity import _sha
        from agenthub.enterprise import Denied
        settings=json.loads(Path(os.environ['CLOUD_BROKER_SETTINGS']).read_text())
        broker=broker_from_settings(settings)
        client=TestClient(create_app(self.registry,allowed_hosts=['testserver'],brokers={'fixture':broker}))
        for federated,principal in ((False,self.alice),(True,self.bob)):
            bootstrap=login_local_fixture(broker,settings,federated=federated)
            identity=broker.exchange(bootstrap['code'],bootstrap['request'],callback_uri=bootstrap['callback_uri'])
            self.assertEqual(identity['provider'],'company' if federated else 'native')
            self.store.identity_bindings.bind(identity['issuer'],identity['subject'],'orchard',principal,
                federation_required=federated,allowed_providers=['company'] if federated else ['native'])
            start=client.post('/enterprise/v3/auth/begin',json={'tenant':'orchard','broker':'fixture'})
            self.assertEqual(start.status_code,200,start.json())
            with self.store.open() as state:
                saved=state.db.execute('SELECT request_json FROM cloud_login_states WHERE digest=%s',(_sha(start.json()['state']),)).fetchone()
            callback=login_local_fixture(broker,settings,federated=federated,request=json.loads(saved['request_json']))
            data={'tenant':'orchard','broker':'fixture','state':callback['state'],'code':callback['code'],
                'callback_uri':callback['callback_uri'],'enrollment':'broker-'+principal}
            finish=client.post('/enterprise/v3/auth/complete',json=data)
            self.assertEqual(finish.status_code,200,finish.json())
            token=finish.json()['credential']
            self.assertEqual(finish.headers['cache-control'],'no-store')
            self.assertEqual(self.store.authenticate(token)['actor'],principal)
            with self.assertRaises(Denied):self.store.authenticate(finish.json()['refresh_token'])
            self.assertEqual(client.get('/enterprise/v1/status',headers={'Authorization':'Bearer '+token}).status_code,200)
            self.assertEqual(client.post('/enterprise/v3/auth/complete',json=data).status_code,404)

    @unittest.skipUnless(os.environ.get('CLOUD_BROKER_SETTINGS'),'explicit synthetic local Keycloak fixture required')
    def test_actual_client_enrollment_subprocess_callback_native_and_federated(self):
        import socket
        import subprocess
        import sys
        import threading
        from urllib.parse import parse_qs,urlsplit,urlencode
        from urllib.request import urlopen
        import uvicorn
        from agenthub.cloud_api import create_app
        from agenthub.broker_config import broker_from_settings,login_local_fixture
        from agentclient.transport import EnterpriseLocal
        settings=json.loads(Path(os.environ['CLOUD_BROKER_SETTINGS']).read_text())
        broker=broker_from_settings(settings)
        app=create_app(self.registry,brokers={'fixture':broker})
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(32)
        url='http://127.0.0.1:'+str(sock.getsockname()[1])
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.02)
            self.assertTrue(server.started)
            for federated,principal in ((False,self.alice),(True,self.bob)):
                bootstrap=login_local_fixture(broker,settings,federated=federated)
                identity=broker.exchange(bootstrap['code'],bootstrap['request'],callback_uri=bootstrap['callback_uri'])
                self.store.identity_bindings.bind(identity['issuer'],identity['subject'],'orchard',principal,
                    federation_required=federated,allowed_providers=['company'] if federated else ['native'])
                profile=self.home/('client-enroll-'+principal)
                proc=subprocess.Popen([sys.executable,'-m','agentclient.cloud_enroll','--home',str(profile),
                    '--url',url,'--tenant','orchard','--broker','fixture','--enrollment','client-'+principal,
                    '--callback-uri',settings['redirect_uri'],'--timeout','30','--no-browser','--credential-store','file'],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    line=proc.stdout.readline();self.assertTrue(line)
                    start=json.loads(line);auth_url=start['authorization_url']
                    args=parse_qs(urlsplit(auth_url).query)
                    callback=login_local_fixture(broker,settings,federated=federated,
                        request={'url':auth_url,'state':args['state'][0]})
                    with urlopen(callback['callback_uri']+'?'+urlencode({'state':callback['state'],'code':callback['code']}),timeout=5) as response:
                        self.assertEqual(response.status,200)
                    output,error=proc.communicate(timeout=20)
                    self.assertEqual(proc.returncode,0,error)
                    result=json.loads(output);self.assertEqual(result['client_model_calls'],0)
                    config=json.loads((profile/'config.json').read_text())
                    self.assertEqual(config['projects'],{});self.assertFalse((profile/'knowledge.sqlite').exists())
                    token=(profile/'credential').read_text().strip()
                    refresh=(profile/'credential.refresh').read_text().strip()
                    self.assertNotIn(token,line+output+error);self.assertNotIn(refresh,line+output+error)
                    self.assertTrue(config['knowledge_backend']['credential_renewal'])
                    self.assertEqual(self.store.authenticate(token)['actor'],principal)
                    status=EnterpriseLocal(url,profile/'credential').request('/enterprise/v1/status')
                    self.assertIsInstance(status,dict)
                finally:
                    if proc.poll() is None:proc.kill();proc.communicate(timeout=5)
        finally:
            server.should_exit=True;thread.join(timeout=10);sock.close()


if __name__=='__main__':unittest.main()
