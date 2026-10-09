-- Stable meaningful occurrences survive current-claim revision changes.
-- Source and claim IDs are historical references, never read authorization.
CREATE TABLE episode_occurrences(
    occurrence_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    turn TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    UNIQUE(project,session,turn));

CREATE INDEX episode_occurrences_project_created
    ON episode_occurrences(project,created,occurrence_id);

CREATE TABLE episode_revisions(
    revision_id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL REFERENCES episode_occurrences(occurrence_id)
        DEFERRABLE INITIALLY DEFERRED,
    generation_id TEXT NOT NULL REFERENCES knowledge_generations(generation_id)
        DEFERRABLE INITIALLY DEFERRED,
    job_id TEXT NOT NULL REFERENCES curation_episode_jobs(id)
        DEFERRABLE INITIALLY DEFERRED,
    revision_number INTEGER NOT NULL CHECK(revision_number>0),
    previous_revision_id TEXT REFERENCES episode_revisions(revision_id)
        DEFERRABLE INITIALLY DEFERRED,
    source_hash TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    disposition TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at DOUBLE PRECISION NOT NULL,
    UNIQUE(occurrence_id,generation_id,revision_number));

CREATE INDEX episode_revisions_current
    ON episode_revisions(occurrence_id,generation_id,revision_number DESC);
CREATE INDEX episode_revisions_recorded
    ON episode_revisions(generation_id,recorded_at,revision_id);

CREATE TABLE episode_revision_evidence(
    revision_id TEXT NOT NULL REFERENCES episode_revisions(revision_id)
        DEFERRABLE INITIALLY DEFERRED,
    atom_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    PRIMARY KEY(revision_id,atom_key,source_id,segment_id));

CREATE INDEX episode_revision_evidence_source
    ON episode_revision_evidence(source_id,revision_id);

CREATE TABLE episode_revision_claim_links(
    revision_id TEXT NOT NULL REFERENCES episode_revisions(revision_id)
        DEFERRABLE INITIALLY DEFERRED,
    atom_key TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    claim_revision_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(revision_id,atom_key,claim_revision_id));

CREATE INDEX episode_revision_claim_links_document
    ON episode_revision_claim_links(document_id,claim_revision_id);
