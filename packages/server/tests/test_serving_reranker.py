"""Provider-free serving contracts; fixture expectations are authored development."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from unittest.mock import patch
import unittest

from agenthub.serving_reranker import ServingReranker, apply_selection, candidate_packet

FIXTURE = Path(__file__).parent/'fixtures/serving_reranker_v1/cases.json'


class Meter:
    def __init__(self):
        self.events = []
    def reserve(self, *args, **kwargs): self.events.append(('reserve', args, kwargs))
    def dispatched(self, *args): self.events.append(('dispatched', args))
    def finish(self, *args, **kwargs): self.events.append(('finish', args, kwargs))


class ServingRerankerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meter = Meter()
        self.calls = []
        self.usage = {'input_tokens': 120, 'output_tokens': 12, 'cached_input_tokens': 100}
        self.cases = json.loads(FIXTURE.read_text())['cases']

    def ranker(self, output, **kwargs):
        def runner(home, config, instruction, payload, schema):
            self.calls.append((home, config, instruction, payload, schema))
            if isinstance(output, Exception): raise output
            return output, self.usage
        return ServingReranker(self.root/'private', {}, ledger=self.root/'ledger.sqlite',
                               meter=self.meter, runner=runner, **kwargs)

    def test_process_admission_is_nonblocking_and_has_no_accounting_attempt(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch('agenthub.serving_reranker._PROVIDER_SLOTS', slots):
            result = self.ranker({}).rerank('query', self.cases[3]['cards'])
        slots.release()
        self.assertEqual(result['coverage_gaps'], ['reranker_busy'])
        self.assertEqual(result['reranking']['status'], 'busy')
        self.assertFalse(self.calls)
        self.assertFalse(self.meter.events)
        self.assertFalse((self.root/'ledger.sqlite').exists())

    def test_process_admission_releases_after_invalid_input_and_provider_failure(self):
        slots = threading.BoundedSemaphore(1)
        with patch('agenthub.serving_reranker._PROVIDER_SLOTS', slots):
            with self.assertRaises(ValueError):
                self.ranker({}).rerank('q'*2001, self.cases[3]['cards'])
            failed = self.ranker(TimeoutError()).rerank('query', self.cases[3]['cards'])
            self.assertEqual(failed['reranking']['status'], 'unavailable')
            valid = self.ranker({'order': [], 'support': 'none'}).rerank('query', self.cases[3]['cards'])
            self.assertEqual(valid['reranking']['status'], 'returned')
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))
        slots.release()

    def test_fixture_freeze(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            'dae19d0f721aa42d57c1812ead9e15b5da9abbb8f43b8c339d58873bcb9974d1')

    def test_authored_cases_preserve_exact_cards_and_evidence(self):
        for case in self.cases:
            with self.subTest(case=case['name']):
                ranker = self.ranker({'order': case['order'], 'support': case['support']})
                result = ranker.rerank(case['query'], case['cards'])
                expected = [case['cards'][int(key[1:])] for key in case['order']]
                self.assertEqual(result['results'], expected)
                self.assertEqual(result['answerable'], case['support'] == 'complete')
                self.assertEqual(result['reranking']['usage'], self.usage)
                self.assertEqual(result['reranking']['status'], 'returned')
                for card in result['results']: self.assertIn(card, case['cards'])

    def test_no_constructor_or_empty_candidate_provider_call(self):
        ranker = self.ranker({})
        self.assertEqual(self.calls, [])
        self.assertFalse(ranker.rerank('query', [])['answerable'])
        self.assertEqual(self.meter.events, [])
        self.assertFalse((self.root/'ledger.sqlite').exists())

    def test_complete_support_is_not_tied_to_old_deterministic_gate(self):
        result = self.ranker({'order': ['c0'], 'support': 'complete'}).rerank(
            'Where?', self.cases[3]['cards'])
        self.assertTrue(result['answerable'])
        self.assertEqual(result['support'],'complete')
        self.assertTrue(result['results'])

    def test_unknown_duplicate_or_malformed_output_fails_closed(self):
        for output in ({'order': ['secret-id'], 'support': 'complete'},
                       {'order': ['c0', 'c0'], 'support': 'complete'},
                       {'order': ['c0'], 'support': 'none'},
                       {'order': [], 'support': 'complete'},
                       {'order': ['c0'], 'support': 'complete', 'answer': 'invented'},
                       {'order': [False], 'support': 'partial'}, None):
            with self.subTest(output=output):
                result = self.ranker(output).rerank('query', self.cases[3]['cards'])
                self.assertEqual(result['results'], [])
                self.assertFalse(result['answerable'])
                self.assertEqual(result['coverage_gaps'], ['reranker_invalid_output'])
                self.assertEqual(self.meter.events[-1][1][1], 'returned')

    def test_provider_failure_retained_and_not_retried(self):
        result = self.ranker(TimeoutError('source text must not leak')).rerank('query', self.cases[3]['cards'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['coverage_gaps'], ['reranker_unavailable'])
        self.assertNotIn('source text', json.dumps(result))
        self.assertEqual(self.meter.events[-1][1][1], 'uncertain')
        with sqlite3.connect(self.root/'ledger.sqlite') as db:
            self.assertEqual(db.execute('select status from evaluation_attempts').fetchone()[0], 'failed')

    def test_returned_invalid_output_is_still_accounted(self):
        self.ranker({'order': ['unknown'], 'support': 'partial'}).rerank('query', self.cases[3]['cards'])
        with sqlite3.connect(self.root/'ledger.sqlite') as db:
            row = db.execute('select status,input_tokens,output_tokens,cached_input_tokens from evaluation_attempts').fetchone()
        self.assertEqual(row, ('complete', 120, 12, 100))
        self.assertEqual(self.meter.events[-1][1][1], 'returned')

    def test_model_and_tools_are_internally_fixed(self):
        self.ranker({'order': [], 'support': 'none'}).rerank('query', self.cases[3]['cards'])
        _, config, instruction, payload, schema = self.calls[0]
        self.assertEqual(config['observer']['model'], 'gpt-6-luna')
        self.assertEqual(config['observer']['reasoning'], 'low')
        self.assertEqual(config['observer']['timeout_seconds'], 20)
        self.assertEqual(config['_purpose'], 'serving_rerank')
        self.assertFalse(schema['additionalProperties'])
        self.assertIn('untrusted data', instruction)
        self.assertEqual(set(payload), {'query', 'candidates'})

    def test_input_projection_bounds_and_omits_unauthorized_metadata(self):
        cards = [{'id': str(i), 'revision': 'r1', 'lesson': 'x'*4000,
                  'title': 'y'*400, 'raw_source': 'NEVER SEND'} for i in range(30)]
        payload, head, truncated = candidate_packet('query', cards)
        self.assertEqual(len(head), 20)
        self.assertTrue(truncated)
        self.assertLess(len(json.dumps(payload)), 65536)
        self.assertNotIn('NEVER SEND', json.dumps(payload))
        self.assertNotIn('raw_source', json.dumps(payload))
        result = self.ranker({'order': ['c0'], 'support': 'complete'}).rerank('query', cards)
        self.assertFalse(result['answerable'])
        self.assertEqual(len(result['results'][0]['lesson']), 4000)

    def test_unicode_escape_expansion_remains_bounded(self):
        cards = [{'id': str(i), 'revision': 'r', 'lesson': '\U0001f600'*2000} for i in range(20)]
        payload, head, truncated = candidate_packet('\U0001f600'*2000, cards)
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=True).encode()), 65536)
        self.assertTrue(head)
        self.assertTrue(truncated)

    def test_invalid_input_never_dispatches(self):
        for cards in ([{'id': 'x', 'revision': 'r'}, {'id': 'x', 'revision': 'r'}],
                      [{'id': 'x'}], [{'id': 'x', 'revision': 'r', 'lesson': []}]):
            with self.assertRaises(ValueError): self.ranker({}).rerank('query', cards)
        with self.assertRaises(ValueError): self.ranker({}).rerank('q'*2001, self.cases[3]['cards'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.meter.events, [])

    def test_unknown_or_incomplete_provider_configuration_rejected(self):
        for config in ({'backend_execution': {'kind': 'unknown'}}, {'backend_execution': {'kind': 'operator_api'}}):
            with self.assertRaisesRegex(RuntimeError, 'unknown_execution_adapter|explicit_operator_provider_config_required'):
                ServingReranker(self.root, config, ledger=self.root/'ledger', meter=self.meter)

    def test_runtime_bounds_reject_bool_and_unbounded_settings(self):
        for option in ({'timeout_seconds': True}, {'timeout_seconds': 600},
                       {'max_candidates': 21}, {'max_chars': 9999}):
            with self.assertRaisesRegex(ValueError, 'execution_bounds'): self.ranker({}, **option)

    def test_synthetic_candidate_count_does_not_change_accounting_call_count(self):
        counts = []
        for size in (1, 20, 100):
            cards = [{'id': str(i), 'revision': 'r', 'lesson': 'fact'} for i in range(size)]
            self.meter.events.clear()
            self.ranker({'order': ['c0'], 'support': 'partial'}).rerank('query', cards)
            counts.append(len(self.meter.events))
        self.assertEqual(counts, [3, 3, 3])

    def test_admission_failure_never_dispatches(self):
        def deny(*args, **kwargs): raise RuntimeError('busy')
        self.meter.reserve = deny
        result = self.ranker({}).rerank('query', self.cases[3]['cards'])
        self.assertFalse(self.calls)
        self.assertEqual(result['coverage_gaps'], ['reranker_unavailable'])

    def test_reference_validator_accepts_no_freeform_evidence(self):
        cards = self.cases[3]['cards']
        with self.assertRaises(ValueError): apply_selection(cards, {'order': ['c0\nother'], 'support': 'complete'})


if __name__ == '__main__': unittest.main()
