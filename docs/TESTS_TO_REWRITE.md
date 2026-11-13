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
| `test_cloud_recovery.LogicalRecoveryPostgresTests.test_populated_claim_temporal_and_episode_history_survive_offline_reconcile` | Temporal assertions and episode history survived an offline restore (it needed a recovery profile and never ran in CI). |

## Tests kept with a removed path taken out

- `test_workspace_access.RecallFilterPathTests.test_a_filter_never_widens_what_a_session_may_read`
  and `test_the_filter_costs_only_its_own_resolution`: the dated-search assertions
  are gone; the row-level-security, three-layer and candidate assertions are unchanged.
- `test_staging_history.NativeHistoryTests.test_current_grant_revocation_denies_search_detail_and_bytes`:
  asks a current question instead of a dated one; revocation still has to deny
  search, detail and original bytes.

## The curated corpus and the unused tables

Removing the curated search path and its tables (CHANGELOG, "Removed") deleted
the tests of that path and moved the visibility probe of the access and privacy
tests from a reviewed note to a captured chat passage. No test was turned off.

Deleted with the code they tested (13 files): `test_candidate_selection`,
`test_cloud_answerability_v2`, `test_cloud_compound_anaphor`,
`test_cloud_facet_selection`, `test_cloud_negative_trial`,
`test_cloud_receiver_query`, `test_contact_answerability`,
`test_curated_speaker_selection`, `test_history_retrieval_conflict_types`,
`test_history_retrieval_conflicts`, `test_staging_selection`,
`test_staging_selection_postgres`, `test_staging_selection_v2`.

| Test removed from a file that stays | What it covered |
| --- | --- |
| `test_cloud_retrieval.SupportedAnswers` (1 test) and `HybridPolicyTests.test_location_and_reason_require_both_facets_and_specific_subject` | Answer selection and compound location-and-reason questions. |
| `test_retrieval_alternatives.Alternatives.test_heading_only_passage_is_not_a_fact_when_prose_guard_enabled` | The prose guard of card selection. |
| `test_knowledge...test_explicit_version_platform_unit_date_and_scope_conflicts_are_excluded` | The applicability filter of card selection. |
| `test_retrieval_evaluation.EvaluationTests.test_model_judge_denied` | The answerability-judge hook. |
| `test_cloud_ops` (6 of 7 tests) and `test_cloud_maintenance.MaintenanceTests.test_unknown_usage_reconciles_one_attempt_and_admission_is_backend_owned` | Model-attempt admission, reservation, reconciliation and price estimates. |
| `test_cloud_refresh.BatchedRefreshPostgresTests.test_episode_related_and_backend_dependencies_are_all_retained` | Curation-job sources in the document refresh (tables dropped). |

Kept with a removed part taken out:

- `test_cloud_identity_postgres...test_pending_observer_preference_never_delivers_shared_derived_claim`:
  the observer that marked candidates is gone, so the pending marker is planted;
  that it withholds the document from everyone is still asserted. The private
  promotion that followed went with the observer code.
- `test_general_sources...test_v2_restore_purges_saved_outputs_and_retains_current_denials`
  and `test_data_lifecycle` (`curation_rows`): no longer seed or scan the dropped
  curation tables. Every remaining table is still scanned for deleted content.
- `test_backend_retrieval_v2...test_reviewed_release_preserves_raw_privacy_and_revocation`,
  `test_memory_governance...test_owner_correction_replaces_poisoned_text_for_other_readers_immediately`
  and the two derived-document tests of `test_staging_retrieval_sql`: a release and a
  correction in place exist only for reviewed notes, so they are asserted by opening
  the note by id; the correction test also asserts recall on a corrected capture.

## Coverage worth writing again

- `vaelius_test_support.hub.cloud_load` (the volume rehearsal behind
  `test_cloud_load` and `test_cloud_faults`, which need a private parent profile
  and never run in CI) seeds curated-representation rows and counts `answerable`
  results. Search now reads passages only, so the rehearsal finds nothing until
  its seeding is moved to captured passages.

- `processing/media_projection` (kept: source indexing and evidence use it) lost its
  direct unit tests, which ran through the curator's episode packet
  (`test_media_projection`, deleted). It is now covered only through
  `test_raw_default.RawDefaultTests.test_redaction_media_gap_and_policy_change_preserve_boundaries`.
- A recall-quality test over raw passages can reuse
  `packages/server/research/vaelius_test_support/hub/retrieval_evaluation.py`.
