-- Additive replacement: applied migration 022 and its receipts remain immutable.
LOCK TABLE enterprise_sources,backend_source_revisions,cloud_source_acl,enterprise_dependencies
    IN SHARE ROW EXCLUSIVE MODE;
CREATE OR REPLACE FUNCTION search_refresh_source_policies(ids TEXT[]) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF ids IS NULL THEN
        SELECT array_agg(source_id) INTO ids FROM (
            SELECT id source_id FROM enterprise_sources UNION SELECT source_id FROM search_source_policies
        ) all_sources;
    END IF;
    IF coalesce(cardinality(ids),0)=0 THEN RETURN; END IF;
    -- Row locks serialize changes only for the affected sources. The following
    -- statements take fresh READ COMMITTED snapshots after waiting for writers.
    PERFORM id FROM enterprise_sources WHERE id=ANY(ids) ORDER BY id FOR NO KEY UPDATE;
    INSERT INTO search_policies(definition)
        SELECT DISTINCT definition FROM search_source_policy_definitions WHERE source_id=ANY(ids)
        ORDER BY definition ON CONFLICT(definition) DO NOTHING;
    WITH changes AS MATERIALIZED (
        SELECT i.source_id,coalesce(old.policy_id,0) old_policy,coalesce(p.id,0) new_policy
        FROM unnest(ids) i(source_id)
        LEFT JOIN search_source_policies old USING(source_id)
        LEFT JOIN search_source_policy_definitions fresh USING(source_id)
        LEFT JOIN search_policies p ON p.definition=fresh.definition
        WHERE coalesce(old.policy_id,0)!=coalesce(p.id,0)
    ), deltas AS (
        SELECT x.document_id,c.old_policy policy_id,-count(*) amount
        FROM changes c JOIN enterprise_dependencies x USING(source_id) GROUP BY x.document_id,c.old_policy
        UNION ALL
        SELECT x.document_id,c.new_policy policy_id,count(*) amount
        FROM changes c JOIN enterprise_dependencies x USING(source_id) GROUP BY x.document_id,c.new_policy
    )
    INSERT INTO search_document_policies(document_id,policy_id,dependency_count)
        SELECT document_id,policy_id,sum(amount)::bigint FROM deltas GROUP BY document_id,policy_id
        HAVING sum(amount)!=0 ORDER BY document_id,policy_id
        ON CONFLICT(document_id,policy_id) DO UPDATE
            SET dependency_count=search_document_policies.dependency_count+excluded.dependency_count;
    INSERT INTO search_source_policies(source_id,policy_id)
        SELECT i.source_id,coalesce(p.id,0) FROM unnest(ids) i(source_id)
        LEFT JOIN search_source_policy_definitions fresh USING(source_id)
        LEFT JOIN search_policies p ON p.definition=fresh.definition
        ON CONFLICT(source_id) DO UPDATE SET policy_id=excluded.policy_id;
    PERFORM search_policy_validate_counts();
END $$;

CREATE OR REPLACE FUNCTION search_policy_dependencies_changed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE relation_sql TEXT; ids TEXT[];
BEGIN
    IF TG_OP='TRUNCATE' THEN DELETE FROM search_document_policies; RETURN NULL; END IF;
    relation_sql=CASE TG_OP
        WHEN 'INSERT' THEN 'SELECT document_id,source_id,1 amount FROM new_rows'
        WHEN 'DELETE' THEN 'SELECT document_id,source_id,-1 amount FROM old_rows'
        ELSE 'SELECT document_id,source_id,1 amount FROM new_rows UNION ALL SELECT document_id,source_id,-1 amount FROM old_rows'
    END;
    EXECUTE 'SELECT array_agg(DISTINCT source_id) FROM ('||relation_sql||') changed' INTO ids;
    PERFORM id FROM enterprise_sources WHERE id=ANY(ids) ORDER BY id FOR NO KEY UPDATE;
    EXECUTE 'INSERT INTO search_document_policies(document_id,policy_id,dependency_count)
        SELECT d.document_id,coalesce(p.policy_id,0),sum(d.amount)::bigint
        FROM ('||relation_sql||') d LEFT JOIN search_source_policies p USING(source_id)
        GROUP BY d.document_id,coalesce(p.policy_id,0) HAVING sum(d.amount)!=0 ORDER BY d.document_id,coalesce(p.policy_id,0)
        ON CONFLICT(document_id,policy_id) DO UPDATE
            SET dependency_count=search_document_policies.dependency_count+excluded.dependency_count';
    PERFORM search_policy_validate_counts();
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION search_rebuild_permissions() RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    LOCK TABLE enterprise_sources,backend_source_revisions,cloud_source_acl,enterprise_dependencies
        IN SHARE ROW EXCLUSIVE MODE;
    INSERT INTO search_policies(definition)
        SELECT DISTINCT definition FROM search_source_policy_definitions ON CONFLICT DO NOTHING;
    DELETE FROM search_document_policies;
    DELETE FROM search_source_policies;
    INSERT INTO search_source_policies
        SELECT s.source_id,p.id FROM search_source_policy_definitions s JOIN search_policies p USING(definition);
    INSERT INTO search_document_policies
        SELECT x.document_id,coalesce(p.policy_id,0),count(*)
        FROM enterprise_dependencies x LEFT JOIN search_source_policies p USING(source_id)
        GROUP BY x.document_id,coalesce(p.policy_id,0);
END $$;
DROP FUNCTION search_policy_projection_lock();
