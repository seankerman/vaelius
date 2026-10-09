import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class VaeliusCliTests(unittest.TestCase):
    def test_new_command_has_separate_default_without_changing_explicit_profiles(self):
        from agentclient.cli import main, vaelius_main
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch('agentclient.cli.Path.home', return_value=home), patch('agentclient.install.uninstall') as uninstall:
                vaelius_main(['uninstall'])
                self.assertEqual(uninstall.call_args.args[0], home / '.local/share/vaelius/client')
                main(['uninstall'])
                self.assertEqual(uninstall.call_args.args[0], home / '.local/share/agentnetwork/client')
                explicit = home / 'chosen-profile'
                vaelius_main(['--home', str(explicit), 'uninstall'])
                self.assertEqual(uninstall.call_args.args[0], explicit)

    def test_client_imports_require_no_server_or_model_runtime(self):
        import sys
        code = ('import agentclient.cli, sys; '
                'assert not any(x.startswith(("agenthub", "onnxruntime", "tokenizers")) for x in sys.modules)')
        subprocess.run([sys.executable, '-I', '-c', code], check=True, capture_output=True)
