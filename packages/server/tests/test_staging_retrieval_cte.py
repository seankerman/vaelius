"""The frozen policy scenarios through complete CTE-authorized ranking.

Uses the already frozen staging source/dependency fixtures and canonical oracle.
"""
import unittest

from test_staging_retrieval_sql import CompletePolicyEquivalence


class AuthorizedCtePolicy(CompletePolicyEquivalence):
    def compare_full(self, *, expected=None, contexts=None):
        super().compare_full(expected=expected, contexts=contexts)
        self.store.retrieval_authorization_shape = 'authorized_cte'
        for who, ctx in (contexts or self.contexts).items():
            with self.subTest(principal=who), self.store.open() as state:
                oracle = {}
                for row in state.db.execute('SELECT id FROM enterprise_documents WHERE tenant=?', ('acme',)):
                    doc = self.store._document_allowed(state.db, ctx, row[0])
                    if doc:
                        oracle[row[0]] = doc['active_revision_id']
                ids = self.store._authorized_documents(state, ctx)
                actual = {ident: state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?', (ident,)).fetchone()[0] for ident in ids}
                self.assertEqual(actual, oracle)
            # Exercise actual ranked CTE statements; all selected revisions must
            # belong to the complete current authorized set, not a global top-k.
            for query in ('gauge', 'owner-a gauge reference'):
                for candidate in self.store.candidates(ctx, query, vector=False, limit=100):
                    self.assertIn(candidate['document_id'], oracle)
                    self.assertEqual(candidate['revision_id'], oracle[candidate['document_id']])


if __name__ == '__main__':
    unittest.main()
