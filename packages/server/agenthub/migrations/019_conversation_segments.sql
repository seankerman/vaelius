CREATE TABLE IF NOT EXISTS cloud_segment_uploads (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, connection TEXT NOT NULL,
    external_id TEXT NOT NULL, revision TEXT NOT NULL, source_id TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
    byte_length BIGINT NOT NULL CHECK (byte_length >= 0 AND byte_length <= 8388608),
    status TEXT NOT NULL CHECK (status IN ('uploading','uploaded','accepted','failed','removed')),
    updated DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant,connection,external_id,revision)
);
CREATE TABLE IF NOT EXISTS cloud_conversation_segments (
    source_id TEXT PRIMARY KEY REFERENCES enterprise_sources(id), tenant TEXT NOT NULL,
    connection TEXT NOT NULL, external_id TEXT NOT NULL, revision TEXT NOT NULL,
    object_key TEXT NOT NULL REFERENCES cloud_segment_uploads(object_key),
    sha256 TEXT NOT NULL, byte_length BIGINT NOT NULL CHECK (byte_length >= 0 AND byte_length <= 8388608),
    representation TEXT NOT NULL CHECK (representation = 'canonical_redacted_event_v1'),
    status TEXT NOT NULL CHECK (status IN ('active','removed')),
    created DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant,connection,external_id,revision)
);
CREATE INDEX IF NOT EXISTS cloud_segment_tenant_connection
    ON cloud_conversation_segments(tenant,connection,status);
