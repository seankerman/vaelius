"""One backend, explicit project connections, no cross-project fallback."""
import unittest
from agentclient.transport import capture_connection

class CaptureConnectionTests(unittest.TestCase):
    def test_each_enrolled_project_selects_its_own_connection(self):
        cfg={'knowledge_backend':{'mode':'enterprise_local','api_version':'cloud-local-1',
            'connection_ids':{'one':'c-one','two':'c-two'},'connection_id':'old'}}
        self.assertEqual(capture_connection(cfg,'one'),'c-one')
        self.assertEqual(capture_connection(cfg,'two'),'c-two')
        with self.assertRaises(ValueError):capture_connection(cfg,'unknown')

    def test_single_connection_profiles_remain_supported_and_malformed_mapping_denies(self):
        cfg={'knowledge_backend':{'mode':'enterprise_local','api_version':'cloud-local-1','connection_id':'one'}}
        self.assertEqual(capture_connection(cfg,'project'),'one')
        for mapping in ([],{'project':None},{'project':''}):
            cfg['knowledge_backend']['connection_ids']=mapping
            with self.assertRaises(ValueError):capture_connection(cfg,'project')
