-- No per-item sharing (owner decision, docs/ACCESS_MODEL.md): an item is read by
-- its workspace's audience, or only by its owner when it is personal memory. The
-- owner tools that shared single items with chosen people or groups
-- (`share_memories`, `vaelius-client share`) are removed.
--
-- Every active share of the passage tier, and every owner-issued share, is revoked
-- (kept for audit). Revoking only removes access, never adds it. The table stays
-- because the operator originals-tier grant (agenthub.raw_access, `policy`) still
-- uses it; a CHECK constraint keeps every active grant originals-only and
-- operator-issued, so the share path of the read predicate (Python oracle, indexed
-- SQL filter and row-level security alike) can match nothing.
UPDATE raw_source_grants SET revoked=extract(epoch FROM now()),revoked_by='migration_055'
    WHERE revoked IS NULL AND (passages=1 OR issued_with<>'policy');
ALTER TABLE raw_source_grants ADD CONSTRAINT raw_source_grants_originals_only
    CHECK (revoked IS NOT NULL OR (passages=0 AND issued_with='policy'));
-- raw_granted is the passage-share prefilter; no passage is shared any more.
UPDATE enterprise_documents SET raw_granted=0 WHERE raw_granted<>0;
