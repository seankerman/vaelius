-- Explicit, owner-issued grants to read retained originals (raw sources and
-- original document bytes). Additive: no credential or source row is rewritten.
-- Redacted passages, search and evidence never consult this table; it only
-- widens the owner-only original-read check for named principals or members of
-- a named project. Revocation keeps the row for audit (revoked IS NOT NULL).
CREATE TABLE IF NOT EXISTS raw_source_grants(
    tenant TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('source','connection')),
    scope_id TEXT NOT NULL,
    grantee_kind TEXT NOT NULL CHECK(grantee_kind IN ('principal','project')),
    grantee TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    revoked DOUBLE PRECISION,
    PRIMARY KEY(tenant,scope_kind,scope_id,grantee_kind,grantee),
    FOREIGN KEY(tenant,granted_by) REFERENCES enterprise_principals(tenant,id));
CREATE INDEX IF NOT EXISTS raw_source_grants_active ON raw_source_grants(tenant,scope_kind,scope_id)
    WHERE revoked IS NULL;
