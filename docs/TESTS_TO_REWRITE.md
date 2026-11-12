# Tests to rewrite after the trim

The trim to the core product ([CHANGELOG](../CHANGELOG.md), "Removed") deleted the
tests of removed features together with their code. This file lists core tests that
were turned off with `unittest.skip("quarantined: needs rewrite after trim")`
(test id, one line on what it covered), and coverage that went away with removed
code and is worth writing again.

## Quarantined

None. Every core test that leaned on removed code was fixed in a few lines.

## Tests removed from files that stay

Each exercised only a removed feature.

| Test | What it covered |
| --- | --- |
| `test_workspace_access.RecallFilterPathTests.test_curated_temporal_recall_honours_the_filter` | The `read` filter on dated search over curated assertions (path removed). |
| `test_search_reader.SearchReader.test_operator_review_cannot_expand_to_unselected_documents` | Operator draft review could not widen the search reader (draft review removed). |
| `test_raw_default.RawDefaultTests.test_enrichment_is_disabled_by_runtime_default` | The curation worker stayed idle by default (worker removed). |
| `test_raw_default.RawDefaultTests.test_optional_ranker_keeps_long_raw_evidence_discoverable` | Long raw passages stayed discoverable behind the model reranker (reranker removed). |
| `test_cloud_identity_postgres.IdentityPostgresTests.test_actual_worker_promotes_ingest_only_captured_preference_privately` | The curation worker promoted a captured preference privately (worker removed). |
| `test_backend_retrieval_v2` (2 tests) | Dated answers and typed validity from curated temporal assertions. |
| `test_staging_history.NativeHistoryTests` (3 tests) | Explicit validity dates read from original documents, and their repair. |
| `test_cloud_maintenance.MaintenanceTests.test_hold_fences_running_claim_and_retry_is_explicit_and_model_free` | Operator hold and retry of curation jobs. |
| `test_deep_cleanup.CanonicalCurationPolicy` (3 tests) | The curator policy version and its validated-record rule. |

## Tests kept with a removed path taken out

- `test_workspace_access.RecallFilterPathTests.test_a_filter_never_widens_what_a_session_may_read`
  and `test_the_filter_costs_only_its_own_resolution`: the dated-search assertions
  are gone; the row-level-security, three-layer and candidate assertions are unchanged.
- `test_staging_history.NativeHistoryTests.test_current_grant_revocation_denies_search_detail_and_bytes`:
  asks a current question instead of a dated one; revocation still has to deny
  search, detail and original bytes.

## Coverage worth writing again

- `processing/media_projection` (kept: source indexing and evidence use it) lost its
  direct unit tests, which ran through the curator's episode packet
  (`test_media_projection`, deleted). It is now covered only through
  `test_raw_default.RawDefaultTests.test_redaction_media_gap_and_policy_change_preserve_boundaries`.
- A recall-quality test over raw passages can reuse
  `packages/server/research/vaelius_test_support/hub/retrieval_evaluation.py`.
