"""Frozen failures reproduced during PR #1 review; synthetic secrets only."""
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from test_credential_renewal import Service, file_profile, SCOPE


class RegressionTests(unittest.TestCase):
    def test_delegation_reduction_is_accepted_without_identity_change(self):
        from agentclient.credentials import renew, _binding
        with tempfile.TemporaryDirectory() as td:
            cfg, path = file_profile(td)
            saved = dict(SCOPE, actions=['ingest', 'read'], expires_at=time.time()-1,
                         binding=_binding(cfg['url'], 'old-access'))
            state = Path(td)/'credential.renewal.json'
            state.write_text(json.dumps(saved)); state.chmod(0o600)
            with patch('agentclient.credentials._request', side_effect=Service()):
                self.assertEqual(renew(cfg)['status'], 'renewed')
            self.assertNotEqual(path.read_text().strip(), 'old-access')
            self.assertFalse((Path(td)/'credential.pending.json').exists())

    def test_scope_expansion_is_rejected(self):
        from agentclient.credentials import renew, _binding
        with tempfile.TemporaryDirectory() as td:
            cfg, path = file_profile(td)
            saved = dict(SCOPE, actions=[], expires_at=time.time()-1,
                         binding=_binding(cfg['url'], 'old-access'))
            state = Path(td)/'credential.renewal.json'
            state.write_text(json.dumps(saved)); state.chmod(0o600)
            with patch('agentclient.credentials._request', side_effect=Service()):
                with self.assertRaisesRegex(ValueError, 'scope_changed'): renew(cfg)
            self.assertEqual(path.read_text().strip(), 'old-access')

    def test_plaintext_backend_is_not_a_secure_keychain(self):
        from agentclient.credentials import keyring_backend
        backend = types.SimpleNamespace(priority=0.5)
        module = types.SimpleNamespace(get_keyring=lambda: backend)
        backends = types.ModuleType('keyring.backends')
        backends.fail = types.SimpleNamespace(Keyring=type('Fail', (), {}))
        with patch.dict('sys.modules', {'keyring': module, 'keyring.backends': backends}):
            self.assertIsNone(keyring_backend())

    def test_short_lifetime_does_not_rotate_on_every_request(self):
        from agentclient.credentials import renew
        with tempfile.TemporaryDirectory() as td:
            cfg, _ = file_profile(td); service=Service(expires_in=60)
            with patch('agentclient.credentials._request', side_effect=service):
                for _ in range(3): self.assertEqual(renew(cfg)['status'], 'current')
            self.assertEqual(service.rotations, 0)
