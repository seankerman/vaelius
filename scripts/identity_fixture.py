#!/usr/bin/env python3
"""Explicit disposable Keycloak reference. All account/secret state stays private."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from dev_services import free_port, private_json, run

IMAGE='quay.io/keycloak/keycloak:26.7.4'


def start(state,engine,resource):
    import requests
    state.mkdir(parents=True,exist_ok=True,mode=0o700);state.chmod(0o700)
    receipt=state/'identity.json'
    if receipt.exists():
        previous=json.loads(receipt.read_text())
        if previous['resource']!=resource:raise ValueError('fixture_resource_changed')
        actual=json.loads(run(engine,'inspect',previous['container']))[0]
        if actual['Config']['Labels'].get('vaelius.identity-fixture')!=previous['namespace']:raise ValueError('fixture_ownership')
        run(engine,'start',previous['container']);return {'fixture':str(receipt),'resumed':True}
    namespace='vaelius-idp-'+hashlib.sha256(str(state).encode()).hexdigest()[:12]
    port=free_port();callback='http://127.0.0.1:'+str(free_port())+'/callback'
    base='http://127.0.0.1:'+str(port);issuer=base+'/realms/vaelius'
    admin=secrets.token_urlsafe(32);password=secrets.token_urlsafe(32);secret=secrets.token_urlsafe(32)
    scopes=[{'name':'vaelius:'+action,'protocol':'openid-connect','attributes':{'include.in.token.scope':'true'}} for action in
        ('read','source_read','ingest','feedback')]
    client={'clientId':'vaelius-plugin','publicClient':True,'standardFlowEnabled':True,
        'directAccessGrantsEnabled':False,'redirectUris':[callback],
        'attributes':{'pkce.code.challenge.method':'S256'},'defaultClientScopes':['profile','email'],
        'optionalClientScopes':[s['name'] for s in scopes],
        'protocolMappers':[{'name':'vaelius-audience','protocol':'openid-connect',
            'protocolMapper':'oidc-audience-mapper','config':{'included.custom.audience':resource,
            'access.token.claim':'true','id.token.claim':'false','introspection.token.claim':'true'}},
            {'name':'introspection-audience','protocol':'openid-connect','protocolMapper':'oidc-audience-mapper',
             'config':{'included.client.audience':'vaelius-api','access.token.claim':'true','id.token.claim':'false',
                       'introspection.token.claim':'true'}},
            {'name':'subject','protocol':'openid-connect','protocolMapper':'oidc-usermodel-property-mapper',
             'config':{'user.attribute':'id','claim.name':'sub','jsonType.label':'String','access.token.claim':'true',
                       'id.token.claim':'true','introspection.token.claim':'true'}}]}
    realm={'realm':'vaelius','enabled':True,'sslRequired':'none','registrationAllowed':False,
        'revokeRefreshToken':True,'refreshTokenMaxReuse':0,'accessTokenLifespan':60,
        'ssoSessionIdleTimeout':1800,'ssoSessionMaxLifespan':3600,
        'clientScopes':scopes,'clients':[client,{'clientId':'vaelius-api','publicClient':False,
            'secret':secret,'standardFlowEnabled':False,'serviceAccountsEnabled':True}],
        'users':[{'username':name,'enabled':True,'emailVerified':True,
            'email':name+'@example.invalid','firstName':name,'lastName':'Synthetic',
            'credentials':[{'type':'password','value':password,'temporary':False}]} for name in ('alice','bob')]}
    imports=state/'import';imports.mkdir(mode=0o700)
    private_json(imports/'vaelius-realm.json',realm)
    (state/'introspection.secret').write_text(secret);(state/'introspection.secret').chmod(0o600)
    envfile=state/'admin.env';envfile.write_text('KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD='+admin+'\n');envfile.chmod(0o600)
    container=namespace+'-keycloak'
    # Root is only for reading the owner-only imported fixture in this disposable
    # container. Production instructions require a service UID and mounted secrets.
    run(engine,'run','-d','--name',container,'--label','vaelius.identity-fixture='+namespace,
        '--user','0','--env-file',str(envfile),'-p','127.0.0.1:'+str(port)+':8080',
        '-v',str(imports)+':/opt/keycloak/data/import:ro',IMAGE,
        'start-dev','--import-realm','--hostname',base)
    data={'disposable':True,'namespace':namespace,'container':container,'engine':engine,
        'base_url':base,'issuer':issuer,'callback_uri':callback,'resource':resource,
        'client_id':'vaelius-plugin','password':password,'admin_password':admin,
        'provider':{'issuer':issuer,'tenant':'acme','client_ids':['vaelius-plugin'],
            'introspection_client_id':'vaelius-api','client_secret_file':str(state/'introspection.secret'),
            'actions':['read','source_read','ingest','feedback']}}
    private_json(receipt,data)
    deadline=time.monotonic()+90
    while True:
        try:
            if requests.get(issuer+'/.well-known/openid-configuration',timeout=2).status_code==200:break
        except requests.RequestException:pass
        if time.monotonic()>=deadline:raise RuntimeError('identity_fixture_start_timeout')
        time.sleep(.5)
    return {'fixture':str(receipt),'synthetic_only':True,'model_calls':0}


def stop(state,destroy=False):
    data=json.loads((state/'identity.json').read_text());engine=data['engine']
    actual=json.loads(run(engine,'inspect',data['container']))[0]
    if actual['Config']['Labels'].get('vaelius.identity-fixture')!=data['namespace']:raise ValueError('fixture_ownership')
    run(engine,'rm','-f','-v',data['container']) if destroy else run(engine,'stop',data['container'])
    return {'stopped':data['container'],'destroyed':destroy}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','destroy']);parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--resource');parser.add_argument('--engine',default=shutil.which('podman') or shutil.which('docker'))
    args=parser.parse_args();os.umask(0o077)
    if args.action=='start' and not args.resource:parser.error('--resource is required')
    print(json.dumps(start(args.state.resolve(),args.engine,args.resource) if args.action=='start'
        else stop(args.state.resolve(),args.action=='destroy')))
