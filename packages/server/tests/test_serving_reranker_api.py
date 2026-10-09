"""Serving boundary regressions with no PostgreSQL or provider dispatch."""
from contextlib import contextmanager
import copy
import json
import unittest

from starlette.testclient import TestClient
from agenthub.cloud_api import create_app
from agenthub.enterprise import Denied


class Store:
    tenant_id = 'synthetic'
    def __init__(self):
        self.locked = False
        self.revoked = False
        self.withdrawn = set()
        self.authentications = 0
        self.checks = []
        self.searches = []
        self.serving_reranker = None
        self.result = {'results': [{'id': 'doc', 'revision': 'r1',
            'title': 'Synthetic source', 'lesson': 'Alice saved the dataset in /data/current.',
            'evidence_status': 'source_linked_unverified'}], 'answerable': True}

    def require_ready(self): pass

    def authenticate(self, token, request_id=None):
        if token != 'synthetic' or self.revoked: raise Denied()
        self.authentications += 1
        return {'tenant': self.tenant_id, 'actor': 'alice', 'actions': ['read'],
                'request_id': request_id, 'authentication': self.authentications}

    @contextmanager
    def delivery_read_lock(self):
        if self.locked: raise AssertionError('nested delivery lock')
        self.locked = True
        try: yield
        finally: self.locked = False

    delivery_lock = delivery_read_lock

    def search(self, ctx, value, *, candidate_pool=False):
        if self.locked: raise AssertionError('search unexpectedly holds delivery lock')
        self.searches.append(copy.deepcopy(value))
        result=copy.deepcopy(self.result)
        if candidate_pool:result['_candidate_pool']=True
        return result

    def validate_search_delivery(self, ctx, value, result):
        if not self.locked: raise AssertionError('delivery check without lock')
        self.checks.append(ctx['authentication'])
        kept = [card for card in result['results'] if card['id'] not in self.withdrawn]
        if len(kept) == len(result['results']): return result
        return dict(result, results=kept, answerable=False,
            coverage_gaps=[*result.get('coverage_gaps', []), 'authorization_changed_before_delivery'])


class Registry:
    def __init__(self, store): self.store = store
    def store_for_token(self, token): return self.store


class Ranker:
    def __init__(self, store, action=None, output=None):
        self.store, self.action, self.output = store, action, output
        self.calls = []

    def rerank(self, query, cards, *, context=None):
        if self.store.locked: raise AssertionError('model called while delivery lock held')
        if not self.store.checks: raise AssertionError('model called before current authorization')
        self.calls.append({'query': query, 'cards': copy.deepcopy(cards),'context':context})
        if self.action: self.action()
        if self.output is not None: return copy.deepcopy(self.output)
        return {'results': list(reversed(cards)), 'answerable': True, 'support':'complete',
                'reranking': {'status': 'returned', 'seconds': 0.01,
                              'usage': {'input_tokens': 100}, 'attempt_id': 'private-attempt'}}


class ServingRerankerApiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.ranker = Ranker(self.store)
        self.store.serving_reranker = self.ranker
        self.client = self.enterContext(TestClient(create_app(Registry(self.store), allowed_hosts=['testserver'],
                                           timing_sink=lambda record: None)))
        self.headers = {'Authorization': 'Bearer synthetic'}

    def search(self, *, path='/enterprise/v3/search', **extra):
        return self.client.post(path, json={'version': '0.2', 'query': 'Where is the dataset?',
            'project': 'project-a', 'mode': 'explicit', **extra}, headers=self.headers)

    def mcp_call(self):
        return self.client.post('/mcp', headers={**self.headers,
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25', 'X-AgentNetwork-Project': 'project-a'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                  'params': {'name': 'search_memory', 'arguments': {'query': 'Where is the dataset?'}}})

    def test_mcp_search_uses_same_reranking_and_authorization_operation(self):
        response = self.mcp_call()
        self.assertEqual(response.status_code, 200, response.text)
        wire = response.json()['result']
        self.assertFalse(wire.get('isError'), wire)
        value = json.loads(wire['content'][0]['text'])
        self.assertEqual(len(self.ranker.calls), 1)
        self.assertEqual(len(self.store.checks), 2)
        self.assertEqual(value['records'], self.store.result['results'])
        self.assertNotIn('private-attempt', response.text)

    def test_mcp_withdrawal_during_model_returns_no_evidence(self):
        self.ranker.action = lambda: self.store.withdrawn.add('doc')
        response = self.mcp_call()
        self.assertEqual(response.status_code, 200, response.text)
        value = json.loads(response.json()['result']['content'][0]['text'])
        self.assertEqual(value['records'], [])
        self.assertFalse(value['answerable'])

    def test_startup_health_and_readiness_do_not_call_reranker(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertEqual(self.client.get('/ready').status_code, 200)
        self.assertFalse(self.ranker.calls)

    def test_rest_versions_share_authorized_unlocked_reranking(self):
        for path in ('/enterprise/v1/search', '/enterprise/v3/search'):
            with self.subTest(path=path):
                self.store.checks.clear()
                response = self.search(path=path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(self.store.checks), 2)
                self.assertGreater(self.store.checks[1], self.store.checks[0])
                self.assertEqual(self.ranker.calls[-1]['cards'], self.store.result['results'])
                self.assertTrue(response.json()['answerable'])
        self.assertEqual(len(self.ranker.calls), 2)

    def test_revoked_identity_during_model_cannot_receive_result(self):
        self.ranker.action = lambda: setattr(self.store, 'revoked', True)
        response = self.search()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.ranker.calls), 1)
        self.assertNotIn('/data/current', response.text)

    def test_withdrawal_during_model_removes_selected_card(self):
        self.ranker.action = lambda: self.store.withdrawn.add('doc')
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['results'], [])
        self.assertFalse(result['answerable'])
        self.assertIn('authorization_changed_before_delivery', result['coverage_gaps'])
        self.assertEqual(len(self.store.checks), 2)

    def test_withdrawal_before_dispatch_never_enters_model_payload(self):
        self.store.withdrawn.add('doc')
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['results'], [])
        self.assertTrue(not self.ranker.calls or not self.ranker.calls[0]['cards'])

    def test_diagnostics_do_not_escape_and_prior_capture_gap_survives(self):
        self.store.result['coverage_gaps'] = ['incomplete_source_capture']
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['coverage_gaps'], ['incomplete_source_capture'])
        for private in ('reranking', 'input_tokens', 'private-attempt'):
            self.assertNotIn(private, response.text)

    def test_failed_model_uses_current_deterministic_fallback_and_marks_gap(self):
        self.store.result['coverage_gaps'] = ['incomplete_source_capture']
        self.ranker.output = {'results': [], 'answerable': False,
            'coverage_gaps': ['reranker_unavailable'],
            'reranking': {'status': 'unavailable', 'seconds': 0.01, 'usage': {}}}
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['results'], self.store.result['results'])
        self.assertTrue(result['answerable'])
        self.assertEqual(set(result['coverage_gaps']), {'incomplete_source_capture', 'reranker_unavailable', 'deterministic_ranking_fallback'})
        self.assertNotIn('reranking', result)

    def test_response_bounds_after_reranking_and_gap_merge(self):
        cards = [{'id': 'd'+str(i), 'revision': 'r1', 'title': 'x'*100,
                  'lesson': 'context '*100} for i in range(10)]
        self.store.result = {'results': cards, 'answerable': True,
                             'coverage_gaps': ['incomplete_source_capture']}
        for mode, bound in (('automatic', 1500), ('explicit', 4000)):
            with self.subTest(mode=mode):
                response = self.search(mode=mode)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=True)), bound)
                self.assertFalse(result['answerable'])
                self.assertEqual(len(self.ranker.calls[-1]['cards']), len(cards))

    def test_partial_evidence_is_not_promoted_to_complete(self):
        self.ranker.output={'results':self.store.result['results'],'answerable':False,
            'support':'partial','reranking':{'status':'returned'}}
        response=self.search()
        self.assertTrue(response.json()['results'])
        self.assertFalse(response.json()['answerable'])
        self.assertEqual(response.json()['support'],'partial')

    def test_oversized_first_card_does_not_erase_smaller_useful_evidence(self):
        first=dict(self.store.result['results'][0],id='huge',lesson='x'*5000)
        self.ranker.output={'results':[first,*self.store.result['results']],
            'answerable':True,'support':'complete','reranking':{'status':'returned'}}
        result=self.search().json()
        self.assertEqual(result['results'],self.store.result['results'])
        self.assertEqual(result['support'],'partial')
        self.assertIn('delivery_evidence_omitted',result['coverage_gaps'])

    def test_medium_card_respects_client_card_contract(self):
        from agentclient.cloud_contract import validate_response
        large=dict(self.store.result['results'][0],id='medium',lesson='x'*1500)
        self.ranker.output={'results':[large,*self.store.result['results']],
            'answerable':True,'support':'complete','reranking':{'status':'returned'}}
        result=self.search(mode='explicit').json()
        self.assertEqual(result['results'],self.store.result['results'])
        self.assertEqual(validate_response('/enterprise/v3/search',result)['support'],'partial')

    def test_disabled_stage_preserves_existing_response(self):
        self.store.serving_reranker = None
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.store.result)
        self.assertFalse(self.ranker.calls)

    def test_historical_request_preserved_for_both_authorization_checks(self):
        response = self.search(as_of='2026-09-01', time_mode='known_at')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.searches[0]['as_of'], '2026-09-01')
        self.assertEqual(self.store.searches[0]['time_mode'], 'known_at')
        self.assertEqual(len(self.store.checks), 2)


if __name__ == '__main__': unittest.main()
