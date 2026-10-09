"""Explicit hosted selection must not loosen the loopback default."""
import json
from pathlib import Path
import tempfile
import unittest
from agentclient.transport import enterprise_client
from agentclient.install import mcp_block

class HostedTransport(unittest.TestCase):
    def test_https_opt_in_is_shared_by_capture_and_mcp(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);key=home/'token';key.write_text('fixture');key.chmod(0o600)
            backend={'mode':'enterprise_local','api_version':'cloud-local-1','url':'https://memory.example.test',
                'credential_file':str(key),'transport':'https'}
            config={'knowledge_backend':backend};(home/'config.json').write_text(json.dumps(config))
            self.assertEqual(enterprise_client(config).url,backend['url'])
            self.assertIn('https://memory.example.test/mcp',mcp_block(home))
            del backend['transport']
            with self.assertRaises(ValueError):enterprise_client(config)
            backend['transport']='https'
            for url in ('http://memory.example.test','https://user:secret@memory.example.test','https://memory.example.test/?token=x'):
                backend['url']=url
                with self.assertRaises(ValueError):enterprise_client(config)
