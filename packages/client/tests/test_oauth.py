"""Provider refresh recovery is independent of private profiles and model SDKs."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_credential_renewal import file_profile,SCOPE


def profile(td):
    cfg,path=file_profile(td)
    cfg.update(tenant='acme',enrollment_id='device',oauth={'issuer':'https://identity.example',
        'client_id':'plugin','token_endpoint':'https://identity.example/token','resource':'https://memory.example/mcp'})
    return cfg,path


class OAuthRecovery(unittest.TestCase):
    def test_provider_may_issue_access_without_refresh(self):
        from agentclient.oauth import issued
        self.assertEqual(issued({'token_type':'Bearer','access_token':'synthetic-access','expires_in':60}),
                         ('synthetic-access',None))

    def test_wrong_issuer_callback_is_rejected_before_code_exchange(self):
        from agentclient.oauth import enroll
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            callback=SimpleNamespace(uri='http://127.0.0.1:59001/callback',state=None)
            callback.wait=lambda timeout:{'state':callback.state,'code':'synthetic-code','iss':'https://other.example'}
            with patch('agentclient.oauth.discover',return_value={'issuer':'https://identity.example',
                    'authorization_endpoint':'https://identity.example/auth','scope':'openid',
                    'resource':'https://memory.example/mcp','require_issuer_response':True}),\
                    patch('agentclient.oauth.request') as request:
                with self.assertRaisesRegex(ValueError,'issuer_mismatch'):
                    enroll(Path(td)/'profile',url='https://memory.example',tenant='acme',client_id='plugin',
                           enrollment='device',callback=callback,credential_store='file')
                request.assert_not_called()
    def test_ambiguous_dispatch_is_never_retried(self):
        from agentclient.credentials import renew,LoginRequired
        with tempfile.TemporaryDirectory() as td:
            cfg,path=profile(td)
            with patch('agentclient.oauth.request',side_effect=TimeoutError()) as request:
                with self.assertRaises(TimeoutError):renew(cfg,force=True)
                with self.assertRaises(LoginRequired):renew(cfg)
                self.assertEqual(request.call_count,1)
            self.assertEqual(path.read_text().strip(),'old-access')

    def test_saved_provider_return_recovers_without_another_refresh(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=profile(td)
            pending={'phase':'returned','endpoint':cfg['url'],'issuer':cfg['oauth']['issuer'],
                     'access':'synthetic-new-access','refresh':'synthetic-new-refresh','scope':SCOPE}
            journal=Path(td)/'credential.pending.json';journal.write_text(json.dumps(pending));journal.chmod(0o600)
            with patch('agentclient.oauth._request',return_value=dict(SCOPE,expires_at=time.time()+60)),\
                    patch('agentclient.oauth.request') as request:
                self.assertEqual(renew(cfg)['status'],'recovered');request.assert_not_called()
            self.assertEqual(path.read_text(),'synthetic-new-access');self.assertFalse(journal.exists())

    def test_endpoint_change_rejects_saved_return_before_sending_it(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=profile(td)
            journal=Path(td)/'credential.pending.json';journal.write_text(json.dumps({'phase':'returned',
                'endpoint':'https://other.example','issuer':cfg['oauth']['issuer']}));journal.chmod(0o600)
            with patch('agentclient.oauth._request') as request:
                with self.assertRaisesRegex(ValueError,'endpoint_changed'):renew(cfg)
                request.assert_not_called()

    def test_interrupted_login_commits_profile_with_preserved_enrollment(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg,path=profile(td)
            original={'projects':{'/synthetic':'maple'},'sessions':{'s':'maple'},'knowledge_backend':dict(cfg)}
            config=Path(td)/'config.json';config.write_text(json.dumps(original));config.chmod(0o600)
            pending={'phase':'login_returned','endpoint':cfg['url'],'issuer':cfg['oauth']['issuer'],
                'access':'synthetic-new-access','refresh':'synthetic-new-refresh','backend':cfg,
                'profile':td,'initial_config':{},'identity':SCOPE}
            journal=Path(td)/'credential.pending.json';journal.write_text(json.dumps(pending));journal.chmod(0o600)
            with patch('agentclient.oauth._request',return_value=dict(SCOPE,expires_at=time.time()+60)),\
                    patch('agentclient.oauth.request') as request:
                self.assertEqual(renew(cfg)['status'],'recovered');request.assert_not_called()
            self.assertEqual(json.loads(config.read_text())['projects'],original['projects'])
            self.assertEqual(json.loads(config.read_text())['sessions'],original['sessions'])
