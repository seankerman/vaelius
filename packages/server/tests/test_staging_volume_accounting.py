"""Continuation reuses accounting/cache, without restoring removed time quotas."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from agenthub.retrieval_embedding_cache import CampaignEmbedder

class Model:
    model_key='staging-accounting-fixture:512'
    dimension=512
    def embed_documents(self,texts):
        return [[1.0 if i==int(hashlib.sha256(t.encode()).hexdigest(),16)%512 else 0.0 for i in range(512)] for t in texts]

class VolumeAccountingTests(unittest.TestCase):
    def test_new_run_reuses_prior_cache_and_accumulates_usage_without_a_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);shared=root/'existing';shared.mkdir(mode=0o700)
            owned=root/'new';owned.mkdir(mode=0o700)
            existing=CampaignEmbedder(Model(),shared,max_embedding_seconds=1)
            original=existing.embed_documents(['verified earlier derivative'])
            continued=CampaignEmbedder(Model(),owned,accounting_root=shared)
            self.assertEqual(continued.embed_documents(['verified earlier derivative']),original)
            continued.embed_documents(['new derivative'])
            self.assertFalse((owned/'embedding-derivative/cache.sqlite').exists())
            self.assertEqual(existing.stats()['model_calls'],2)
            self.assertIsNone(existing.stats()['max_embedding_seconds'])
