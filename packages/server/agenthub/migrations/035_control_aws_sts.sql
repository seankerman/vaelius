-- AWS sign-in replay guard (control plane). A signed sts:GetCallerIdentity request
-- is accepted once: only a SHA-256 digest of its SigV4 signature is kept, until STS
-- would reject that signature as expired. No AWS credential is ever stored.
CREATE TABLE IF NOT EXISTS cloud_aws_sts_replay(
    digest TEXT PRIMARY KEY, expires_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS cloud_aws_sts_replay_expiry ON cloud_aws_sts_replay(expires_at);
