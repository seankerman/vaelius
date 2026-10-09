"""The client supplies a transport and credentials, never the tool catalogue."""
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from agentclient.mcp import MemoryTools,serve
from agentclient.mcp_headers import headers
from agentclient.install import install,uninstall


def profile(home):
    token=home/'credential';token.write_text('synthetic');token.chmod(0o600)
    config={'projects':{'/synthetic/project':'maple'},'knowledge_backend':{'mode':'enterprise_local',
        'api_version':'cloud-local-1','url':'http://127.0.0.1:55486','credential_file':str(token)}}
    (home/'config.json').write_text(json.dumps(config))


class MCPTransport(unittest.TestCase):
    def test_no_catalog_and_stdio_forwards_unchanged(self):
        import agentclient.mcp as module
        self.assertFalse(hasattr(module,'TOOLS'))
        request={'jsonrpc':'2.0','id':1,'method':'tools/list'}
        expected={'jsonrpc':'2.0','id':1,'result':{'tools':[{'name':'server_owned'}]}}
        memory=MemoryTools('/unused','maple');out=io.StringIO()
        with patch.object(memory,'rpc',return_value=expected) as rpc:
            serve(memory,io.BytesIO((json.dumps(request)+'\n').encode()),out)
            rpc.assert_called_once_with(request)
        self.assertEqual(json.loads(out.getvalue()),expected)

    def test_headers_are_private_and_explicit_scope_is_enrolled(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);profile(home)
            value=headers(home,project='maple')
            self.assertEqual(value['Authorization'],'Bearer synthetic')
            self.assertEqual(value['X-AgentNetwork-Project'],'maple')
            with self.assertRaises(ValueError):headers(home,project='other')
            (home/'credential').chmod(0o644)
            with self.assertRaises(ValueError):headers(home,project='maple')

    def test_direct_http_install_preserves_unrelated_config_and_no_secret_in_toml(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'client';home.mkdir();profile(home)
            codex=Path(td)/'codex';codex.mkdir();original='[mcp_servers.other]\ncommand="other"\n'
            (codex/'config.toml').write_text(original)
            install(home,codex,{},{});install(home,codex,{},{})
            text=(codex/'config.toml').read_text();entry=tomllib.loads(text)['mcp_servers']['agentnetwork_memory']
            self.assertEqual(entry['url'],'http://127.0.0.1:55486/mcp')
            self.assertIn('agentclient.mcp_headers',entry['http_headers_helper'])
            self.assertNotIn('command',entry);self.assertNotIn('synthetic',text)
            uninstall(home);self.assertEqual((codex/'config.toml').read_text(),original)

    def test_upgrade_only_exact_owned_old_stdio_block(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'client';home.mkdir();profile(home);codex=Path(td)/'codex';codex.mkdir()
            old='\n# BEGIN AgentNetwork managed MCP\n[mcp_servers.agentnetwork_memory]\ncommand="old-python"\n# END AgentNetwork managed MCP\n'
            (codex/'config.toml').write_text('# unrelated\n'+old)
            (home/'installation.json').write_text(json.dumps({'mcp_block':old}))
            install(home,codex,{},{});self.assertNotIn('old-python',(codex/'config.toml').read_text())
            (codex/'config.toml').write_text('[mcp_servers.agentnetwork_memory]\ncommand="not-owned"\n')
            with self.assertRaises(ValueError):install(home,codex,{},{})

class InstallUpgrade(unittest.TestCase):
    def test_interpreter_upgrade_replaces_only_receipt_owned_hook_commands(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'client';home.mkdir();profile(home)
            codex=Path(td)/'codex';codex.mkdir()
            unrelated={'type':'command','command':'unrelated-hook','timeout':9}
            (codex/'hooks.json').write_text(json.dumps({'hooks':{'Stop':[{'hooks':[unrelated]}]}}))
            with patch('agentclient.install.sys.executable','/old/python'):install(home,codex,{},{})
            with patch('agentclient.install.sys.executable','/new/python'):install(home,codex,{},{})
            hooks=json.loads((codex/'hooks.json').read_text())['hooks']
            self.assertIn(unrelated,hooks['Stop'][0]['hooks'])
            for groups in hooks.values():
                commands=[h.get('command','') for g in groups for h in g['hooks']]
                self.assertFalse(any('/old/python' in c for c in commands))
                self.assertEqual(sum('/new/python' in c for c in commands),1)
