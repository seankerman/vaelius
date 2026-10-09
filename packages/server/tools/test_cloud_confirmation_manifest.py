"""File-only evaluator metadata guard regression; no installed/runtime imports."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).parent
FIXTURE_SHA = '8da73a96353f430442fcbc7b891a9eb3eed0da5a83dd5875bd5a99f57cd76dca'


class ManifestGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'fixtures/cloud_confirmation_manifest_cases_v1.json'
        if hashlib.sha256(path.read_bytes()).hexdigest() != FIXTURE_SHA:
            raise AssertionError('guard fixture changed')
        cls.data = json.loads(path.read_text())

    def tool(self):
        spec = importlib.util.spec_from_file_location('confirmation_next', ROOT / 'evaluate_cloud_confirmation_next.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        return tool

    def load(self, fixture, manifest):
        tool = self.tool()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        fp, mp = home / 'fixture.json', home / 'manifest.json'
        fp.write_text(json.dumps(fixture)); sha = hashlib.sha256(fp.read_bytes()).hexdigest()
        manifest = copy.deepcopy(manifest); manifest['fixture_sha256'] = sha
        mp.write_text(json.dumps(manifest)); tool.FIXTURE_SHA = sha
        calls = []
        def dispatch():
            loaded = tool.load_frozen(fp, mp)
            calls.append('search')
            return loaded
        return dispatch, calls

    def test_historical_integer_iteration_failure_reproduced(self):
        with self.assertRaises(TypeError):
            list(iter(self.data['cases'][0]['patch']['families']))

    def test_valid_list_preserves_actual_families(self):
        dispatch, calls = self.load(self.data['fixture'], self.data['valid_manifest'])
        fixture, manifest = dispatch()
        self.assertEqual(len(fixture['queries']), 30)
        self.assertEqual(len(manifest['families']), 6)
        self.assertEqual(calls, ['search'])

    def test_invalid_manifest_refuses_before_dispatch(self):
        for case in self.data['cases']:
            with self.subTest(case=case['name']):
                manifest = self.data['valid_manifest'] | case['patch']
                dispatch, calls = self.load(self.data['fixture'], manifest)
                with self.assertRaisesRegex(ValueError, 'confirmation_manifest_'):
                    dispatch()
                self.assertEqual(calls, [])

    def test_duplicate_query_ids_refuse_before_dispatch(self):
        fixture = copy.deepcopy(self.data['fixture'])
        fixture['queries'][1]['id'] = fixture['queries'][0]['id']
        dispatch, calls = self.load(fixture, self.data['valid_manifest'])
        with self.assertRaisesRegex(ValueError, 'confirmation_manifest_'):
            dispatch()
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
