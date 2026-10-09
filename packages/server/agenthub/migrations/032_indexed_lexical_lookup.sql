-- PostgreSQL cannot push the non-leakproof text-search operators through RLS.
-- This fixed, owner-executed lookup lets GIN find text matches first, then uses
-- the very same current document oracle before returning IDs/revisions/scores.
-- No source/claim text, arbitrary SQL, caller-controlled identity, or policy
-- cache is exposed. The restricted reader still checks outer result delivery.
CREATE FUNCTION search_lexical_matches(query TEXT)
RETURNS TABLE(document_id TEXT,revision_id TEXT,score REAL)
LANGUAGE plpgsql STABLE SECURITY DEFINER AS $fn$
BEGIN
    IF query IS NULL OR length(query)>16384 OR
        nullif(current_setting('agenthub.search_context',true),'') IS NULL THEN RETURN; END IF;
    RETURN QUERY SELECT f.document_id,f.revision_id,
        ts_rank_cd(to_tsvector('simple',f.body),websearch_to_tsquery('simple',query))
        FROM knowledge_fts f JOIN knowledge_documents d
            ON d.document_id=f.document_id AND d.active_revision_id=f.revision_id
        WHERE to_tsvector('simple',f.body) @@ websearch_to_tsquery('simple',query)
            AND search_document_allowed(f.document_id);
END $fn$;
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_lexical_matches(text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_lexical_matches(text) FROM PUBLIC;
