-- Periodic user-access review evidence (SOC 2 CC6.2/CC6.3, ISO 27001 A.5.18).
-- Additive. Rows hold identifiers, decisions and outcomes only: never tokens,
-- digests, source text or claim text. A recorded decision is final evidence; the
-- application only fills in its outcome once the revocation has been applied.
CREATE TABLE IF NOT EXISTS access_reviews(
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, opened_by TEXT NOT NULL,
    opened DOUBLE PRECISION NOT NULL, stale_days INTEGER NOT NULL,
    report_sha256 TEXT NOT NULL, line_count INTEGER NOT NULL,
    closed DOUBLE PRECISION, closed_by TEXT);
CREATE TABLE IF NOT EXISTS access_review_lines(
    review_id TEXT NOT NULL REFERENCES access_reviews(id), line_id TEXT NOT NULL,
    principal TEXT NOT NULL, kind TEXT NOT NULL, target_json TEXT NOT NULL,
    flags_json TEXT NOT NULL, PRIMARY KEY(review_id,line_id));
CREATE TABLE IF NOT EXISTS access_review_decisions(
    review_id TEXT NOT NULL, line_id TEXT NOT NULL, reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','revoke')),
    comment TEXT NOT NULL, decided DOUBLE PRECISION NOT NULL,
    outcome_json TEXT NOT NULL DEFAULT '{}', applied DOUBLE PRECISION,
    PRIMARY KEY(review_id,line_id),
    FOREIGN KEY(review_id,line_id) REFERENCES access_review_lines(review_id,line_id));
