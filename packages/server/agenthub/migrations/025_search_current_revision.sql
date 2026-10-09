-- A document's current grants must not expose an older revision by omission of
-- the normal active-revision join. Historical delivery has its own policy path.
CREATE FUNCTION search_revision_allowed(target TEXT, revision TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER AS $fn$
    SELECT EXISTS(SELECT 1 FROM knowledge_documents d
        WHERE d.document_id=target AND d.active_revision_id=revision
        AND search_document_allowed(target));
$fn$;
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_revision_allowed(text,text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_revision_allowed(text,text) FROM PUBLIC;
