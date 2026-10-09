-- Bounded project-scoped reconciliation keys; a derivative of active claim revisions.
CREATE TABLE knowledge_candidate_keys(
    project TEXT NOT NULL,
    key_kind TEXT NOT NULL,
    key_value TEXT NOT NULL,
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    PRIMARY KEY(project,key_kind,key_value,document_id));

CREATE INDEX knowledge_candidate_keys_document
    ON knowledge_candidate_keys(document_id);
CREATE INDEX knowledge_candidate_keys_lookup
    ON knowledge_candidate_keys(project,key_kind,key_value);
