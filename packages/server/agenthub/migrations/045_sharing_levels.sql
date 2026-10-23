-- One sharing ladder for every content type: private < team < organization.
-- Additive: applied migrations stay immutable, and nothing becomes more visible.
--
-- Effective sharing of a captured source is the minimum of
--   the author's own setting      enterprise_principals.max_sharing
--   the scope-file request        enterprise_sources.sharing_requested / originals_requested
--   the project ceiling           enterprise_projects.max_sharing, else the tenant default
--                                 enterprise_organizations.default_max_sharing
--   the tenant ceiling            enterprise_organizations.max_sharing
--   the connection visibility     enterprise_sources.visibility
--   the origin ceiling            policy definition key origin_ceiling (absent = organization)
-- It is stored per source at ingest (sharing, originals_sharing) and every
-- ceiling is evaluated again at query time, so lowering any term hides content
-- on the next request and raising one never exposes a source stored lower.
--
-- Existing rows keep today's behavior:
-- * existing tenants get a tenant ceiling of team (the old maximum) and keep
--   their default_max_sharing; only tenants created after this migration
--   default both to organization;
-- * existing principals that own a source stored as team get team (so nothing
--   now visible disappears); every other principal, and every new one, is private;
-- * existing originals stay private (owner and explicit grants only).
DO $levels$
DECLARE item RECORD;
BEGIN
    -- Replace the two-level CHECK constraints of migration 042, whatever their names.
    FOR item IN SELECT c.conname,c.conrelid::regclass AS relation FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
        WHERE c.contype='c' AND c.connamespace=current_schema()::regnamespace
        AND (c.conrelid,a.attname) IN (('enterprise_organizations'::regclass,'default_max_sharing'),
            ('enterprise_projects'::regclass,'max_sharing'),('enterprise_sources'::regclass,'sharing'),
            ('enterprise_sources'::regclass,'sharing_requested'))
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',item.relation,item.conname);
    END LOOP;
END $levels$;
ALTER TABLE enterprise_organizations ADD CONSTRAINT enterprise_organizations_default_max_sharing_level
    CHECK (default_max_sharing IN ('private','team','organization'));
ALTER TABLE enterprise_projects ADD CONSTRAINT enterprise_projects_max_sharing_level
    CHECK (max_sharing IN ('private','team','organization'));
ALTER TABLE enterprise_sources ADD CONSTRAINT enterprise_sources_sharing_level
    CHECK (sharing IN ('private','team','organization'));
ALTER TABLE enterprise_sources ADD CONSTRAINT enterprise_sources_sharing_requested_level
    CHECK (sharing_requested IN ('private','team','organization'));

-- Tenant ceiling: existing tenants are filled with team, new tenants get organization.
ALTER TABLE enterprise_organizations ADD COLUMN IF NOT EXISTS max_sharing TEXT NOT NULL DEFAULT 'team'
    CONSTRAINT enterprise_organizations_max_sharing_level CHECK (max_sharing IN ('private','team','organization'));
ALTER TABLE enterprise_organizations ALTER COLUMN max_sharing SET DEFAULT 'organization';
ALTER TABLE enterprise_organizations ALTER COLUMN default_max_sharing SET DEFAULT 'organization';

-- The person's own ceiling, set only by that person (agenthub.project_sharing).
ALTER TABLE enterprise_principals ADD COLUMN IF NOT EXISTS max_sharing TEXT NOT NULL DEFAULT 'private'
    CONSTRAINT enterprise_principals_max_sharing_level CHECK (max_sharing IN ('private','team','organization'));
UPDATE enterprise_principals p SET max_sharing='team' WHERE p.max_sharing='private' AND EXISTS(
    SELECT 1 FROM enterprise_sources s WHERE s.tenant=p.tenant AND s.owner=p.id AND s.sharing='team');

-- Retained originals have their own level, never above the passage level.
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS originals_sharing TEXT NOT NULL DEFAULT 'private'
    CONSTRAINT enterprise_sources_originals_sharing_level CHECK (originals_sharing IN ('private','team','organization'));
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS originals_requested TEXT NOT NULL DEFAULT 'private'
    CONSTRAINT enterprise_sources_originals_requested_level CHECK (originals_requested IN ('private','team','organization'));

-- raw_shared is now the stored level of a passage's own source: 0 private,
-- 1 team (unchanged from migration 042), 2 organization. It is a stable
-- prefilter; the policy pass still decides.
-- source_project is the external project of every source a document depends on
-- (NULL when they differ). It orders and scopes retrieval; it grants nothing.
ALTER TABLE enterprise_documents ADD COLUMN IF NOT EXISTS source_project TEXT;
UPDATE enterprise_documents ed SET source_project=x.project FROM (
    SELECT d.document_id,min(s.external_project) project FROM enterprise_dependencies d
    JOIN enterprise_sources s ON s.id=d.source_id GROUP BY d.document_id
    HAVING count(DISTINCT s.external_project)=1) x
WHERE x.document_id=ed.id AND ed.source_project IS NULL;

