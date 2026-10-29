-- Fixed session labels (docs/ACCESS_MODEL.md section 7, PR B).
--
-- A recall request may carry a session label. The service turns it into the set S
-- of workspaces whose audience contains the session audience (current materialized
-- membership; agenthub.workspace_access._session_label) and passes it in the search
-- context (`session`). Row-level security then admits only items whose audience
-- contains the session audience: never origin_private; the item's workspace in S when
-- it is not private, or a passage reaching the reader through shares whose subjects
-- contain the session audience; every carried workspace in S; a legacy 'team' origin
-- needs the workspace and every carried one among the direct audiences (SD). A private
-- session (or no label) adds nothing. Mirrors agenthub.search_permissions.SESSION and
-- WorkspaceAccessMixin._session_admits. No table changes.

CREATE OR REPLACE FUNCTION search_session_admits(definition JSONB, label JSONB, granted BOOLEAN)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    inside TEXT[];
    direct TEXT[];
    carried TEXT[];
BEGIN
    IF label IS NULL OR jsonb_typeof(label)!='object' OR coalesce((label->>'private')::boolean,false) THEN
        RETURN true;
    END IF;
    IF definition IS NULL OR definition->>'origin_ceiling'='private' THEN RETURN false; END IF;
    inside:=ARRAY(SELECT jsonb_array_elements_text(coalesce(label->'workspaces','[]'::jsonb)));
    carried:=ARRAY(SELECT jsonb_array_elements_text(coalesce(definition->'origin_projects','[]'::jsonb)));
    IF NOT ((NOT coalesce((definition->>'private')::boolean,false) AND definition->>'project'=ANY(inside))
            OR coalesce(granted,false)) THEN
        RETURN false;
    END IF;
    IF NOT carried <@ inside THEN RETURN false; END IF;
    IF definition->>'origin_ceiling'='team' THEN
        direct:=ARRAY(SELECT jsonb_array_elements_text(coalesce(label->'direct','[]'::jsonb)));
        IF NOT (definition->>'project'=ANY(direct) AND carried <@ direct) THEN RETURN false; END IF;
    END IF;
    RETURN true;
END $fn$;

-- search_passage_granted, and the share's subject contains the session audience
-- (a principal in session_principals or a group in session_groups).
CREATE OR REPLACE FUNCTION search_passage_session_granted(target TEXT, tenant_id TEXT, actor_id TEXT,
        session_principals TEXT[], session_groups TEXT[])
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
BEGIN
    IF actor_id IS NULL OR EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
            WHERE held.tenant=tenant_id AND held.principal=actor_id) THEN
        RETURN false;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM raw_source_grants received WHERE received.tenant=tenant_id
            AND received.revoked IS NULL AND received.passages=1
            AND ((received.grantee_kind='principal' AND received.grantee=actor_id)
                OR (received.grantee_kind='group' AND received.grantee IN (SELECT closure.group_id
                    FROM group_closure closure WHERE closure.tenant=tenant_id AND closure.principal=actor_id)))) THEN
        RETURN false;
    END IF;
    RETURN EXISTS(SELECT 1 FROM enterprise_dependencies dep WHERE dep.document_id=target)
        AND NOT EXISTS(SELECT 1 FROM enterprise_dependencies dep
            JOIN enterprise_sources granted ON granted.id=dep.source_id
            LEFT JOIN search_source_policies granted_mapping ON granted_mapping.source_id=granted.id
            LEFT JOIN search_policies granted_policy ON granted_policy.id=granted_mapping.policy_id
            WHERE dep.document_id=target AND NOT coalesce((
                granted.tenant=tenant_id AND granted.active=1 AND granted.owner!=actor_id
                AND EXISTS(SELECT 1 FROM enterprise_organizations organization
                    JOIN enterprise_projects project ON project.tenant=organization.id
                        AND project.id=granted.external_project AND project.active=1
                    JOIN raw_source_grants grant_row ON grant_row.tenant=granted.tenant
                        AND grant_row.revoked IS NULL AND grant_row.passages=1
                        AND grant_row.granted_by=granted.owner
                    WHERE organization.id=granted.tenant
                    AND (grant_row.issued_with='policy' OR organization.owner_sharing='allowed')
                    AND ((grant_row.scope_kind='source' AND grant_row.scope_id=granted.id)
                        OR (grant_row.scope_kind='session' AND grant_row.scope_id=(
                            SELECT memory.session FROM memories memory WHERE memory.id=granted.id))
                        OR (grant_row.scope_kind='connection'
                            AND grant_row.scope_id=granted_policy.definition->>'connection'))
                    AND ((grant_row.grantee_kind='principal' AND grant_row.grantee=actor_id)
                        OR (grant_row.grantee_kind='group' AND EXISTS(SELECT 1 FROM group_closure closure
                            WHERE closure.tenant=granted.tenant AND closure.group_id=grant_row.grantee
                            AND closure.principal=actor_id)))
                    AND ((grant_row.grantee_kind='principal' AND grant_row.grantee=ANY(session_principals))
                        OR (grant_row.grantee_kind='group' AND grant_row.grantee=ANY(session_groups))))
            ),false));
