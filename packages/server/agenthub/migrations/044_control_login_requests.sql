-- Interactive login requests (control plane). `begin` records one for any
-- well-formed tenant name without consulting whether that tenant exists, so the
-- unauthenticated response is the same for every name; the tenant is resolved
-- only when the matching state returns. Only a SHA-256 digest of the state is
-- the key. The request holds the PKCE verifier and nonce until it is consumed
-- or expires; nothing here is a credential or a copy of tenant data.
CREATE TABLE IF NOT EXISTS cloud_login_requests(
    digest TEXT PRIMARY KEY, tenant TEXT NOT NULL, broker TEXT NOT NULL,
    request_json TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS cloud_login_requests_tenant ON cloud_login_requests(tenant,created);
CREATE INDEX IF NOT EXISTS cloud_login_requests_expiry ON cloud_login_requests(expires_at);
