-- Private chats (docs/ACCESS_MODEL.md, rule 5).
--
-- A person can make the rest of a running chat private (`vaelius-client session
-- private`, or the MCP tool `private_session`), and a folder can ask for it
-- (`capture = "private"` in `.vaelius`). The client then asks for owner-private
-- captures, and records the chat's session ids here so the service itself stores
-- every later capture that names one of them owner-private, whatever the client
-- sends. Same shape and rules as read_only_sessions (migration 053): only a digest
-- of (tenant, principal, session id) is kept, a row only ever narrows, there is no
-- route that removes one, and rows older than 30 days are swept.
CREATE TABLE IF NOT EXISTS private_sessions(
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    session TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant, principal, session)
);
CREATE INDEX IF NOT EXISTS private_sessions_created ON private_sessions(created);
