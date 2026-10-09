-- Control records only: routing and external identity bindings never contain
-- source payloads, memory claims or a copy of the tenant knowledge corpus.
CREATE TABLE cloud_tenants(
    id TEXT PRIMARY KEY, dsn TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE cloud_credential_routes(
    digest TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES cloud_tenants(id),
    active INTEGER NOT NULL DEFAULT 1, created DOUBLE PRECISION NOT NULL);
CREATE TABLE cloud_external_bindings(
    issuer TEXT NOT NULL, subject TEXT NOT NULL, tenant TEXT NOT NULL REFERENCES cloud_tenants(id),
    principal TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(issuer,subject,tenant));
