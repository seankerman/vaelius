-- Origin labels and session taint (agentclient.origin, agenthub.project_sharing).
-- Additive: applied migrations stay immutable, and nothing becomes more visible.
--
-- A captured event may carry what it touched: a ceiling (the widest level its
-- content may get), a reason category, and the other projects whose content it
-- carries. They are stored per source as sent and never change afterwards:
--   origin_ceiling   private | team | organization, NULL when the event sent none
--   origin_reason    a category such as personal_content or mcp_server, never content
--   origin_projects  other project names, a JSON array as text (at most 16)
-- The ceiling and the other projects' current ceilings are terms of the sharing
-- minimum at ingest and again at query time, and a team reader must be an active
-- member of the source project and of every other project. Existing sources have
-- no origin, so their policy definitions and visibility are unchanged.
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS origin_ceiling TEXT
    CONSTRAINT enterprise_sources_origin_ceiling_level CHECK (origin_ceiling IN ('private','team','organization'));
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS origin_reason TEXT
    CONSTRAINT enterprise_sources_origin_reason_category CHECK (origin_reason ~ '^[a-z_]{1,40}$');
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS origin_projects TEXT NOT NULL DEFAULT '[]'
    CONSTRAINT enterprise_sources_origin_projects_list CHECK (jsonb_typeof(origin_projects::jsonb)='array'
        AND jsonb_array_length(origin_projects::jsonb)<=16);

-- The origin is fixed at ingest: no later change, by policy or by hand, can widen it.
CREATE OR REPLACE FUNCTION enterprise_sources_origin_fixed() RETURNS TRIGGER LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.origin_ceiling IS DISTINCT FROM OLD.origin_ceiling
            OR NEW.origin_reason IS DISTINCT FROM OLD.origin_reason
            OR NEW.origin_projects IS DISTINCT FROM OLD.origin_projects THEN
        RAISE EXCEPTION 'origin_label_is_fixed' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $fn$;
DROP TRIGGER IF EXISTS enterprise_sources_origin_fixed ON enterprise_sources;
CREATE TRIGGER enterprise_sources_origin_fixed BEFORE UPDATE OF origin_ceiling,origin_reason,origin_projects
    ON enterprise_sources FOR EACH ROW EXECUTE FUNCTION enterprise_sources_origin_fixed();

-- Keys are added only for sources that have a restricting origin, so every
-- existing definition is byte-identical and no projection rebuild is needed.
CREATE OR REPLACE VIEW search_source_policy_definitions AS
SELECT s.id AS source_id,jsonb_build_object(
    'tenant',s.tenant,'owner',s.owner,'project',s.external_project,
    'visibility',s.visibility,'active',s.active,
    'has_revision',r.source_id IS NOT NULL,'connection',r.connection,
    'has_acl',a.source_id IS NOT NULL,'acl_state',a.state,'acl_until',a.valid_until
) || CASE WHEN s.principal_quarantine=1 THEN '{"quarantined":true}'::jsonb ELSE '{}'::jsonb END
  || CASE WHEN s.sharing IN ('team','organization') THEN jsonb_build_object('sharing',s.sharing)
          ELSE '{}'::jsonb END
  || CASE WHEN s.origin_ceiling IN ('private','team') THEN jsonb_build_object('origin_ceiling',s.origin_ceiling)
          ELSE '{}'::jsonb END
  || CASE WHEN s.origin_projects!='[]' THEN jsonb_build_object('origin_projects',s.origin_projects::jsonb)
          ELSE '{}'::jsonb END AS definition
FROM enterprise_sources s
LEFT JOIN backend_source_revisions r ON r.source_id=s.id
LEFT JOIN cloud_source_acl a ON a.source_id=s.id;

