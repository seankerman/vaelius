"""Refresh rotation, crash recovery, keychain storage and logout. No real server."""
import io, json, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

SCOPE={'tenant':'acme','principal':'alice','actor':'alice','enrollment':'device','actions':['read']}


def denied():return HTTPError('synthetic',404,'not_found',{},None)


class Service:
    """Synthetic service rotation semantics, mirroring the server contract.

    A spent refresh token presented with its exact recorded successors (a lost
    reply) is answered again while the successor is unused; any other use of a
    spent refresh token revokes the family.
    """
    def __init__(self,*,access='old-access',refresh='old-refresh',expires_in=10):
        self.access={access:time.time()+expires_in};self.refresh={refresh:None}
        self.revoked=False;self.rotations=0;self.lose_reply=False;self.calls=[]
    def __call__(self,backend,token,route,data=None):
        self.calls.append((route,token,data))
        if route.endswith('auth/credential'):
            if self.revoked or self.access.get(token,0)<=time.time():raise denied()
            return dict(SCOPE,expires_at=self.access[token])
        if route.endswith('auth/revoke'):
            if token is not None:raise AssertionError('revocation must not need a Bearer token')
            self.revoked=True;return {'revoked':True}
        if not route.endswith('auth/renew') or token is not None:raise AssertionError('refresh is body-only')
        presented=data['refresh_token'];pair=(data['replacement_access_token'],data['replacement_refresh_token'])
        if self.revoked or presented not in self.refresh:raise denied()
        if self.refresh[presented] is not None:
            if self.refresh[presented]!=pair or self.refresh.get(pair[1]) is not None:
                self.revoked=True;raise denied()
        else:
            self.refresh[presented]=pair;self.refresh[pair[1]]=None
            self.access={pair[0]:time.time()+3600};self.rotations+=1
        if self.lose_reply:self.lose_reply=False;raise TimeoutError()
        return dict(SCOPE,expires_at=self.access.get(pair[0],0),refresh_expires_at=time.time()+86400,
            session_expires_at=time.time()+86400*90)
    def current(self):
        return next(iter(self.access)),next(r for r,successor in self.refresh.items() if successor is None)


class FakeKeyring:
    def __init__(self):self.items={}
    def get_password(self,service,name):return self.items.get((service,name))
    def set_password(self,service,name,value):self.items[(service,name)]=value
    def delete_password(self,service,name):del self.items[(service,name)]


def file_profile(td,*,access='old-access',refresh='old-refresh'):
    path=Path(td)/'credential';path.write_text(access+'\n');path.chmod(0o600)
    if refresh is not None:
        extra=Path(td)/'credential.refresh';extra.write_text(refresh+'\n');extra.chmod(0o600)
    return {'url':'http://127.0.0.1:1','credential_file':str(path)},path


