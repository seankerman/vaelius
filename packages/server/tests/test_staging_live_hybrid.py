"""Frozen preflight contract: lexical smoke cannot be labeled hybrid delivery."""
import unittest
from unittest.mock import MagicMock

from agenthub.processing.semantic import MODEL_KEY


class HybridLiveContractTests(unittest.TestCase):
    def helper(self):
        from vaelius_test_support.hub.cloud_live import prepare_hybrid_delivery
        return prepare_hybrid_delivery

    def settings(self):
        return {'semantic': {'enabled': True, 'accounting_root': '/private/existing-campaign'}}

    def store(self):
        store = MagicMock()
        store.hybrid_enabled = True
        store.semantic_model_key = MODEL_KEY
        store.semantic_embedder.model_key = MODEL_KEY
        store.reindex_vectors.return_value = {'model_key': MODEL_KEY, 'dimension': 512,
            'generation': 'vectors-synthetic', 'documents': 2, 'search': 'exact_pgvector'}
        state = store.open.return_value.__enter__.return_value
        state.db.execute.return_value.fetchone.return_value = {
            'generation_id': 'vectors-synthetic', 'model_key': MODEL_KEY, 'status': 'active'}
        return store

    def test_semantic_off_does_not_dispatch_or_claim_hybrid(self):
        store = self.store()
        with self.assertRaisesRegex(ValueError, 'hybrid_live_profile_required'):
            self.helper()(store, {'semantic': {'enabled': False}})
        store.reindex_vectors.assert_not_called()

    def test_shared_accounting_required_before_embedding(self):
        store = self.store()
        with self.assertRaisesRegex(ValueError, 'hybrid_live_profile_required'):
            self.helper()(store, {'semantic': {'enabled': True}})
        store.reindex_vectors.assert_not_called()

    def test_actual_generation_identity_checked_after_atomic_activation(self):
        store = self.store()
        result = self.helper()(store, self.settings())
        self.assertEqual(result['generation'], 'vectors-synthetic')
        store.reindex_vectors.assert_called_once_with(max_documents=10000)

    def test_wrong_model_or_generation_cannot_claim_hybrid(self):
        for mismatch in ('model', 'generation'):
            store = self.store()
            if mismatch == 'model':
                store.semantic_embedder.model_key = 'synthetic-onehot'
            else:
                store.open.return_value.__enter__.return_value.db.execute.return_value.fetchone.return_value = {
                    'generation_id': 'old', 'model_key': MODEL_KEY, 'status': 'active'}
            with self.assertRaises(ValueError):
                self.helper()(store, self.settings())


if __name__ == '__main__':
    unittest.main()
