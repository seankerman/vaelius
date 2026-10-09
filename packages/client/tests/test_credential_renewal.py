"""Frozen renewal crash/revocation cases. No real server or credentials."""
import json, tempfile, unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

class CredentialRenewal(unittest.TestCase):
    def test_lost_response_recovers_prepared_token_and_scope(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'token';path.write_text('old');path.chmod(0o600)
            cfg={'url':'http://127.0.0.1:1','credential_file':str(path)}
            live=['old'];calls=[]
            def request(backend,token,route,data=None):
                if token!=live[0]:raise HTTPError('synthetic',403,'denied',{},None)
                if data:
                    live[0]=data['replacement_token'];calls.append(route);raise TimeoutError()
                return {'tenant':'acme','principal':'alice','actor':'alice','enrollment':'device',
                        'actions':['read'],'expires_at':100}
            with patch('agentclient.credentials._request',side_effect=request):
                with self.assertRaises(TimeoutError):renew(cfg,force=True)
                self.assertEqual(path.read_text(),'old')
                result=renew(cfg)
            self.assertEqual(path.read_text(),live[0]);self.assertEqual(len(calls),1)
            self.assertEqual(result['status'],'recovered')

    def test_revoked_credential_is_not_reenrolled_or_overwritten(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'token';path.write_text('old');path.chmod(0o600)
            cfg={'url':'http://127.0.0.1:1','credential_file':str(path)}
            with patch('agentclient.credentials._request',side_effect=HTTPError('synthetic',403,'denied',{},None)) as call:
                with self.assertRaises(HTTPError):renew(cfg,force=True)
            self.assertEqual(path.read_text(),'old');self.assertEqual(call.call_count,1)

    def test_parallel_renewals_share_one_dispatch_and_cached_metadata(self):
        from agentclient.credentials import renew
        from concurrent.futures import ThreadPoolExecutor
        import time
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'token';path.write_text('old');path.chmod(0o600)
            cfg={'url':'http://127.0.0.1:1','credential_file':str(path)}
            live=['old'];calls=[]
            def request(backend,token,route,data=None):
                self.assertEqual(token,live[0])
                if data:live[0]=data['replacement_token'];calls.append(route)
                return {'tenant':'acme','principal':'alice','actor':'alice','enrollment':'device',
                    'actions':['read'],'expires_at':time.time()+(10 if live[0]=='old' else 3600)}
            with patch('agentclient.credentials._request',side_effect=request),ThreadPoolExecutor(4) as pool:
                values=list(pool.map(lambda _:renew(cfg),range(8)))
            self.assertEqual(len(calls),1);self.assertEqual(sum(v['status']=='renewed' for v in values),1)
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_endpoint_change_never_sends_pending_secret(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'token';path.write_text('old');path.chmod(0o600)
            pending=path.with_name('token.pending.json')
            pending.write_text(json.dumps({'endpoint':'http://127.0.0.1:1'}));pending.chmod(0o600)
            cfg={'url':'http://127.0.0.1:2','credential_file':str(path)}
            with patch('agentclient.credentials._request') as call:
                with self.assertRaisesRegex(ValueError,'endpoint_changed'):renew(cfg)
            call.assert_not_called()