class CredentialRenewal(unittest.TestCase):
    def test_rotation_sends_refresh_only_in_body_and_replaces_both_tokens(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td);service=Service()
            with patch('agentclient.credentials._request',side_effect=service):
                self.assertEqual(renew(cfg)['status'],'renewed')
            access,refresh=service.current()
            self.assertEqual(path.read_text().strip(),access)
            self.assertEqual((Path(td)/'credential.refresh').read_text().strip(),refresh)
            for name in ('credential','credential.refresh','credential.renewal.json'):
                self.assertEqual((Path(td)/name).stat().st_mode&0o777,0o600)
            self.assertFalse((Path(td)/'credential.pending.json').exists())
            renewals=[call for call in service.calls if call[0].endswith('auth/renew')]
            self.assertEqual(len(renewals),1);self.assertIsNone(renewals[0][1])
            self.assertEqual(renewals[0][2]['refresh_token'],'old-refresh')
            # The access token is never offered as the refresh credential.
            self.assertNotIn('old-access',json.dumps(renewals[0][2]))

    def test_lost_reply_is_recovered_by_identical_retry_not_treated_as_reuse(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td);service=Service();service.lose_reply=True
            with patch('agentclient.credentials._request',side_effect=service):
                with self.assertRaises(TimeoutError):renew(cfg,force=True)
                self.assertEqual(path.read_text().strip(),'old-access')
                self.assertTrue((Path(td)/'credential.pending.json').exists())
                result=renew(cfg)
            self.assertEqual(result['status'],'recovered');self.assertFalse(service.revoked)
            self.assertEqual(service.rotations,1)
            self.assertEqual((path.read_text().strip(),(Path(td)/'credential.refresh').read_text().strip()),service.current())

    def test_crash_between_token_writes_recovers_without_reuse(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td);service=Service();service.lose_reply=True
            with patch('agentclient.credentials._request',side_effect=service):
                with self.assertRaises(TimeoutError):renew(cfg,force=True)
                pending=json.loads((Path(td)/'credential.pending.json').read_text())
                # Access slot written, refresh slot not yet: resend is still identical.
                path.write_text(pending['replacement_access_token'])
                self.assertEqual(renew(cfg)['status'],'recovered')
            self.assertFalse(service.revoked);self.assertEqual(service.rotations,1)

    def test_revoked_family_requires_login_and_keeps_local_tokens(self):
        from agentclient.credentials import renew,LoginRequired
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td);service=Service();service.revoked=True
            with patch('agentclient.credentials._request',side_effect=service):
                with self.assertRaises(LoginRequired):renew(cfg,force=True)
            self.assertEqual(path.read_text().strip(),'old-access')
            self.assertEqual((Path(td)/'credential.refresh').read_text().strip(),'old-refresh')
            self.assertFalse((Path(td)/'credential.pending.json').exists())

    def test_profile_without_refresh_token_never_renews_with_access_token(self):
        from agentclient.credentials import renew,LoginRequired
        with tempfile.TemporaryDirectory() as td:
            cfg,_=file_profile(td,refresh=None);service=Service()
            with patch('agentclient.credentials._request',side_effect=service):
                with self.assertRaises(LoginRequired):renew(cfg)
            self.assertFalse(any(call[0].endswith('auth/renew') for call in service.calls))

    def test_expired_access_token_still_renews_with_refresh_token(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,_=file_profile(td);service=Service(expires_in=-60)
            with patch('agentclient.credentials._request',side_effect=service):
                self.assertEqual(renew(cfg)['status'],'renewed')

    def test_parallel_renewals_share_one_dispatch_and_cached_metadata(self):
        from agentclient.credentials import renew
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td);service=Service()
            with patch('agentclient.credentials._request',side_effect=service),ThreadPoolExecutor(4) as pool:
                values=list(pool.map(lambda _:renew(cfg),range(8)))
            self.assertEqual(service.rotations,1);self.assertFalse(service.revoked)
            self.assertEqual(sum(v['status']=='renewed' for v in values),1)
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_endpoint_change_never_sends_pending_secret(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td)
            pending=path.with_name('credential.pending.json')
            pending.write_text(json.dumps({'endpoint':'http://127.0.0.1:1'}));pending.chmod(0o600)
            cfg['url']='http://127.0.0.1:2'
            with patch('agentclient.credentials._request') as call:
                with self.assertRaisesRegex(ValueError,'endpoint_changed'):renew(cfg)
            call.assert_not_called()


