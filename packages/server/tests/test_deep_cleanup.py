"""Freeze removed alternative implementations without weakening live behavior."""
import importlib
import json
from pathlib import Path
import unittest

FIXTURE=Path(__file__).parent/'fixtures/deep_cleanup_v2/retired_symbols.json'

class RetiredImplementations(unittest.TestCase):
    def test_local_corpus_search_and_socket_service_are_absent(self):
        fixture=json.loads(FIXTURE.read_text())
        for name, symbols in fixture['retired_exports'].items():
            module=importlib.import_module(name)
            for symbol in symbols:
                with self.subTest(module=name,symbol=symbol):
                    self.assertFalse(hasattr(module,symbol),'Delete the old implementation; do not keep a forwarding shim.')

    def test_canonical_ingest_lifecycle_and_embedding_contract_remain(self):
        fixture=json.loads(FIXTURE.read_text())
        for name,symbols in fixture['retained_exports'].items():
            module=importlib.import_module(name)
            for symbol in symbols:self.assertTrue(callable(getattr(module,symbol)),symbol)
        from agenthub.processing.semantic import normalize_vector,prepare_document,prepare_query
        self.assertEqual(normalize_vector([3,4],2),[.6,.8])
        self.assertTrue(prepare_document('A cited fact').endswith('A cited fact'))
        self.assertTrue(prepare_query('Where is it?').endswith('Where is it?'))

class CanonicalCurationPolicy(unittest.TestCase):
    def test_default_is_continuous_durable_memory(self):
        from agenthub.processing.episode_pipeline import curator_version
        from agenthub.processing.durable_memory import VERSION
        self.assertEqual(curator_version({}),VERSION)
        self.assertEqual(curator_version({'episode_curation':{'policy':'durable_memory'}}),VERSION)

    def test_retired_policy_is_explicitly_rejected(self):
        from agenthub.processing.episode_pipeline import curator_version
        with self.assertRaisesRegex(ValueError,'retired_curation_policy'):
            curator_version({'episode_curation':{'policy':'episode'}})

    def test_new_candidates_require_validated_memory_record(self):
        from agenthub.processing.episode_pipeline import _observation
        with self.assertRaisesRegex(ValueError,'validated_memory_record_required'):
            _observation({'title':'Unvalidated old candidate'}, {})

class OneAuthority(unittest.TestCase):
    def test_both_store_entrypoints_use_the_same_indexed_search(self):
        from agenthub.postgres import PostgresEnterpriseStore
        from agenthub.cloud_runtime import CloudStore
        from agenthub.cloud_retrieval import HybridRetrievalMixin
        from agenthub.enterprise import EnterpriseStore
        self.assertIs(PostgresEnterpriseStore.search,HybridRetrievalMixin.search)
        self.assertIs(CloudStore.search,HybridRetrievalMixin.search)
        self.assertNotIn('search',EnterpriseStore.__dict__)

    def test_corpus_initializers_require_migrated_postgres(self):
        from unittest.mock import Mock
        import sqlite3
        names=('knowledge','episode_pipeline','temporal','metadata','context_delivery')
        for name in names:
            initialize=importlib.import_module('agenthub.processing.'+name).initialize
            db=Mock();initialize(db);db.require_schema.assert_called_once_with()
            with sqlite3.connect(':memory:') as old:
                with self.assertRaises(AttributeError):initialize(old)
