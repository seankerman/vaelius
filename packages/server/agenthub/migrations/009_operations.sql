CREATE TABLE IF NOT EXISTS cloud_admission(
 tenant TEXT PRIMARY KEY,max_parallel INTEGER NOT NULL CHECK(max_parallel>0),
 max_attempts INTEGER NOT NULL CHECK(max_attempts>0),enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cloud_usage_attempts(
 id TEXT PRIMARY KEY,tenant TEXT NOT NULL,job_id TEXT NOT NULL,purpose TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('reserved','dispatched','returned','failed','uncertain','cancelled')),
 is_retry INTEGER NOT NULL DEFAULT 0,reserved DOUBLE PRECISION NOT NULL,lease_until DOUBLE PRECISION NOT NULL,
 finished DOUBLE PRECISION,usage TEXT NOT NULL DEFAULT '{}',result_digest TEXT,
 latency DOUBLE PRECISION,price_version TEXT,estimated_cost DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS cloud_usage_tenant_status ON cloud_usage_attempts(tenant,status,lease_until);
CREATE TABLE IF NOT EXISTS cloud_metrics(
 id TEXT PRIMARY KEY,tenant TEXT NOT NULL,kind TEXT NOT NULL,amount BIGINT NOT NULL,
 created DOUBLE PRECISION NOT NULL,details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS cloud_recovery_state(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),ready INTEGER NOT NULL DEFAULT 1,
 reason TEXT NOT NULL DEFAULT '',manifest TEXT NOT NULL DEFAULT '{}',updated DOUBLE PRECISION NOT NULL
);