END $fn$;

-- Migration 048's function, plus the session term on every policy.
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
    reads BOOLEAN := coalesce((context->>'source_read')::boolean,false);
    at_time DOUBLE PRECISION := extract(epoch FROM statement_timestamp());
    session_label JSONB := CASE WHEN jsonb_typeof(context->'session')='object'
        AND NOT coalesce((context->'session'->>'private')::boolean,false) THEN context->'session' END;
    session_principals TEXT[] := ARRAY(SELECT jsonb_array_elements_text(coalesce(context->'session'->'principals','[]'::jsonb)));
    session_groups TEXT[] := ARRAY(SELECT jsonb_array_elements_text(coalesce(context->'session'->'groups','[]'::jsonb)));
BEGIN
    IF context IS NULL OR tenant_id IS NULL OR actor_id IS NULL THEN RETURN false; END IF;
    IF context->>'reach'='project' AND requested_project IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM enterprise_documents ed WHERE ed.id=target
            AND (ed.source_project=requested_project OR ed.representation!='raw'
                OR (ed.raw_granted=1 AND ed.raw_owner IS DISTINCT FROM actor_id AND reads
                    AND search_passage_granted(ed.id,tenant_id,actor_id))
                OR (ed.raw_shared>0 AND (SELECT bool_and(required.dependency_count>0
                        AND search_audience_is_organization(policy.definition,tenant_id))
                    FROM search_document_policies required
                    LEFT JOIN search_policies policy ON policy.id=required.policy_id
                    WHERE required.document_id=ed.id) IS TRUE))) THEN
        RETURN false;
    END IF;
    RETURN EXISTS(SELECT 1 FROM knowledge_documents d
        JOIN enterprise_documents ed ON ed.id=d.document_id
        WHERE d.document_id=target AND ed.tenant=tenant_id AND ed.active=1
        AND (review IS NULL OR d.document_id=ANY(review))
        -- Passages need `read`. raw_shared/raw_granted are prefilters only.
        AND (ed.representation!='raw' OR (reads AND (ed.raw_owner=actor_id OR ed.raw_shared>0 OR ed.raw_granted=1)))
        AND d.lifecycle='active' AND ((ed.tenant=tenant_id AND ed.active=1 AND d.lifecycle='active'
            AND (ed.internal_project=ANY(scopes::text[]) OR (ed.representation='raw' AND ed.raw_granted=1
                AND ed.raw_owner IS DISTINCT FROM actor_id AND reads AND search_passage_granted(ed.id,tenant_id,actor_id))
                OR EXISTS(SELECT 1 FROM cloud_preferences pf
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
                    -- An operator quarantine serves a principal's held sources to that
                    -- principal only; every document that depends on one is held too.
                    AND (coalesce((policy.definition->>'quarantined')::boolean,false)=false
                        OR policy.definition->>'owner'=actor_id)
                    AND (policy.definition->>'owner'=actor_id
                        OR search_reader_in_audience(policy.definition,tenant_id,actor_id,delegated,false)
                        OR (ed.representation='raw' AND ed.raw_granted=1 AND ed.raw_owner IS DISTINCT FROM actor_id
                            AND reads AND search_passage_granted(ed.id,tenant_id,actor_id)
                            AND search_reader_in_audience(policy.definition,tenant_id,actor_id,delegated,true)))
                    -- Recall in a labelled session: the item's audience contains the session's.
                    AND (session_label IS NULL OR search_session_admits(policy.definition,session_label,
                        ed.representation='raw' AND ed.raw_granted=1 AND ed.raw_owner IS DISTINCT FROM actor_id AND reads
                        AND search_passage_session_granted(ed.id,tenant_id,actor_id,session_principals,session_groups)))
                ) IS TRUE)
            FROM search_document_policies required
            LEFT JOIN search_policies policy ON policy.id=required.policy_id
            LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
            WHERE required.document_id=d.document_id)) OR d.document_id=ANY(releases)));
END $fn$;

DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_session_admits(jsonb,jsonb,boolean) SET search_path = %I, pg_temp',current_schema());
    EXECUTE format('ALTER FUNCTION search_passage_session_granted(text,text,text,text[],text[]) SET search_path = %I, pg_temp',current_schema());
    EXECUTE format('ALTER FUNCTION search_document_allowed(text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_passage_session_granted(text,text,text,text[],text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION search_document_allowed(text) FROM PUBLIC;
