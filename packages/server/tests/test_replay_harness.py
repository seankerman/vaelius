"""Native replay must expose the isolated MCP host without shell or global hooks."""
from pathlib import Path
import unittest
from unittest.mock import patch
from vaelius_test_support.client.replay_harness import command,DISABLED,allowed_discovery


class ReplayCommandTests(unittest.TestCase):
    def test_empty_builtin_discovery_is_not_a_foreign_memory_server(self):
        item={'server':'codex','tool':'list_mcp_resources','result':{'content':[{'type':'text','text':'{"resources":[]}'}]}}
        self.assertTrue(allowed_discovery(item))
        item['result']['content'][0]['text']='{"resources":[{"uri":"private://elsewhere"}]}'
        self.assertFalse(allowed_discovery(item))
        item['server']='unknown';self.assertFalse(allowed_discovery(item))

    def test_mcp_code_host_available_while_shell_and_hooks_disabled(self):
        with patch('vaelius_test_support.client.replay_harness.resolve_executable',return_value='/bin/codex'):
            args=command({'observer':{'model':'gpt-6-luna'}},Path('/private/replay'),['/bin/python','gateway.py'])
        self.assertNotIn('code_mode_host',DISABLED)
        self.assertIn('shell_tool',DISABLED);self.assertIn('hooks',DISABLED)
        self.assertIn('--ignore-user-config',args);self.assertIn('--ephemeral',args)
        self.assertIn('forced_login_method="chatgpt"',args)
        self.assertIn('mcp_servers.replay_memory.required=true',args)
        self.assertIn('code_mode_host',args)


if __name__=='__main__':unittest.main()
