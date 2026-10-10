-- Refresh-token rotation and family revocation (RFC 9700 section 4.14.2, RFC 7009).
-- Additive and idempotent: no existing credential row is rewritten. Access tokens
-- issued before this migration have no family; they keep authenticating until their
-- own expiry and cannot be exchanged for a refresh token (the user logs in again).
-- Only SHA-256 digests are stored; raw tokens never reach the database.
CREATE TABLE IF NOT EXISTS enterprise_credential_families(
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, principal TEXT NOT NULL, acting_for TEXT,
    enrollment TEXT NOT NULL, actions TEXT NOT NULL,
    identity_issuer TEXT, identity_subject TEXT, identity_provider TEXT,
    created DOUBLE PRECISION NOT NULL, absolute_expires_at DOUBLE PRECISION NOT NULL,
    revoked_at DOUBLE PRECISION, revoked_reason TEXT,
    FOREIGN KEY(tenant,principal) REFERENCES enterprise_principals(tenant,id),
    FOREIGN KEY(tenant,acting_for) REFERENCES enterprise_principals(tenant,id));
ALTER TABLE enterprise_credentials ADD COLUMN IF NOT EXISTS family TEXT
    REFERENCES enterprise_credential_families(id);
CREATE INDEX IF NOT EXISTS enterprise_credentials_family ON enterprise_credentials(family)
    WHERE family IS NOT NULL;
-- A refresh token is spent (used_at set) by exactly one rotation. Its successor
-- digests let an identical lost-reply retry be answered again instead of being
-- mistaken for reuse; any other presentation of a spent token revokes the family.
CREATE TABLE IF NOT EXISTS enterprise_refresh_tokens(
    digest TEXT PRIMARY KEY, family TEXT NOT NULL REFERENCES enterprise_credential_families(id),
    active INTEGER NOT NULL DEFAULT 1, created DOUBLE PRECISION NOT NULL,
    idle_expires_at DOUBLE PRECISION NOT NULL, used_at DOUBLE PRECISION,
    successor_digest TEXT, successor_access_digest TEXT);
CREATE INDEX IF NOT EXISTS enterprise_refresh_tokens_family ON enterprise_refresh_tokens(family);
