CREATE TABLE IF NOT EXISTS cloud_usage_reconciliations(
 attempt_id TEXT NOT NULL REFERENCES cloud_usage_attempts(id),
 original_status TEXT NOT NULL,new_status TEXT NOT NULL,result_digest TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL,PRIMARY KEY(attempt_id,result_digest)
);
