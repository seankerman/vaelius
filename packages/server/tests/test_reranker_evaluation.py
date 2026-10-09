"""Authored labels and grading guardrails; no provider calls or LLM judge."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rank_evaluation', ROOT/'tools/evaluate_serving_reranker.py')
evaluation = importlib.util.module_from_spec(spec); spec.loader.exec_module(evaluation)


class RerankerEvaluation(unittest.TestCase):
    def test_fixtures_frozen_before_live_comparison(self):
        for name, digest in (
            ('development','38ff0693f50d58dfe3e37c6819abb3f58dcc50552d9a47f6d332de9b3209be6b'),
            ('confirmation','799302a7faf94692f0484b44a7cfb85995b12345cc82373db5d2b7b0392cdd5f')):
            path = ROOT/'tests/fixtures/serving_reranker_v2'/(name+'.json')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            for case in json.loads(path.read_text())['cases']:
                keys = {'c'+str(i) for i in range(len(case['cards']))}
                self.assertTrue(set(case['allowed']).issubset(keys))
                self.assertTrue(set(case['top']).issubset(case['allowed']))
                self.assertTrue(all(set(group).issubset(case['allowed']) for group in case['required_groups']))

    def test_complementary_evidence_cannot_get_credit_for_missing_facet(self):
        case = dict(required_groups=[['c0'],['c1']],allowed=['c0','c1'],top=['c0','c1'],support='complete')
        result = evaluation.grade(case, {'order':['c0'],'support':'complete'})
        self.assertFalse(result['coverage']); self.assertFalse(result['passed'])
        self.assertTrue(evaluation.grade(case, {'order':['c1','c0'],'support':'complete'})['passed'])

    def test_topical_noise_and_false_abstention_are_separate_failures(self):
        case = dict(required_groups=[['c1']],allowed=['c1'],top=['c1'],support='complete')
        noise = evaluation.grade(case, {'order':['c0','c1'],'support':'complete'})
        self.assertTrue(noise['coverage']); self.assertFalse(noise['precision']); self.assertFalse(noise['top'])
        abstain = evaluation.grade(case, {'order':['c1'],'support':'partial'})
        self.assertTrue(abstain['ranking']); self.assertFalse(abstain['support']); self.assertFalse(abstain['passed'])

    def test_malformed_outputs_never_earn_absence_credit(self):
        case = dict(required_groups=[],allowed=[],top=[],support='none')
        for returned in (None, {}, {'support':'none'}, {'order':[],'support':'none','answer':'invented'},
                         {'order':['c0','c0'],'support':'none'}, {'order':'c0','support':'none'}):
            with self.subTest(returned=returned):self.assertFalse(evaluation.grade(case, returned)['passed'])
        self.assertTrue(evaluation.grade(case, {'order':[],'support':'none'})['passed'])