class KeychainStorage(unittest.TestCase):
    def profile(self,td,keyring):
        home=Path(td);backend={'mode':'enterprise_local','api_version':'cloud-local-1','url':'http://127.0.0.1:1',
            'credential_file':str(home/'credential'),'credential_store':'keyring','credential_renewal':True}
        (home/'config.json').write_text(json.dumps({'projects':{'/synthetic':'maple'},'knowledge_backend':backend}))
        from agentclient.credentials import open_store
        store=open_store(backend,keyring=keyring);store.write('access','old-access');store.write('refresh','old-refresh')
        return home,backend

    def test_keychain_holds_tokens_and_helpers_only_see_access_token(self):
        from agentclient.credentials import renew
        from agentclient.transport import enterprise_client
        from agentclient.mcp_headers import headers
        keyring=FakeKeyring();service=Service()
        with tempfile.TemporaryDirectory() as td,patch('agentclient.credentials.keyring_backend',return_value=keyring),\
                patch('agentclient.credentials._request',side_effect=service):
            home,backend=self.profile(td,keyring)
            self.assertEqual(renew(backend)['status'],'renewed')
            access,refresh=service.current()
            self.assertEqual(sorted(value for value in keyring.items.values()),sorted([access,refresh]))
            self.assertFalse((home/'credential').exists());self.assertFalse((home/'credential.refresh').exists())
            self.assertFalse(any(refresh in p.read_text() for p in home.iterdir() if p.is_file()))
            self.assertEqual(enterprise_client({'knowledge_backend':backend}).token,access)
            value=headers(home,project='maple')
            self.assertEqual(value['Authorization'],'Bearer '+access);self.assertNotIn(refresh,json.dumps(value))

    def test_recorded_keychain_store_fails_closed_when_unavailable(self):
        from agentclient.credentials import access_token
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);(home/'credential').write_text('stale-file-token');(home/'credential').chmod(0o600)
            backend={'url':'http://127.0.0.1:1','credential_file':str(home/'credential'),'credential_store':'keyring'}
            with patch('agentclient.credentials.keyring_backend',return_value=None):
                with self.assertRaisesRegex(ValueError,'keyring_unavailable'):access_token(backend)

    def test_missing_keyring_package_is_detected_as_unavailable(self):
        from agentclient.credentials import keyring_backend
        with patch.dict('sys.modules',{'keyring':None}):self.assertIsNone(keyring_backend())

    def test_enrollment_selects_keychain_or_falls_back_to_private_files(self):
        from agentclient.cloud_enroll import enroll
        from urllib.parse import urlencode
        from urllib.request import urlopen
        for available in (True,False):
            keyring=FakeKeyring() if available else None
            with self.subTest(keyring=available),tempfile.TemporaryDirectory() as td,\
                    patch('agentclient.credentials.keyring_backend',return_value=keyring):
                from agentclient.cloud_enroll import CallbackServer
                with CallbackServer('http://127.0.0.1:0/callback') as callback:
                    def request(path,value):
                        if path.endswith('begin'):
                            return {'state':'expected','expires_at':time.time()+10,'url':'http://127.0.0.1:4444/auth?'+urlencode({
                                'state':'expected','redirect_uri':callback.uri,'response_type':'code',
                                'code_challenge_method':'S256','code_challenge':'a'*43})}
                        return {'credential':'synthetic-access','refresh_token':'synthetic-refresh','expires_at':time.time()+3600}
                    def browser(url):
                        with urlopen(callback.uri+'?state=expected&code=synthetic-code',timeout=2):return True
                    result=enroll(Path(td)/'profile',url='http://127.0.0.1:8888',tenant='orchard',broker='fixture',
                        enrollment='plugin',callback=callback,request=request,browser=browser,timeout=2,credential_store='auto')
                profile=Path(result['profile']);backend=json.loads((profile/'config.json').read_text())['knowledge_backend']
                self.assertEqual(backend['credential_store'],'keyring' if available else 'file')
                self.assertTrue(backend['credential_renewal']);self.assertTrue(result['refresh_token_stored'])
                if available:
                    self.assertEqual(sorted(keyring.items.values()),['synthetic-access','synthetic-refresh'])
                    self.assertFalse((profile/'credential').exists())
                else:
                    self.assertEqual((profile/'credential.refresh').read_text().strip(),'synthetic-refresh')
                    self.assertEqual((profile/'credential.refresh').stat().st_mode&0o777,0o600)
                self.assertNotIn('synthetic-refresh',json.dumps(result)+(profile/'config.json').read_text())


class Logout(unittest.TestCase):
    def test_logout_revokes_family_then_deletes_local_tokens(self):
        from agentclient.cli import main
        with tempfile.TemporaryDirectory() as td:
            cfg,_=file_profile(td);service=Service()
            cfg.update(mode='enterprise_local',api_version='cloud-local-1')
            (Path(td)/'config.json').write_text(json.dumps({'knowledge_backend':cfg}))
            out=io.StringIO()
            with patch('agentclient.credentials._request',side_effect=service),redirect_stdout(out):
                main(['--home',td,'logout'])
            self.assertTrue(service.revoked)
            self.assertEqual(service.calls[-1][2],{'token':'old-refresh'})
            self.assertEqual(json.loads(out.getvalue()),{'revoked':True,'local_credentials_deleted':True})
            self.assertNotIn('old-',out.getvalue())
            for name in ('credential','credential.refresh','credential.pending.json','credential.renewal.json'):
                self.assertFalse((Path(td)/name).exists())

    def test_failed_revocation_keeps_tokens_unless_local_only(self):
        from agentclient.credentials import logout
        with tempfile.TemporaryDirectory() as td:
            cfg,path=file_profile(td)
            with patch('agentclient.credentials._request',side_effect=TimeoutError()):
                with self.assertRaises(TimeoutError):logout(cfg)
                self.assertTrue(path.exists())
            with patch('agentclient.credentials._request') as call:
                self.assertEqual(logout(cfg,local_only=True),{'revoked':False,'local_credentials_deleted':True})
            call.assert_not_called();self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
