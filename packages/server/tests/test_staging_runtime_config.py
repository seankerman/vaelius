"""The serving runtime applies the identified retrieval configuration."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch


class RuntimeConfigurationTests(unittest.TestCase):
    def test_reranker_is_internal_opt_in_and_startup_does_not_dispatch(self):
        from agenthub.cloud_runtime import registry_from_settings
        class Registry:
            def __init__(self, dsn, home, max_stores, store_factory):
                self.store_factory = store_factory
        class Store:
            def __init__(self, *args, **kwargs): pass
        with tempfile.TemporaryDirectory() as directory, \
             patch('agenthub.cloud_runtime.CloudStore', Store), \
             patch('agenthub.cloud_runtime.TenantRegistry', Registry), \
             patch('agenthub.processing.harness.run_structured') as provider, \
             patch('agenthub.cloud_ops.Meter'):
            home = Path(directory)
            worker = home/'worker.json'
            worker.write_text(json.dumps({'observer': {'model': 'gpt-6-luna'}}))
            worker.chmod(0o600)
            settings = {'control_dsn': 'synthetic'}
            factory = registry_from_settings(settings, home).store_factory
            store=factory(home, 'synthetic', 'org')
            self.assertFalse(hasattr(store, 'serving_reranker'))
            self.assertFalse(store.curation_enabled)
            self.assertEqual(store.retrieval_corpus,'sources')
            settings['reranking'] = {'enabled': True, 'worker_config': str(worker),
                                    'ledger': str(home/'shared.sqlite'), 'timeout_seconds': 30}
            factory = registry_from_settings(settings, home).store_factory
            ranker = factory(home, 'synthetic', 'org').serving_reranker
            self.assertEqual(ranker.config['observer']['model'], 'gpt-6-luna')
            self.assertEqual(ranker.config['observer']['reasoning'], 'low')
            self.assertEqual(ranker.timeout_seconds, 30)
            provider.assert_not_called()
            worker.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'reranker_private_configuration'):
                registry_from_settings(settings, home)

    def test_serving_factory_keeps_admitted_selection_and_current_authorization(self):
        from agenthub.cloud_runtime import registry_from_settings
        from agenthub.retrieval_embedding_cache import CampaignEmbedder
        # Config4 was admitted before semantic implementation or measured search.
        config = {'authorization_shape': 'authorized_cte', 'selection_policy': 'facets_v1',
                  'numeric_mode': 'typed', 'candidate_limit': 20, 'lexical_weight': 1,
                  'vector_weight': 1, 'require_prose': False, 'vector': True}
        made = []
        class Store:
            def __init__(self, path, dsn, tenant, registry=None):
                made.append(self)
        class Registry:
            def __init__(self, dsn, home, max_stores, store_factory):
                self.store_factory = store_factory
        with tempfile.TemporaryDirectory() as directory, \
             patch('agenthub.cloud_runtime.CloudStore', Store), \
             patch('agenthub.cloud_runtime.TenantRegistry', Registry):
            registry = registry_from_settings({'control_dsn': 'synthetic', 'retrieval': config}, Path(directory))
            store = registry.store_factory(Path(directory)/'tenant', 'synthetic', 'org-73', registry)
            self.assertEqual(store.retrieval_authorization_shape, 'compiled')
            self.assertEqual(store.retrieval_selection_policy, 'facets_v1')
            self.assertEqual(store.retrieval_numeric_mode, 'typed')
            self.assertEqual(store.retrieval_candidate_limit, 20)
            self.assertFalse(store.hybrid_enabled)  # No model enabled merely by policy configuration.
            self.assertIsNone(store.semantic_embedder)

    def test_invalid_runtime_policy_is_rejected_before_tenant_resolution(self):
        from agenthub.cloud_runtime import registry_from_settings
        for config in ({'authorization_shape': 'global_topk'}, {'candidate_limit': 100000},
                       {'selection_policy': 'paid_reranker'}, {'model': 'customer-choice'},
                       {'vector': 'yes'}):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, 'runtime_retrieval_configuration'):
                registry_from_settings({'control_dsn': 'unused', 'retrieval': config}, Path('/tmp/unused'))


if __name__ == '__main__':
    unittest.main()
