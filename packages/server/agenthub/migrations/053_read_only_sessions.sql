-- Read-only chats (docs/ACCESS_MODEL.md section 7, "Read-only chats").
--
-- A person can switch a running chat to read-only (`vaelius-client session read-only`
-- or the MCP tool `read_only_session`). The client then stops capturing, and records
-- the chat's session ids here so the service itself refuses every write that names
-- one of them: captures, compaction boundaries, delivery receipts, and MCP writes on a
-- connection that was registered. Only a digest of (tenant, principal, session id) is
-- kept; a row can only ever narrow what is accepted. Rows older than 30 days are swept.
CREATE TABLE IF NOT EXISTS read_only_sessions(
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    session TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant, principal, session)
);
CREATE INDEX IF NOT EXISTS read_only_sessions_created ON read_only_sessions(created);
