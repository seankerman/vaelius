"""A small answer corpus can have a large authorized evidence history.

The noise rows are synthetic policy metadata, not an ingestion benchmark.
Ranking must remain bounded and denial must still invalidate a mixed document.
"""
import time
import unittest

from test_staging_retrieval_sql import CompletePolicyEquivalence


class LargeSourceScope(CompletePolicyEquivalence):
    def test_large_evidence_scope_rank_and_current_denial(self):
        source, document = self.note('copper-evidence', visibility='team')
        private, private_document = self.note('private-evidence')
        with self.store.open() as state, state.db:
            # Replicate only the accepted source's policy fields. These rows
            # are deliberately irrelevant to the two supported documents.
            state.db.execute("""INSERT INTO enterprise_sources
                (id,tenant,owner,external_project,internal_project,external_id,
                 enrollment,payload_hash,visibility,raw_visibility,active,
                 policy_version,source_version,occurred_at,created,
                 occurred_precision,occurred_timezone)
                SELECT md5('large-source-fixture-' || n::text),tenant,owner,
                 external_project,internal_project,'noise-' || n::text,
                 enrollment,payload_hash,visibility,raw_visibility,active,
                 policy_version,source_version,occurred_at,created,
                 occurred_precision,occurred_timezone
                FROM enterprise_sources CROSS JOIN generate_series(1,55000) n
                WHERE id=?""", (source,))
        self.store.retrieval_authorization_shape = 'authorized_cte'
        started = time.monotonic()
        found = self.store.candidates(self.contexts['alice'], 'copper gauge', vector=False)
        self.assertIn(document, {row['document_id'] for row in found})
        self.assertLess(time.monotonic() - started, 2.0)
        with self.store.open() as state, state.db:
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)', (document, private))
        denied = self.store.candidates(self.contexts['bob'], 'copper gauge', vector=False)
        self.assertNotIn(document, {row['document_id'] for row in denied})
        self.assertNotIn(private_document, {row['document_id'] for row in denied})


if __name__ == '__main__':
    unittest.main()
