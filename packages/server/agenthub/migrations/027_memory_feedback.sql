-- Self-reported outcomes never assert independent verification or rewrite claims.
CREATE TABLE memory_feedback_reports (
 id TEXT PRIMARY KEY,
 tenant TEXT NOT NULL,
 principal TEXT NOT NULL,
 actor TEXT NOT NULL,
 enrollment TEXT NOT NULL,
 document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id),
 revision_id TEXT NOT NULL,
 request_key TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('helpful','unhelpful','incorrect','not_used')),
 created DOUBLE PRECISION NOT NULL,
 UNIQUE(tenant,principal,enrollment,request_key)
);
CREATE INDEX memory_feedback_document_revision ON memory_feedback_reports(document_id,revision_id);
