"""Cloud delivery contracts: preserve NONE, bounded private defaults and originals."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class CloudDeliveryTests(unittest.TestCase):

    def config(self, home):
        cfg = {'projects': {'/fictional/project': 'demo'}, 'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2', 'capture_owner': 'transcript', 'api_version': 'cloud-local-1'}}
        (Path(home) / 'config.json').write_text(json.dumps(cfg))
        return cfg

    def backend(self):

        class Backend:

            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append((path, value))
                if path.endswith('preferences'):
                    return {'values': {'test_runner': 'pytest'}, 'private_owner': 'alice', 'preferences': [{'key': 'test_runner', 'value': 'pytest', 'scope': 'user', 'project': None, 'task': None, 'document_id': 'private-pref', 'revision_id': 'r2'}]}
                if path.endswith('search'):
                    return {'answerable': False, 'results': [{'id': 'candidate', 'revision': 'r1', 'title': 'Nearby evidence', 'lesson': 'Uncertain', 'evidence_status': 'unverified'}]}
                return {'status': 'recorded'}
        return Backend()

    def test_hook_start_delivers_preferences_and_receipts_without_search(self):
        from agentclient.hooks import handle
        with tempfile.TemporaryDirectory() as home:
            self.config(home)
            backend = self.backend()
            with patch('agentclient.transport.enterprise_client', return_value=backend):
                result = handle(home, {'hook_event_name': 'SessionStart', 'cwd': '/fictional/project', 'session_id': 'new'})
            context = result['hookSpecificOutput']['additionalContext']
            self.assertIn('pytest', context)
            self.assertLessEqual(len(context), 1500)
            self.assertFalse(any((p.endswith('search') for p, _ in backend.calls)))
            receipts = [v for p, v in backend.calls if p.endswith('receipts')]
            self.assertEqual(receipts[0]['cards'], [{'id': 'private-pref', 'revision': 'r2'}])
            self.assertFalse((Path(home) / 'knowledge.sqlite').exists())

    def test_delivery_diagnostics_cannot_hold_an_acknowledged_source_turn(self):
        from agentclient.capture_outbox import Outbox
        fixture = json.loads((Path(__file__).resolve().parents[2] / 'server/tests/fixtures/processing/cloud_diagnostics_v1.json').read_text())
        with tempfile.TemporaryDirectory() as home:
            cfg = self.config(home)
            cfg['knowledge_backend']['connection_id'] = 'captured-agent'
            records = [dict(case, at=100, kind='UserPromptSubmit', session='complete-chat', session_hash='synthetic', turn='first', project='demo') for case in fixture['cases']]
            (Path(home) / 'enterprise-capture-gaps.jsonl').write_text(''.join((json.dumps(r) + '\n' for r in records)))
            box = Outbox(home)
            try:
                self.assertEqual(box.queue_diagnostics(cfg), sum((r['capture_gap'] for r in records)))
                payloads = [json.loads(r[0]) for r in box.db.execute('SELECT payload FROM pending')]
                self.assertEqual({p['blocks'][0]['value']['reason'] for p in payloads}, {r['reason'] for r in records if r['capture_gap']})
                self.assertEqual(box.queue_diagnostics(cfg), 0)
            finally:
                box.close()

    def test_explicitly_useful_partial_evidence_is_delivered_but_unknown_is_not(self):
        from agentclient.hooks import handle
        with tempfile.TemporaryDirectory() as home:
            self.config(home)
            for support in ('partial','none',None):
                backend=self.backend();request=backend.request
                def result(path,value=None):
                    reply=request(path,value)
                    if path.endswith('search') and support is not None:reply['support']=support
                    return reply
                backend.request=result
                with patch('agentclient.transport.enterprise_client',return_value=backend):
                    reply=handle(home,{'hook_event_name':'UserPromptSubmit','cwd':'/fictional/project',
                        'session_id':'partial-session','prompt':'Where is the artifact?'})
                context=reply.get('hookSpecificOutput',{}).get('additionalContext','')
                self.assertEqual('Nearby evidence' in context,support=='partial')
                if support=='partial':self.assertIn('may be incomplete',context)

    def test_support_is_validated_at_transport_boundary(self):
        from agentclient.cloud_contract import validate_response
        card={'id':'d','revision':'r','title':'t','lesson':'l','evidence_status':'unverified'}
        for support,answerable,cards in [('partial',False,[card]),('none',False,[]),('complete',True,[card])]:
            reply={'results':cards,'answerable':answerable,'support':support}
            self.assertEqual(validate_response('/enterprise/v3/search',reply),reply)
        for support,answerable,cards in [('maybe',False,[card]),('complete',False,[card]),('partial',True,[card]),('none',False,[card])]:
            with self.assertRaises(ValueError):validate_response('/enterprise/v3/search',{'results':cards,'answerable':answerable,'support':support})
