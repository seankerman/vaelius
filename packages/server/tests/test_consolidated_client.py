"""Frozen cleanup controls: model-free imports, owned MCP config and feedback transport."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock
from agentclient.install import install, uninstall
from agentclient.mcp import MemoryTools


def profile(home):
    cfg={'projects':{'/synthetic/project':'demo'},'sessions':{},'knowledge_backend':{
        'mode':'enterprise_local','api_version':'cloud-local-1','capture_version':'enterprise-local-2',
        'url':'http://127.0.0.1:55555','credential_file':str(home/'credential'),'connection_id':'demo'}}
    (home/'config.json').write_text(json.dumps(cfg));return cfg


class ConsolidatedClient(unittest.TestCase):
    def test_all_client_modules_import_without_backend_or_models(self):
        code='import pkgutil,importlib,sys,agentclient; [importlib.import_module(m.name) for m in pkgutil.walk_packages(agentclient.__path__, "agentclient.")]; assert not any(k.startswith(("agenthub", "vaelius_test_support", "onnxruntime", "tokenizers")) for k in sys.modules)'
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True)

    def test_install_reinstall_uninstall_preserves_other_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'client';home.mkdir();profile(home)
            codex=Path(td)/'codex';codex.mkdir();original='# personal settings\nmodel = "configured"\n[mcp_servers.other]\ncommand = "other"\n'
            (codex/'config.toml').write_text(original)
            install(home,codex,{},{});install(home,codex,{},{})
            cfg=tomllib.loads((codex/'config.toml').read_text())
            self.assertEqual(cfg['mcp_servers']['other']['command'],'other')
            self.assertIn('agentnetwork_memory',cfg['mcp_servers'])
            self.assertEqual(cfg['mcp_servers']['agentnetwork_memory']['url'],'http://127.0.0.1:55555/mcp')
            self.assertIn('agentclient.mcp_headers',cfg['mcp_servers']['agentnetwork_memory']['http_headers_helper'])
            self.assertNotIn('args',cfg['mcp_servers']['agentnetwork_memory'])
            uninstall(home)
            self.assertEqual((codex/'config.toml').read_text(),original)
            self.assertEqual(json.loads((codex/'hooks.json').read_text())['hooks'],{})

    def test_conflicting_server_does_not_modify_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'client';home.mkdir();profile(home);codex=Path(td)/'codex';codex.mkdir()
            (codex/'config.toml').write_text('[mcp_servers.agentnetwork_memory]\ncommand="other"\n')
            with self.assertRaises(ValueError):install(home,codex,{}, {})
            self.assertFalse((codex/'hooks.json').exists())

    def test_legacy_profile_cannot_open_a_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);(home/'config.json').write_text('{"projects":{"/tmp":"demo"}}')
            with self.assertRaises(ValueError):MemoryTools(home,'demo').call('memory_status',{})
            self.assertFalse((home/'client.sqlite').exists())

    def test_feedback_is_revision_scoped_and_not_verified(self):
        from agenthub.mcp_tools import MemoryTools as BackendTools
        backend=Mock()
        backend.request.return_value={'status':'recorded','classification':'agent_self_report','independently_verified':False}
        result=BackendTools('demo',backend).call('report_memory_outcome',
            {'id':'doc','revision':'r1','outcome':'helpful','request_key':'feedback-1'})
        path,data=backend.request.call_args.args
        self.assertEqual(path,'/enterprise/v3/feedback');self.assertEqual(data['revision'],'r1')
        self.assertFalse(result['independently_verified'])
