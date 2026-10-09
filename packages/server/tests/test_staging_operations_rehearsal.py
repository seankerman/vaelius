"""Public synthetic fixture and safe operational rehearsal controller contracts."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'run_staging_operations_rehearsal.py'
spec = importlib.util.spec_from_file_location('staging_ops_controller', TOOL)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class StagingOperationsPreparationTests(unittest.TestCase):
    def test_source_grounded_variety_replacements_and_lifecycle_targets(self):
        fixture = controller.load_fixture()
        documents = fixture['documents']
        self.assertEqual(len(documents), 20)
        self.assertEqual({Path(doc['filename']).suffix for doc in documents}, {'.md', '.txt', '.html'})
        self.assertGreater(max(len(doc['text']) for doc in documents), 2000)
        self.assertTrue(any('東京' in doc['text'] for doc in documents))
        for doc in documents:
            self.assertGreater(len(doc['query']), 18)
            self.assertGreater(len(doc['expected_answer']), 20)
            if 'replacement' in doc:
                replacement = doc['replacement']
                self.assertNotEqual(replacement['sha256'], doc['sha256'])
                self.assertNotEqual(replacement['expected_answer'], doc['expected_answer'])
                self.assertNotIn(doc['expected_answer'], replacement['text'])
        targets = {fixture['lifecycle'][action] for action in ('withdraw', 'delete')}
        self.assertEqual(len(targets), 2)
        self.assertTrue(targets <= {doc['id'] for doc in documents})
        self.assertEqual(len(fixture['tenants']), 3)
        self.assertFalse({'acme', 'bravo'} & set(fixture['tenants']))
        self.assertNotIn(fixture['principal'], {'alice', 'bob'})

    def test_changed_source_or_labels_refused_before_database_dispatch(self):
        fixture = controller.load_fixture()
        fixture['documents'][0]['expected_answer'] = 'an unsupported claim'
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'fixture.json'
            target.write_text(json.dumps(fixture))
            with self.assertRaisesRegex(ValueError, 'fixture_changed'):
                controller.load_fixture(target)

    def test_plan_only_cannot_import_production_or_provision_even_with_semantic_argument(self):
        with patch.object(controller, 'provision', side_effect=AssertionError('database dispatch')), \
                patch.object(controller, 'run', side_effect=AssertionError('execution dispatch')), \
                redirect_stdout(io.StringIO()) as output:
            controller.main(['--services', '/missing/services.json', '--profile', '/missing/profile',
                '--output', '/missing/receipt.json', '--semantic-manifest', '/missing/manifest.json', '--plan-only'])
        value = json.loads(output.getvalue())
        self.assertEqual(value['database_calls'], 0)
        self.assertEqual(value['model_calls'], 0)
        self.assertTrue(value['execution_pending_exclusive_window'])

    def test_execution_refuses_foreign_directory_before_provision(self):
        with patch.object(controller.Path, 'cwd', return_value=Path('/synthetic-foreign-directory')), \
                patch.object(controller, 'fresh_profile', side_effect=AssertionError('profile mutation')), \
                patch.object(controller, 'run', side_effect=AssertionError('database dispatch')):
            with self.assertRaisesRegex(ValueError, 'installed_isolated_tmp_execution_required'):
                controller.main(['--services', '/missing/services.json', '--profile', '/missing/profile',
                    '--output', '/missing/receipt.json'])

    def test_private_receipts_are_exclusive_and_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'receipt.json'
            controller.private_json(path, {'success': False})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                controller.private_json(path, {'success': True})
            self.assertFalse(json.loads(path.read_text())['success'])

    def test_profile_refuses_foreign_founder_and_existing_owned_target(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(controller.Path, 'home', return_value=Path(tmp)):
            root = Path(tmp) / '.local/share/agentnetwork/enterprise-local/local-staging-readiness-v1'
            for path in (Path(tmp) / 'founder', root):
                with self.assertRaisesRegex(ValueError, 'owned_staging_operations_profile_required'):
                    controller.fresh_profile(path)
            target = root / 'new-operation'
            self.assertEqual(controller.fresh_profile(target), target.resolve())
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            with self.assertRaisesRegex(ValueError, 'target_exists'):
                controller.fresh_profile(target)

    def test_installed_operator_uses_isolated_interpreter_and_bounded_output(self):
        from types import SimpleNamespace
        result = SimpleNamespace(returncode=0, stdout=json.dumps({'result': {'ready': True}}))
        with patch.object(controller.subprocess, 'run', return_value=result) as run:
            self.assertEqual(controller.installed_cli('/tmp/synthetic-profile', 'readiness', 'org_72ce'), {'ready': True})
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], [controller.sys.executable, '-I', '-m', 'agenthub.cloud_local'])
        self.assertEqual(kwargs['cwd'], '/tmp')
        self.assertNotIn('PYTHONPATH', kwargs['env'])
        self.assertEqual(kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