-- Migration 045's function with the origin terms: the current ceiling of every
-- other project (unknown or inactive = private) joins the minimum, and a member
-- of the source project must also be an active member of every other project.
-- Mirrors agenthub.search_permissions.SHARE and ProjectSharingMixin._passage_shared.
CREATE OR REPLACE FUNCTION search_passage_shared(definition JSONB, tenant_id TEXT, actor_id TEXT, minimum INTEGER)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    level INTEGER;
    member BOOLEAN;
    organization_reader BOOLEAN;
    carried JSONB := coalesce(definition->'origin_projects','[]'::jsonb);
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
                coalesce(definition->>'origin_ceiling','organization')),0),
            coalesce((SELECT min(CASE WHEN other.id IS NULL THEN 0 ELSE coalesce(array_position(
                    ARRAY['team','organization'],coalesce(other.max_sharing,organization.default_max_sharing)),0) END)
                FROM jsonb_array_elements_text(carried) named(id)
                LEFT JOIN enterprise_projects other ON other.tenant=project.tenant AND other.id=named.id
                    AND other.active=1),2)),
        EXISTS(SELECT 1 FROM enterprise_memberships membership
            WHERE membership.tenant=tenant_id AND membership.project=project.id
            AND membership.principal=actor_id AND membership.active=1
            AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(carried) named(id)
                WHERE NOT EXISTS(SELECT 1 FROM enterprise_memberships other_member
                    WHERE other_member.tenant=tenant_id AND other_member.project=named.id
                    AND other_member.principal=actor_id AND other_member.active=1))),
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

-- Every document, curated and derived ones included, reaches a reader other than
-- a source's owner only as far as that source's origin allows. A derived document
-- built from a tainted passage would otherwise follow only its connection's
-- visibility. The origin level is the stored origin ceiling capped by the current
-- ceiling of every other project the source carries (unknown or inactive =
-- private): organization changes nothing; team needs an active member of the
-- source project and of every other project; private reaches nobody else.
-- Sources without an origin return at once, so their authorization is unchanged.
-- Mirrors agenthub.search_permissions.ORIGIN_REACHES and
-- ProjectSharingMixin._origin_reaches.
CREATE OR REPLACE FUNCTION search_origin_reaches(definition JSONB, tenant_id TEXT, actor_id TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    level INTEGER;
    member BOOLEAN;
    carried JSONB := coalesce(definition->'origin_projects','[]'::jsonb);
BEGIN
    IF definition->>'origin_ceiling' IS NULL AND definition->'origin_projects' IS NULL THEN RETURN true; END IF;
    SELECT least(
            coalesce(array_position(ARRAY['team','organization'],
                coalesce(definition->>'origin_ceiling','organization')),0),
            coalesce((SELECT min(CASE WHEN other.id IS NULL THEN 0 ELSE coalesce(array_position(
                    ARRAY['team','organization'],coalesce(other.max_sharing,organization.default_max_sharing)),0) END)
                FROM jsonb_array_elements_text(carried) named(id)
                LEFT JOIN enterprise_projects other ON other.tenant=project.tenant AND other.id=named.id
                    AND other.active=1),2)),
        EXISTS(SELECT 1 FROM enterprise_memberships membership
            WHERE membership.tenant=tenant_id AND membership.project=project.id
            AND membership.principal=actor_id AND membership.active=1
            AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(carried) named(id)
                WHERE NOT EXISTS(SELECT 1 FROM enterprise_memberships other_member
                    WHERE other_member.tenant=tenant_id AND other_member.project=named.id
                    AND other_member.principal=actor_id AND other_member.active=1)))
        INTO level, member
        FROM enterprise_projects project
        JOIN enterprise_organizations organization ON organization.id=project.tenant
        WHERE project.tenant=tenant_id AND project.id=definition->>'project' AND project.active=1;
    IF level IS NULL THEN RETURN false; END IF;
    RETURN level=2 OR (level=1 AND member);
END $fn$;
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_origin_reaches(jsonb,text,text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_origin_reaches(jsonb,text,text) FROM PUBLIC;

-- Migration 045's search_document_allowed with the origin clause above added to
-- the per-dependency rules; everything else is unchanged.
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
                    -- Origin labels (migration 046): every document, curated and derived
                    -- ones included, reaches a non-owner only as far as each source's origin allows.
                    AND (policy.definition->>'owner'=actor_id
                        OR search_origin_reaches(policy.definition,tenant_id,actor_id))
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
