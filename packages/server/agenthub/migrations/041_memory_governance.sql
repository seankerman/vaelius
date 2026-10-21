-- Shared-memory poisoning defenses (additive; applied migrations stay immutable).
--
-- * instruction_like: ingest-time label for text that reads as instructions to an
--   agent. Content is kept and served under the usual policy; delivery marks it.
-- * principal_quarantine: an operator hold. Held sources stay readable by their
--   owner and operators, and are withheld from every other reader through the
--   same compiled search policy that already enforces visibility.
-- * ingest_rate_windows: fixed-window write counters shared by API processes.
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS instruction_like SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS principal_quarantine SMALLINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS enterprise_sources_owner_created ON enterprise_sources(tenant,owner,created);

CREATE TABLE IF NOT EXISTS enterprise_principal_quarantine(
    tenant TEXT NOT NULL, principal TEXT NOT NULL, since DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,principal),
    FOREIGN KEY(tenant,principal) REFERENCES enterprise_principals(tenant,id));

CREATE TABLE IF NOT EXISTS ingest_rate_windows(
    scope TEXT NOT NULL, window_start BIGINT NOT NULL,
    writes BIGINT NOT NULL DEFAULT 0, bytes BIGINT NOT NULL DEFAULT 0,
    limited SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY(scope,window_start));

-- The key is added only for held sources, so every existing policy definition
-- is unchanged and no projection rebuild is needed.
CREATE OR REPLACE VIEW search_source_policy_definitions AS
SELECT s.id AS source_id,jsonb_build_object(
    'tenant',s.tenant,'owner',s.owner,'project',s.external_project,
    'visibility',s.visibility,'active',s.active,
    'has_revision',r.source_id IS NOT NULL,'connection',r.connection,
    'has_acl',a.source_id IS NOT NULL,'acl_state',a.state,'acl_until',a.valid_until
) || CASE WHEN s.principal_quarantine=1 THEN '{"quarantined":true}'::jsonb ELSE '{}'::jsonb END AS definition
FROM enterprise_sources s
LEFT JOIN backend_source_revisions r ON r.source_id=s.id
LEFT JOIN cloud_source_acl a ON a.source_id=s.id;

-- Trusted server identity is installed transaction-locally; absent context denies.
-- The migration owner owns this narrowly scoped SECURITY DEFINER function.
CREATE OR REPLACE FUNCTION search_document_allowed(target TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY DEFINER AS $fn$
DECLARE
    context JSONB := nullif(current_setting('agenthub.search_context',true),'')::jsonb;
    tenant_id TEXT := context->>'tenant';
    actor_id TEXT := context->>'actor';
    requested_project TEXT := context->>'project';
    scopes TEXT[] := ARRAY(SELECT jsonb_array_elements_text(context->'scopes'));
    delegated TEXT[] := CASE WHEN context->'delegated'='null'::jsonb THEN NULL
        ELSE ARRAY(SELECT jsonb_array_elements_text(context->'delegated')) END;
    review TEXT[] := CASE WHEN context->'review'='null'::jsonb THEN NULL
        ELSE ARRAY(SELECT jsonb_array_elements_text(context->'review')) END;
    releases TEXT[] := ARRAY(SELECT jsonb_array_elements_text(context->'releases'));
    at_time DOUBLE PRECISION := extract(epoch FROM statement_timestamp());
BEGIN
    IF context IS NULL OR tenant_id IS NULL OR actor_id IS NULL THEN RETURN false; END IF;
    RETURN EXISTS(SELECT 1 FROM knowledge_documents d
        JOIN enterprise_documents ed ON ed.id=d.document_id
        WHERE d.document_id=target AND ed.tenant=tenant_id AND ed.active=1
        AND (review IS NULL OR d.document_id=ANY(review))
        AND (ed.representation!='raw' OR (ed.raw_owner=actor_id AND coalesce((context->>'source_read')::boolean,false)))
        AND d.lifecycle='active' AND ((ed.tenant=tenant_id AND ed.active=1 AND d.lifecycle='active'
            AND (ed.internal_project=ANY(scopes::text[]) OR EXISTS(SELECT 1 FROM cloud_preferences pf
                WHERE pf.document_id=d.document_id AND pf.tenant=tenant_id AND pf.owner=actor_id AND pf.valid_until IS NULL
                AND pf.revision_id=d.active_revision_id AND (pf.scope='user' OR (pf.scope='project' AND pf.project=requested_project))))
            AND (NOT EXISTS(SELECT 1 FROM knowledge_generation_documents gd WHERE gd.document_id=d.document_id)
                OR EXISTS(SELECT 1 FROM knowledge_generation_documents gd JOIN knowledge_generations g
                    ON g.generation_id=gd.generation_id WHERE gd.document_id=d.document_id AND g.status='active') OR d.document_id=ANY(review))
            AND NOT EXISTS(SELECT 1 FROM cloud_preference_candidates pc WHERE pc.document_id=d.document_id AND pc.status IN ('pending','held'))
            AND NOT EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id
                AND (pf.owner!=actor_id OR pf.tenant!=tenant_id))
            AND (NOT EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id)
                OR EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id
                    AND pf.valid_until IS NULL AND pf.revision_id=d.active_revision_id))
            AND (SELECT bool_and(required.dependency_count>0 AND (
                    (policy.definition->>'active')::integer=1
                    AND policy.definition->>'tenant'=tenant_id
                    AND (delegated::text[] IS NULL OR policy.definition->>'project'=ANY(delegated::text[]))
                    AND ((policy.definition->>'has_acl')::boolean=false OR (
                        policy.definition->>'acl_state'='current'
                        AND (policy.definition->>'acl_until')::double precision>at_time))
                    AND ((policy.definition->>'has_revision')::boolean=false OR (
                        connection.tenant=tenant_id AND connection.active=1
                        AND connection.permission_observed+connection.freshness_seconds>=at_time
                        AND (connection.reader_ids::jsonb='[]'::jsonb OR connection.owner=actor_id OR EXISTS(
                            SELECT 1 FROM jsonb_array_elements_text(connection.reader_ids::jsonb) reader
                            WHERE reader.value=actor_id))))
                    AND (policy.definition->>'visibility'='organization'
                        OR (policy.definition->>'visibility'='private' AND policy.definition->>'owner'=actor_id)
                        OR (policy.definition->>'visibility'='team' AND EXISTS(
                            SELECT 1 FROM enterprise_memberships membership
                            JOIN enterprise_projects project ON project.tenant=membership.tenant AND project.id=membership.project
                            WHERE membership.tenant=tenant_id AND membership.project=policy.definition->>'project'
                                AND membership.principal=actor_id AND membership.active=1 AND project.active=1)))
                    -- An operator quarantine serves a principal's held sources to that
                    -- principal only; every document that depends on one is held too.
                    AND (coalesce((policy.definition->>'quarantined')::boolean,false)=false
                        OR policy.definition->>'owner'=actor_id)
                ) IS TRUE)
            FROM search_document_policies required
            LEFT JOIN search_policies policy ON policy.id=required.policy_id
            LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
            WHERE required.document_id=d.document_id)) OR d.document_id=ANY(releases)));
END $fn$;
-- Resolve only trusted objects; pg_temp must follow the trusted schema.
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_document_allowed(text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_document_allowed(text) FROM PUBLIC;
