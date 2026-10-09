-- Exact document discovery should not rank every passage containing a generic noun.
CREATE INDEX cloud_knowledge_title ON knowledge_revisions(lower(claim_json::jsonb->>'title'));
