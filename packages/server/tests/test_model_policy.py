"""Operator model allowlist: every model stage fails closed; nothing is a default."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agenthub.model_policy import ModelPolicy, ModelPolicyError, policy_from_settings, require_stage_allowed

MODEL = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'
BEDROCK = {'backend_execution': {'kind': 'bedrock', 'model': MODEL, 'region': 'us-east-1'}}
ALLOW_BEDROCK = {'allowed_providers': ['bedrock'], 'allowed_models': [MODEL], 'allowed_regions': ['us-east-1']}


def private_json(path, value):
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return path


class PolicyParsingTests(unittest.TestCase):
    def test_absent_policy_allows_nothing(self):
        policy = policy_from_settings({})
        self.assertEqual(policy.providers, frozenset())
        with self.assertRaisesRegex(ModelPolicyError, 'model_provider_not_allowlisted: stage curation provider bedrock'):
            require_stage_allowed(policy, BEDROCK, 'curation')
        with self.assertRaisesRegex(ModelPolicyError, 'model_provider_not_allowlisted'):
            require_stage_allowed(None, BEDROCK, 'curation')

    def test_invalid_policy_is_refused(self):
        for models in ({'allowed_providers': ['bedrock', 'some_vendor']}, {'allowed_providers': 'bedrock'},
                       {'allowed_providers': ['bedrock'], 'allowed_regions': ['US East']},
                       {'allowed_providers': ['bedrock', 'bedrock']}, {'default_provider': 'codex'},
                       {'allowed_providers': ['deterministic']}, []):
            with self.subTest(models=models), self.assertRaisesRegex(ModelPolicyError, 'runtime_models_configuration'):
                policy_from_settings({'models': models})

    def test_provider_model_and_region_are_each_enforced(self):
        policy = policy_from_settings({'models': ALLOW_BEDROCK})
        self.assertEqual(require_stage_allowed(policy, BEDROCK, 'reranking'), ('bedrock', MODEL, 'us-east-1'))
        other_model = {'backend_execution': dict(BEDROCK['backend_execution'], model='us.anthropic.other-model-v1:0')}
        with self.assertRaisesRegex(ModelPolicyError, 'model_not_allowlisted: stage reranking'):
            require_stage_allowed(policy, other_model, 'reranking')
        other_region = {'backend_execution': dict(BEDROCK['backend_execution'], region='eu-west-1')}
        with self.assertRaisesRegex(ModelPolicyError, 'model_region_not_allowlisted'):
            require_stage_allowed(policy, other_region, 'reranking')
        no_region = {'backend_execution': {'kind': 'bedrock', 'model': MODEL}}
        with self.assertRaisesRegex(ModelPolicyError, 'model_stage_region_required'):
            require_stage_allowed(policy, no_region, 'reranking')

    def test_stage_must_name_provider_and_model(self):
        policy = ModelPolicy(frozenset({'bedrock', 'codex', 'operator_api'}))
        for config, error in (({}, 'model_stage_provider_required'),
                              ({'observer': {'model': 'synthetic-model'}}, 'model_stage_provider_required'),
                              ({'backend_execution': {'kind': 'codex'}}, 'model_stage_model_required'),
                              ({'backend_execution': {'kind': 'operator_api'}}, 'model_stage_model_required'),
                              ({'backend_execution': {'kind': 'bedrock', 'region': 'us-east-1'}}, 'model_stage_model_required')):
            with self.subTest(config=config), self.assertRaisesRegex(ModelPolicyError, error):
                require_stage_allowed(policy, config, 'curation')


class StageFailClosedTests(unittest.TestCase):
    """One case per model stage: reranking, curation worker (observer + episode pipeline)."""

    def runtime(self, home, worker, models=None):
        settings = {'control_dsn': 'synthetic',
                    'reranking': {'enabled': True, 'worker_config': str(private_json(home/'ranker.json', worker)),
                                  'timeout_seconds': 20}}
        if models is not None:
            settings['models'] = models
        return settings

    def registry(self, settings, home):
        from agenthub.cloud_runtime import registry_from_settings
        class Registry:
            def __init__(self, dsn, home, max_stores, store_factory): self.store_factory = store_factory
        class Store:
            def __init__(self, *args, **kwargs): pass
        with patch('agenthub.cloud_runtime.CloudStore', Store), patch('agenthub.cloud_runtime.TenantRegistry', Registry), \
             patch('agenthub.cloud_ops.Meter'):
            registry = registry_from_settings(settings, home)
            return registry.store_factory(home, 'synthetic', 'org')

    def test_reranking_stage_fails_closed_at_api_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cases = [(None, 'model_provider_not_allowlisted: stage reranking provider bedrock'),
                     ({'allowed_providers': ['operator_api']}, 'model_provider_not_allowlisted'),
                     ({'allowed_providers': ['bedrock'], 'allowed_models': ['us.anthropic.other-v1:0']}, 'model_not_allowlisted'),
                     ({'allowed_providers': ['bedrock'], 'allowed_regions': ['us-west-2']}, 'model_region_not_allowlisted')]
            for models, error in cases:
                with self.subTest(models=models), self.assertRaisesRegex(ModelPolicyError, error):
                    self.registry(self.runtime(home, BEDROCK, models), home)
            # The legacy implicit form (no provider named) no longer selects Codex.
            legacy = self.runtime(home, {'observer': {'model': 'synthetic-model'}}, {'allowed_providers': ['codex']})
            with self.assertRaisesRegex(ModelPolicyError, 'model_stage_provider_required: stage reranking'):
                self.registry(legacy, home)
            store = self.registry(self.runtime(home, BEDROCK, ALLOW_BEDROCK), home)
            from agenthub.bedrock_execution import BedrockExecution
            self.assertIsInstance(store.serving_reranker.runner, BedrockExecution)
            self.assertIsNone(store.serving_reranker.config['observer']['reasoning'])
            self.assertIsNone(store.serving_reranker.ledger)

    def test_disabled_stages_need_no_allowlist_and_bad_policy_still_fails_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = self.registry({'control_dsn': 'synthetic'}, home)
            self.assertFalse(hasattr(store, 'serving_reranker'))
            with self.assertRaisesRegex(ModelPolicyError, 'runtime_models_configuration'):
                self.registry({'control_dsn': 'synthetic', 'models': {'allowed_providers': ['openai']}}, home)

    def test_reranker_constructed_without_policy_fails_closed(self):
        from agenthub.cloud_execution import ExecutionError
        from agenthub.serving_reranker import ServingReranker
        with tempfile.TemporaryDirectory() as directory, \
             self.assertRaisesRegex(ExecutionError, 'model_provider_not_allowlisted: stage reranking'):
            ServingReranker(directory, BEDROCK, meter=object())

    def test_curation_worker_fails_closed_before_indexing_or_dispatch(self):
        from agenthub import cloud_local
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            private_json(profile/'worker-config.json', BEDROCK)
            class Store:
                curation_enabled = True
                semantic_embedder = None
            class Registry:
                _stores = {}
                def resolve(self, tenant): return Store()
            for models, error in ((None, 'model_provider_not_allowlisted: stage curation'),
                                  ({'allowed_providers': ['bedrock'], 'allowed_models': ['other']}, 'model_not_allowlisted')):
                settings = {} if models is None else {'models': models}
                with self.subTest(models=models), \
                     patch.object(cloud_local, 'runtime', return_value=(settings, Registry())), \
                     patch('agenthub.source_index.SourceIndex', side_effect=AssertionError('indexed before policy')), \
                     patch('agenthub.cloud_execution.execution_from_config', side_effect=AssertionError('dispatch built')), \
                     patch('atexit.register'), \
                     self.assertRaisesRegex(ModelPolicyError, error):
                    cloud_local.main(['worker-run', '--profile', str(profile), '--live'])

    def test_curation_worker_without_runner_requires_policy(self):
        from agenthub.cloud_execution import ExecutionError, execution_from_config
        with self.assertRaisesRegex(ExecutionError, 'model_provider_not_allowlisted: stage curation'):
            execution_from_config(BEDROCK)
        from agenthub.bedrock_execution import BedrockExecution
        runner = execution_from_config(BEDROCK, policy=policy_from_settings({'models': ALLOW_BEDROCK}))
        self.assertIsInstance(runner, BedrockExecution)
        self.assertEqual((runner.model, runner.region), (MODEL, 'us-east-1'))


class NoDefaultProviderTests(unittest.TestCase):
    def test_live_worker_config_names_no_provider_or_model(self):
        from agenthub.cloud_local import worker_config
        from agenthub.cloud_execution import execution_identity, execution_kind, execution_model
        with tempfile.TemporaryDirectory() as directory:
            live = worker_config(directory, provider_free=False)
            self.assertNotIn('backend_execution', live)
            self.assertNotIn('model', live['observer'])
            self.assertIsNone(execution_kind(live)); self.assertIsNone(execution_model(live))
            fixture = worker_config(directory, provider_free=True)
            self.assertEqual(fixture['backend_execution'], {'kind': 'deterministic'})
        self.assertNotEqual(execution_identity({}), execution_identity(
            {'backend_execution': {'kind': 'codex'}, 'observer': {'model': 'synthetic-model'}}))

    def test_explicit_codex_and_operator_api_remain_available_when_allowlisted(self):
        from agenthub.cloud_execution import ApiExecution, CodexExecution, execution_from_config
        codex = {'backend_execution': {'kind': 'codex'}, 'observer': {'model': 'synthetic-model'}}
        self.assertIsInstance(execution_from_config(codex, policy=ModelPolicy(frozenset({'codex'}))), CodexExecution)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory)/'key'; key.write_text('synthetic-not-live'); key.chmod(0o600)
            api = {'backend_execution': {'kind': 'operator_api', 'model': 'synthetic-model', 'credential_file': str(key)}}
            self.assertIsInstance(execution_from_config(api, policy=ModelPolicy(frozenset({'operator_api'}))), ApiExecution)
            from agenthub.cloud_execution import ExecutionError
            with self.assertRaisesRegex(ExecutionError, 'model_provider_not_allowlisted'):
                execution_from_config(api, policy=ModelPolicy(frozenset({'bedrock'})))

    def test_codex_harness_has_no_built_in_model(self):
        from agenthub.processing.harness import HarnessError, _invoke
        with patch('subprocess.run', side_effect=AssertionError('harness started')), \
             patch('subprocess.Popen', side_effect=AssertionError('harness started')), \
             self.assertRaisesRegex(HarnessError, 'model_required'):
            _invoke('.', {'observer': {'executable': 'codex'}}, 'i', {}, {})

    def test_installed_service_code_has_no_built_in_external_model(self):
        import agenthub
        root = Path(agenthub.__file__).parent
        offenders = [str(p.relative_to(root)) for p in root.rglob('*.py') if 'gpt-6-luna' in p.read_text()]
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
