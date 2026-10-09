"""Finite local model work and verified derivative reuse across restarts."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agenthub.retrieval_embedding_cache import CampaignEmbedder, EmbeddingBudgetExceeded


class FakeModel:
    model_key = 'fixture-model-a:512'
    dimension = 512

    def __init__(self):
        self.documents = []
        self.queries = []
        self.fail = False

    def _vectors(self, texts):
        if self.fail:
            raise RuntimeError('fixture_failed_embedding')
        return [[1.0 if position == int(hashlib.sha256(text.encode()).hexdigest(), 16) % 512 else 0.0
                 for position in range(512)] for text in texts]

    def embed_documents(self, texts):
        self.documents.append(list(texts))
        return self._vectors(texts)

    def embed_queries(self, texts):
        self.queries.append(list(texts))
        return self._vectors(texts)


class CampaignEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model = FakeModel()

    def tearDown(self):
        self.temporary.cleanup()

    def test_restart_reuses_verified_document_vectors_and_not_queries(self):
        first = CampaignEmbedder(self.model, self.root)
        expected = first.embed_documents([' durable fact ', 'durable fact', 'second fact'])
        self.assertEqual(len(self.model.documents), 1)
        self.assertEqual(self.model.documents[0], [' durable fact ', 'second fact'])
        self.assertEqual(expected[0], expected[1])
        replacement = FakeModel()
        second = CampaignEmbedder(replacement, self.root)
        self.assertEqual(second.embed_documents(['durable fact', 'second fact']), expected[1:])
        self.assertEqual(replacement.documents, [])
        second.embed_queries(['where is the fact?'])
        second.embed_queries(['where is the fact?'])
        self.assertEqual(len(replacement.queries), 2)
        statistics = second.stats()
        self.assertEqual(statistics['model_calls'], 3)
        self.assertEqual(statistics['query_calls'], 2)
        self.assertTrue(statistics['derivative_cache'])
        self.assertEqual(statistics['provider_calls'], 0)
        self.assertEqual(statistics['cache_entries'], 2)
        self.assertEqual(first.dimension, 512)
        self.assertEqual(first.model_key, self.model.model_key)

    def test_corrupt_packed_vector_is_recomputed_and_model_or_text_invalidates(self):
        cache = CampaignEmbedder(self.model, self.root)
        expected = cache.embed_documents(['first durable fact'])
        with sqlite3.connect(cache.cache_path) as db:
            db.execute('UPDATE embeddings SET vector=?', (b'bad',))
        self.assertEqual(cache.embed_documents(['first durable fact']), expected)
        self.assertEqual(len(self.model.documents), 2)
        self.assertEqual(cache.stats()['invalid_cache_entries'], 1)
        cache.embed_documents(['changed durable fact'])
        self.model.model_key = 'fixture-model-b:512'
        cache.embed_documents(['first durable fact'])
        self.assertEqual(len(self.model.documents), 4)
        self.assertEqual(cache.stats()['cache_entries'], 3)
        self.assertAlmostEqual(sum(v*v for v in expected[0]), 1.0)

    def test_shape_failure_and_failed_model_call_charge_cumulative_time(self):
        cache = CampaignEmbedder(self.model, self.root, max_embedding_seconds=2)
        self.model.fail = True
        with patch('agenthub.retrieval_embedding_cache.time.monotonic', side_effect=[10.0, 12.5]):
            with self.assertRaisesRegex(RuntimeError, 'fixture_failed_embedding'):
                cache.embed_documents(['failure'])
        replacement = CampaignEmbedder(FakeModel(), self.root, max_embedding_seconds=3600)
        statistics = replacement.stats()
        self.assertEqual(statistics['max_embedding_seconds'], 2)
        self.assertEqual(statistics['embedding_seconds'], 2.5)
        self.assertEqual(statistics['failed_calls'], 1)
        self.assertEqual(statistics['remaining_seconds'], 0)
        with self.assertRaises(EmbeddingBudgetExceeded):
            replacement.embed_queries(['cannot reset the budget'])
        self.assertEqual(replacement.model.queries, [])
        # Already verified cache hits remain usable without model work after exhaustion.
        self.assertEqual(replacement.embed_documents([]), [])

    def test_invalid_output_shape_is_charged_without_cache_activation(self):
        cache = CampaignEmbedder(self.model, self.root)
        with patch.object(self.model, 'embed_documents', return_value=[[0.0, 1.0]]):
            with self.assertRaisesRegex(ValueError, 'semantic_dimension_mismatch'):
                cache.embed_documents(['malformed vector'])
        self.assertEqual(cache.stats()['failed_calls'], 1)
        self.assertEqual(cache.stats()['cache_entries'], 0)
        with patch.object(self.model, 'embed_queries', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'embedding_cache_batch_shape'):
                cache.embed_queries(['missing vector'])
        self.assertEqual(cache.stats()['failed_calls'], 2)
        self.assertEqual(cache.stats()['query_calls'], 1)

    def test_verified_cached_hit_remains_usable_after_budget_exhaustion(self):
        cache = CampaignEmbedder(self.model, self.root, max_embedding_seconds=1)
        with patch('agenthub.retrieval_embedding_cache.time.monotonic', side_effect=[10.0, 11.5]):
            expected = cache.embed_documents(['already learned'])
        self.assertEqual(cache.embed_documents(['already learned']), expected)
        self.assertEqual(len(self.model.documents), 1)
        with self.assertRaises(EmbeddingBudgetExceeded):
            cache.embed_documents(['not yet learned'])
        with self.assertRaises(EmbeddingBudgetExceeded):
            cache.embed_queries(['must actually compute'])
        self.assertEqual(cache.stats()['model_calls'], 1)

    def test_interrupted_reservation_cannot_reset_time_on_restart(self):
        cache = CampaignEmbedder(self.model, self.root, max_embedding_seconds=3)
        with sqlite3.connect(cache.cache_path) as db:
            db.execute('UPDATE campaign SET active_started_at=100,active_kind=?', ('document',))
        with patch('agenthub.retrieval_embedding_cache.time.time', return_value=105):
            replacement = CampaignEmbedder(FakeModel(), self.root, max_embedding_seconds=3)
        statistics = replacement.stats()
        self.assertEqual(statistics['embedding_seconds'], 5)
        self.assertEqual(statistics['uncertain_calls'], 1)
        self.assertEqual(statistics['remaining_seconds'], 0)
        with self.assertRaises(EmbeddingBudgetExceeded):
            replacement.embed_documents(['new work'])

    def test_clock_reversal_after_interruption_consumes_uncertain_remaining_budget(self):
        cache = CampaignEmbedder(self.model, self.root, max_embedding_seconds=3)
        with sqlite3.connect(cache.cache_path) as db:
            db.execute('UPDATE campaign SET used_seconds=1,active_started_at=105,active_kind=?', ('query',))
        with patch('agenthub.retrieval_embedding_cache.time.time', return_value=100):
            statistics = CampaignEmbedder(FakeModel(), self.root, max_embedding_seconds=3).stats()
        self.assertEqual(statistics['embedding_seconds'], 3)
        self.assertEqual(statistics['remaining_seconds'], 0)
        self.assertEqual(statistics['uncertain_calls'], 1)

    def test_cache_is_private_contains_hashes_not_source_text_and_invalid_input_is_rejected(self):
        cache = CampaignEmbedder(self.model, self.root)
        cache.embed_documents(['private fixture payload marker'])
        self.assertEqual(cache.cache_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cache.cache_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(b'private fixture payload marker', cache.cache_path.read_bytes())
        with self.assertRaises(ValueError):
            cache.embed_documents([''])
        for invalid in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                CampaignEmbedder(self.model, self.root, max_embedding_seconds=invalid)
        cache.model.dimension = 768
        with self.assertRaises(ValueError):
            cache.embed_documents(['wrong dimension'])

    def test_two_processes_share_one_model_call_and_budget(self):
        CampaignEmbedder(self.model, self.root)
        program = '''
import hashlib,json,sys,time
from agenthub.retrieval_embedding_cache import CampaignEmbedder
class Model:
 model_key='fixture-model-a:512'
 dimension=512
 def embed_documents(self,texts):
  time.sleep(0.1)
  return [[1.0]+[0.0]*511 for text in texts]
 def embed_queries(self,texts):raise AssertionError('queries not requested')
cache=CampaignEmbedder(Model(),sys.argv[1])
cache.embed_documents(['shared process fact'])
print(json.dumps(cache.stats()))
'''
        processes = [subprocess.Popen([sys.executable, '-c', program, str(self.root)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(json.loads(out)['cache_entries'], 1)
        statistics = CampaignEmbedder(self.model, self.root).stats()
        self.assertEqual(statistics['model_calls'], 1)
        self.assertEqual(statistics['cache_entries'], 1)
        self.assertGreater(statistics['embedding_seconds'], 0)


if __name__ == '__main__':
    unittest.main()

class SharedAccountingTests(unittest.TestCase):
    def test_distinct_run_roots_share_budget_cache_and_uncertain_reservation(self):
        from agenthub.retrieval_embedding_cache import resolve_shared_accounting_root
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            old = CampaignEmbedder(FakeModel(), shared, max_embedding_seconds=3)
            old.embed_documents(['verified old vector'])
            first_model, second_model = FakeModel(), FakeModel()
            first = CampaignEmbedder(first_model, shared/'new-run-a', max_embedding_seconds=3, accounting_root=shared)
            second = CampaignEmbedder(second_model, shared/'new-run-b', max_embedding_seconds=3, accounting_root=shared)
            self.assertEqual(first.directory, old.directory)
            self.assertEqual(second.directory, old.directory)
            self.assertEqual(first.embed_documents(['verified old vector']), old.embed_documents(['verified old vector']))
            self.assertFalse(first_model.documents)
            self.assertFalse((shared/'new-run-a'/'embedding-derivative').exists())
            with sqlite3.connect(old.cache_path) as db:
                db.execute('UPDATE campaign SET active_started_at=100,active_kind=?', ('query',))
            with patch('agenthub.retrieval_embedding_cache.time.time', return_value=104):
                recovered = CampaignEmbedder(second_model, shared/'new-run-c', max_embedding_seconds=3, accounting_root=shared)
            self.assertGreaterEqual(recovered.stats()['embedding_seconds'], 4)
            self.assertEqual(recovered.stats()['uncertain_calls'], 1)
            with self.assertRaises(EmbeddingBudgetExceeded):
                first.embed_queries(['no fresh allocation'])
            self.assertEqual(resolve_shared_accounting_root(shared, model_key=FakeModel.model_key), shared.resolve())
            with self.assertRaisesRegex(ValueError, 'model_mismatch'):
                resolve_shared_accounting_root(shared, model_key='wrong-model')

    def test_explicit_shared_root_cannot_create_fresh_campaign(self):
        from agenthub.retrieval_embedding_cache import resolve_shared_accounting_root
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'cache_required'):
                resolve_shared_accounting_root(root)
            self.assertFalse((root/'embedding-derivative').exists())
            with self.assertRaisesRegex(ValueError, 'root_invalid'):
                resolve_shared_accounting_root('relative-path')


if __name__ == "__main__":
    unittest.main()
