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


class DiscoveryIssuerSelection(unittest.TestCase):
    """The service publishes one resource-wide document; the enrollment picks its issuer."""
    A, B = 'https://identity.example/realms/acme', 'https://identity.example/realms/bravo'

    def discover(self, document, **kwargs):
        from agentclient.oauth import discover
        def request(url, data=None):
            if url.endswith('/.well-known/openid-configuration'):
                issuer = url.removesuffix('/.well-known/openid-configuration')
                return {'issuer': issuer, 'code_challenge_methods_supported': ['S256'],
                        'authorization_endpoint': issuer + '/auth', 'token_endpoint': issuer + '/token',
                        'revocation_endpoint': issuer + '/revoke'}
            self.assertEqual(url, 'https://memory.example/.well-known/oauth-protected-resource/mcp/acme')
            return document
        with patch('agentclient.oauth.request', side_effect=request):
            return discover('https://memory.example', 'acme', 'plugin', **kwargs)

    def document(self):
        return {'resource': 'https://memory.example/mcp', 'authorization_servers': [self.A, self.B],
                'scopes_supported': ['vaelius:feedback', 'vaelius:ingest', 'vaelius:read', 'vaelius:source_read'],
                'bearer_methods_supported': ['header'],
                'vaelius_authorization_servers': [
                    {'issuer': self.A, 'scopes_supported': ['vaelius:feedback', 'vaelius:ingest', 'vaelius:read']},
                    {'issuer': self.B, 'scopes_supported': ['vaelius:read']}]}

    def test_several_issuers_need_the_enrollment_issuer(self):
        with self.assertRaisesRegex(ValueError, 'explicit_single_tenant_issuer_required'):
            self.discover(self.document())
        chosen = self.discover(self.document(), issuer=self.A)
        self.assertEqual((chosen['issuer'], chosen['token_endpoint']), (self.A, self.A + '/token'))
        # The chosen issuer's own scopes, not the union across issuers.
        self.assertEqual(chosen['scope'], 'openid vaelius:feedback vaelius:ingest vaelius:read')
        self.assertEqual(self.discover(self.document(), issuer=self.B)['scope'], 'openid vaelius:read')

    def test_owner_self_service_scopes_are_requested_when_offered(self):
        document = self.document()
        document['vaelius_authorization_servers'][0]['scopes_supported'] += ['vaelius:withdraw', 'vaelius:share',
                                                                            'vaelius:manage', 'vaelius:moderate']
        # Withdraw, share and manage (workspaces, bounded by the role in each) are
        # requested; moderation never is (it follows the workspace admin role).
        self.assertEqual(self.discover(document, issuer=self.A)['scope'],
                         'openid vaelius:feedback vaelius:ingest vaelius:read vaelius:withdraw vaelius:share '
                         'vaelius:manage')

    def test_issuer_must_be_advertised_by_the_service(self):
        with self.assertRaisesRegex(ValueError, 'oauth_issuer_not_advertised'):
            self.discover(self.document(), issuer='https://identity.example/realms/elsewhere')

    def test_single_issuer_document_still_discovers_without_an_issuer(self):
        document = {'resource': 'https://memory.example/mcp', 'authorization_servers': [self.A],
                    'scopes_supported': ['vaelius:read', 'vaelius:feedback'], 'bearer_methods_supported': ['header']}
        self.assertEqual(self.discover(document)['issuer'], self.A)
