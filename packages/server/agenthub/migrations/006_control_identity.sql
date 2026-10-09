CREATE TABLE cloud_identity_bindings(
    issuer TEXT NOT NULL,subject TEXT NOT NULL,tenant TEXT NOT NULL REFERENCES cloud_tenants(id),
    principal TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
    federation_required INTEGER NOT NULL DEFAULT 0,allowed_providers TEXT NOT NULL,
    epoch BIGINT NOT NULL DEFAULT 1,PRIMARY KEY(issuer,subject,tenant));
