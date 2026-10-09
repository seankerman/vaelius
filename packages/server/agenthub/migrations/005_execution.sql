CREATE TABLE IF NOT EXISTS backend_provider_returns (
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result TEXT NOT NULL,
  usage TEXT NOT NULL,
  provider_handle TEXT,
  created DOUBLE PRECISION NOT NULL,
  UNIQUE(job_id,purpose,request_hash)
);
CREATE INDEX IF NOT EXISTS backend_returns_job ON backend_provider_returns(job_id);
