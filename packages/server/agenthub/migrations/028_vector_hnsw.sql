-- Additive, rebuildable search accelerator. Original vectors remain authoritative.
-- Iterative filtered scans require pgvector 0.8+. Operator upgrades the extension;
-- a migration never silently changes the globally installed extension version.
DO $check$
DECLARE version_parts INTEGER[];
BEGIN
    SELECT string_to_array(extversion,'.')::integer[] INTO version_parts
        FROM pg_extension WHERE extname='vector';
    IF version_parts IS NULL OR version_parts < ARRAY[0,8,0] THEN
        RAISE EXCEPTION 'AgentHub HNSW retrieval requires pgvector >= 0.8.0';
    END IF;
END $check$;
CREATE INDEX cloud_document_vectors_hnsw_cosine
    ON cloud_document_vectors USING hnsw (embedding vector_cosine_ops)
    WITH (m=16,ef_construction=64);
