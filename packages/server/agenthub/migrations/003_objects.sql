CREATE TABLE IF NOT EXISTS backend_object_uploads (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, connection TEXT NOT NULL,
    external_id TEXT NOT NULL, version TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL, byte_length BIGINT NOT NULL CHECK (byte_length >= 0),
    status TEXT NOT NULL CHECK (status IN ('uploading','uploaded','accepted','failed','removed')),
    source_id TEXT, updated DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant,connection,external_id,version)
);
CREATE TABLE IF NOT EXISTS backend_document_versions (
    source_id TEXT PRIMARY KEY REFERENCES enterprise_sources(id), tenant TEXT NOT NULL,
    connection TEXT NOT NULL, external_id TEXT NOT NULL, version TEXT NOT NULL,
    object_key TEXT NOT NULL REFERENCES backend_object_uploads(object_key),
    sha256 TEXT NOT NULL, byte_length BIGINT NOT NULL CHECK (byte_length >= 0),
    filename TEXT NOT NULL, title TEXT NOT NULL, media_type TEXT NOT NULL,
    parser_status TEXT NOT NULL, locations TEXT NOT NULL,
    original_disposition TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant,connection,external_id,version)
);
CREATE INDEX IF NOT EXISTS backend_document_tenant ON backend_document_versions(tenant,title);
CREATE TABLE IF NOT EXISTS backend_slack_connectors (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, connection TEXT NOT NULL,
    team TEXT NOT NULL, channel TEXT NOT NULL, project TEXT NOT NULL,
    bindings TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL DEFAULT 'enrolled', observed DOUBLE PRECISION NOT NULL DEFAULT 0,
    freshness DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS backend_slack_items (
    connector TEXT NOT NULL REFERENCES backend_slack_connectors(id), external_id TEXT NOT NULL,
    content_hash TEXT NOT NULL, revision BIGINT NOT NULL, source_id TEXT NOT NULL,
    event_version TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(connector,external_id)
);
CREATE TABLE IF NOT EXISTS backend_slack_events (
    connector TEXT NOT NULL REFERENCES backend_slack_connectors(id), event_id TEXT NOT NULL,
    digest TEXT NOT NULL, status TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(connector,event_id)
);
CREATE TABLE IF NOT EXISTS backend_slack_limits (
    connector TEXT NOT NULL REFERENCES backend_slack_connectors(id), method TEXT NOT NULL,
    retry_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(connector,method)
);