-- The key is added only for shared sources. Team definitions are byte-identical
-- to migration 042 and every other definition is unchanged, so no projection
-- rebuild is needed (no source can be stored as organization before this runs).
CREATE OR REPLACE VIEW search_source_policy_definitions AS
SELECT s.id AS source_id,jsonb_build_object(
    'tenant',s.tenant,'owner',s.owner,'project',s.external_project,
    'visibility',s.visibility,'active',s.active,
    'has_revision',r.source_id IS NOT NULL,'connection',r.connection,
    'has_acl',a.source_id IS NOT NULL,'acl_state',a.state,'acl_until',a.valid_until
) || CASE WHEN s.principal_quarantine=1 THEN '{"quarantined":true}'::jsonb ELSE '{}'::jsonb END
  || CASE WHEN s.sharing IN ('team','organization') THEN jsonb_build_object('sharing',s.sharing)
          ELSE '{}'::jsonb END AS definition
FROM enterprise_sources s
LEFT JOIN backend_source_revisions r ON r.source_id=s.id
LEFT JOIN cloud_source_acl a ON a.source_id=s.id;

-- The CURRENT level of one source policy (0 private, 1 team, 2 organization):
-- the level stored at ingest, capped again by today's author setting, project
-- ceiling (or tenant default), tenant ceiling, connection visibility and origin
-- ceiling (policy key origin_ceiling; absent = organization, so no effect yet).
-- With a reader, true when that level reaches them: 1 = an active member of the
-- source project; 2 = also any active principal of the tenant that is not under
-- an operator quarantine. Without a reader (NULL), true when the level is at
-- least `minimum`. Mirrors agenthub.search_permissions.SHARE and
-- ProjectSharingMixin._passage_shared. Only search_document_allowed calls it,
-- and only for passages that are shared, so other rows never pay for it.
CREATE OR REPLACE FUNCTION search_passage_shared(definition JSONB, tenant_id TEXT, actor_id TEXT, minimum INTEGER)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    level INTEGER;
    member BOOLEAN;
    organization_reader BOOLEAN;
BEGIN
    IF definition IS NULL OR coalesce(definition->>'sharing','private') NOT IN ('team','organization') THEN
        RETURN false;
    END IF;
    -- One statement per call: the level and both audiences.
    SELECT least(
            coalesce(array_position(ARRAY['team','organization'],definition->>'sharing'),0),
            coalesce(array_position(ARRAY['team','organization'],definition->>'visibility'),0),
            coalesce(array_position(ARRAY['team','organization'],author.max_sharing),0),
            coalesce(array_position(ARRAY['team','organization'],
                coalesce(project.max_sharing,organization.default_max_sharing)),0),
            coalesce(array_position(ARRAY['team','organization'],organization.max_sharing),0),
            coalesce(array_position(ARRAY['team','organization'],
                coalesce(definition->>'origin_ceiling','organization')),0)),
        EXISTS(SELECT 1 FROM enterprise_memberships membership
            WHERE membership.tenant=tenant_id AND membership.project=project.id
            AND membership.principal=actor_id AND membership.active=1),
        EXISTS(SELECT 1 FROM enterprise_principals reader
            WHERE reader.tenant=tenant_id AND reader.id=actor_id AND reader.active=1
            AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                WHERE held.tenant=reader.tenant AND held.principal=reader.id))
        INTO level, member, organization_reader
        FROM enterprise_projects project
        JOIN enterprise_organizations organization ON organization.id=project.tenant
        JOIN enterprise_principals author ON author.tenant=project.tenant AND author.id=definition->>'owner'
        WHERE project.tenant=tenant_id AND project.id=definition->>'project' AND project.active=1;
    IF level IS NULL OR level<greatest(minimum,1) THEN RETURN false; END IF;
    IF actor_id IS NULL THEN RETURN true; END IF;
    RETURN member OR (level=2 AND organization_reader);
END $fn$;
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_passage_shared(jsonb,text,text,integer) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_passage_shared(jsonb,text,text,integer) FROM PUBLIC;

-- Trusted server identity is installed transaction-locally; absent context denies.
-- The migration owner owns this narrowly scoped SECURITY DEFINER function.
--
-- Retrieval reach (context key `reach`, set by agenthub.search_reader) is checked
-- first and separately: with reach = 'project' and a requested project, a passage
-- from another project is returned only when its current level is organization.
-- It narrows retrieval and never grants anything. Keeping it a separate statement
-- leaves the authorization query's cached plan identical for every reach.
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
    IF context->>'reach'='project' AND requested_project IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM enterprise_documents ed WHERE ed.id=target
            AND (ed.source_project=requested_project OR ed.representation!='raw'
                OR (ed.raw_shared=2 AND (SELECT bool_and(required.dependency_count>0
                        AND search_passage_shared(policy.definition,tenant_id,NULL,2))
                    FROM search_document_policies required
                    LEFT JOIN search_policies policy ON policy.id=required.policy_id
                    WHERE required.document_id=ed.id) IS TRUE))) THEN
        RETURN false;
    END IF;
    RETURN EXISTS(SELECT 1 FROM knowledge_documents d
        JOIN enterprise_documents ed ON ed.id=d.document_id
        WHERE d.document_id=target AND ed.tenant=tenant_id AND ed.active=1
        AND (review IS NULL OR d.document_id=ANY(review))
        -- Redacted passages: the owner, or every dependency's current level reaches
        -- the reader (search_passage_shared above).
        AND (ed.representation!='raw' OR (coalesce((context->>'source_read')::boolean,false) AND (
            ed.raw_owner=actor_id OR (ed.raw_shared>0 AND (SELECT bool_and(required.dependency_count>0
                    AND search_passage_shared(policy.definition,tenant_id,actor_id,1))
                FROM search_document_policies required
                LEFT JOIN search_policies policy ON policy.id=required.policy_id
                WHERE required.document_id=d.document_id) IS TRUE))))
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
