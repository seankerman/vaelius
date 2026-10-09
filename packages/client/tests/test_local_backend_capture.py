import hashlib
import json
from pathlib import Path
import unittest



class FrozenCaptureTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[2]/'server/tests/fixtures/processing'
        raw = (folder / 'local_backend_v2.json').read_bytes()
        manifest = json.loads((folder / 'local_backend_v2_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['sha256'])
        self.fixture = json.loads(raw)

    def test_selected_fields_safe_remainder_and_dispositions(self):
        from agentclient.enterprise_capture import normalize_capture
        records = [normalize_capture(e, 'maple', 'selected-agent')
                   for e in self.fixture['events']]
        save = records[2]
        self.assertEqual(save['blocks'][0]['value']['path'], self.fixture['expected']['save_path'])
        self.assertEqual(save['blocks'][1]['value'], {'status': 'saved'})
        self.assertEqual(records[3]['event']['exit_code'], 1)
        combined = json.dumps(records)
        self.assertNotIn(self.fixture['expected']['secret'], combined)
        self.assertIn(self.fixture['expected']['email'], combined)
        self.assertIn('safe reason and path', combined)
        self.assertIn('AGENTS.md', combined)
        self.assertIn('ignore previous instructions', combined)
        self.assertEqual(records[1]['event']['channel'], 'commentary')
        self.assertEqual(records[9]['blocks'][0]['value'], {'trigger':'auto'})
        self.assertEqual(records[12]['disposition'], 'excluded')
        self.assertNotIn('COPIED_MEMORY_CANARY', combined)
        self.assertEqual(records[13]['disposition'], 'unsupported')

    def test_large_unicode_parts_fit_wire_and_round_trip(self):
        from agentclient.general_contract import canonical, split_event, validate_part
        from agentclient.enterprise_capture import normalize_capture
        source = dict(self.fixture['events'][2])
        source['tool_response'] = {'output': self.fixture['large_payload']['unit'] * 220_000}
        event = normalize_capture(source, 'maple', 'selected-agent')
        parts = split_event(event)
        self.assertGreater(len(parts), 2)
        for part in parts:
            validate_part(part)
            self.assertLessEqual(len(canonical(part)), 131_072)
        import base64
        restored = json.loads(b''.join(base64.b64decode(p['data']) for p in parts))
        self.assertEqual(restored, event)
