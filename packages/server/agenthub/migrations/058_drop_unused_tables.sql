-- Drop the tables nothing writes any more.
--
-- Model curation, the Slack connector and model-attempt accounting were removed
-- (CHANGELOG.md). Their tables had no writer left: curation jobs and the worker,
-- observers, episodes, curated temporal assertions, Slack, model calls and usage
-- attempts. Indexes, constraints and sequences that belong to them go with them;
-- no function, row-level-security policy, trigger or view refers to any of them,
-- and no kept table has a foreign key into them, so the statement needs no CASCADE
-- and fails rather than remove anything else.
--
-- Deliberately kept, although also without a writer: cloud_preference_candidates,
-- knowledge_generations and knowledge_generation_documents. The read predicate
-- (search_document_allowed) reads all three and is unchanged by this migration;
-- knowledge_generation_state stays with them.
DROP TABLE IF EXISTS
    backend_jobs,
    backend_model_input_snapshots,
    backend_observer_dependencies,
    backend_observers,
    backend_processing_dependencies,
    backend_provider_returns,
    backend_slack_connectors,
    backend_slack_events,
    backend_slack_items,
    backend_slack_limits,
    backend_worker_receipts,
    cloud_admission,
    cloud_usage_attempts,
    cloud_usage_reconciliations,
    curation_episode_jobs,
    curation_episode_receipts,
    enterprise_model_inputs,
    episode_candidate_history,
    episode_candidates,
    episode_occurrences,
    episode_revision_claim_links,
    episode_revision_evidence,
    episode_revisions,
    knowledge_temporal_assertions,
    knowledge_temporal_evidence,
    knowledge_temporal_relations,
    memory_metadata,
    model_calls;
