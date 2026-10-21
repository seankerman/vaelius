-- Data lifecycle governance: operator retention policy, legal holds, deferred
-- object purges and content-free run reports. Additive; no content columns.
CREATE TABLE enterprise_retention_policies(
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('*','agent','conversation','document','record')),
    max_age_seconds BIGINT NOT NULL CHECK(max_age_seconds BETWEEN 86400 AND 3153600000),
    actor TEXT NOT NULL,
    updated DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,project,source_type));

CREATE TABLE enterprise_legal_holds(
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('tenant','project','principal','source')),
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    released DOUBLE PRECISION,
    released_by TEXT,
    release_reason TEXT);
CREATE INDEX enterprise_legal_holds_active ON enterprise_legal_holds(tenant,scope,target)
    WHERE released IS NULL;

-- Original objects whose database references were purged in a committed
-- transaction. The object adapter deletes them afterwards; retries are safe.
CREATE TABLE enterprise_object_purges(
    object_key TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    queued DOUBLE PRECISION NOT NULL,
    purged DOUBLE PRECISION);
CREATE INDEX enterprise_object_purges_pending ON enterprise_object_purges(tenant,queued)
    WHERE purged IS NULL;

CREATE TABLE enterprise_lifecycle_runs(
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('retention','erasure','replay')),
    subject TEXT,
    applied INTEGER NOT NULL,
    status TEXT NOT NULL,
    cursor_created DOUBLE PRECISION,
    cursor_id TEXT,
    report TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    updated DOUBLE PRECISION NOT NULL);
CREATE INDEX enterprise_lifecycle_runs_resume ON enterprise_lifecycle_runs(tenant,kind,applied,updated);

-- Bounded retention scans and subject erasure do not read the whole corpus.
CREATE INDEX enterprise_sources_created ON enterprise_sources(tenant,created,id);
CREATE INDEX enterprise_sources_owner ON enterprise_sources(tenant,owner,id);
CREATE INDEX enterprise_deletion_journal_source ON enterprise_deletion_journal(source_id,operation);
CREATE INDEX cloud_document_vectors_document ON cloud_document_vectors(document_id);
CREATE INDEX episode_revisions_job ON episode_revisions(job_id);
