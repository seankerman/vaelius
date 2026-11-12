-- The read predicate without a per-item share term (docs/ACCESS_MODEL.md).
--
-- Migration 055 removed per-item sharing: it revoked every passage share and added
-- the CHECK constraint raw_source_grants_originals_only, so no active grant has
-- passages=1. Every share path of the read predicate required exactly such a row
-- (search_passage_granted and search_passage_session_granted both return false
-- without one), so the term could match nothing. This migration removes it from
-- row-level security, as the same change does in the Python oracle
-- (agenthub.workspace_access) and the indexed SQL filter (agenthub.search_permissions):
--
--   reader in item audience  =  owner
--                               or ( not origin_private and not private
--                                    and a role >= reader in the item's workspace
--                                    and in every carried workspace
--                                    and, for a legacy 'team' origin, a direct role in each )
--
-- plus, for recall in a labelled session, the same shape over the session's
-- workspaces. Nothing is widened: every reader the functions below admit was
-- admitted before, and the term that is gone admitted nobody.
--
-- The operator-issued originals grant (agenthub.raw_access) is untouched: the table
-- raw_source_grants, its constraint and the originals check stay as they are.
-- enterprise_documents.raw_granted (the passage-share prefilter) is no longer read
-- by any predicate; it stays 0 and is kept only so older rows and tools still load.
--
-- The same function also applies the `read` recall filter to documents derived
-- from sources in several workspaces (context key `recall_filter`). The search
-- reader role cannot read source links, so the check runs here, where reach is
-- already narrowed. It is a filter, never a permission: it can only return false
-- earlier, and the predicate below is evaluated in full whenever it passes.

-- The reader is in the item's audience (one policy definition; the reader is not
-- checked against ordinary source rules here):
--   the owner; otherwise never when origin_private (origin_ceiling 'private');
--   not private and a role >= reader in the item's workspace;
--   a role >= reader in every carried workspace (origin_projects), and for a
--   legacy origin ceiling of 'team' a direct (non-organization) role in the
--   workspace and every carried workspace.
-- Mirrors agenthub.search_permissions.AUDIENCE and WorkspaceAccessMixin._in_audience.
CREATE OR REPLACE FUNCTION search_reader_in_audience(definition JSONB, tenant_id TEXT, actor_id TEXT,
        delegated TEXT[])
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    found RECORD;
    legacy BOOLEAN;
    carried TEXT;
BEGIN
    IF definition IS NULL OR actor_id IS NULL THEN RETURN false; END IF;
    IF definition->>'owner'=actor_id THEN RETURN true; END IF;
    IF definition->>'origin_ceiling'='private' THEN RETURN false; END IF;
    IF coalesce((definition->>'private')::boolean,false) THEN RETURN false; END IF;
    legacy:=coalesce(definition->>'origin_ceiling','')='team';
    SELECT * INTO found FROM search_workspace_role(tenant_id,actor_id,definition->>'project',delegated);
    IF found.role<1 OR (legacy AND found.direct<1) THEN RETURN false; END IF;
    FOR carried IN SELECT jsonb_array_elements_text(coalesce(definition->'origin_projects','[]'::jsonb)) LOOP
        SELECT * INTO found FROM search_workspace_role(tenant_id,actor_id,carried,delegated);
        IF found.role<1 OR (legacy AND found.direct<1) THEN RETURN false; END IF;
    END LOOP;
    RETURN true;
END $fn$;

-- The session-label term (recall, docs/ACCESS_MODEL.md rule 6): the item's audience
-- contains the session audience. Never origin_private; the item not private and its
-- workspace in S (label->'workspaces'); every carried workspace in S; a legacy
-- 'team' origin needs the workspace and every carried one among the direct
-- audiences (label->'direct'). A private session (or no label) adds nothing.
-- Mirrors agenthub.search_permissions.SESSION and WorkspaceAccessMixin._session_admits.
CREATE OR REPLACE FUNCTION search_session_admits(definition JSONB, label JSONB)
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
    -- IS NOT TRUE: a definition without a workspace is refused, as the other two
    -- layers refuse it.
    IF coalesce((definition->>'private')::boolean,false)
            OR (definition->>'project'=ANY(inside)) IS NOT TRUE THEN
        RETURN false;
    END IF;
    IF NOT carried <@ inside THEN RETURN false; END IF;
    IF definition->>'origin_ceiling'='team' THEN
        direct:=ARRAY(SELECT jsonb_array_elements_text(coalesce(label->'direct','[]'::jsonb)));
        IF NOT (definition->>'project'=ANY(direct) AND carried <@ direct) THEN RETURN false; END IF;
    END IF;
    RETURN true;
END $fn$;

-- Migration 049's function without the share term, and with the recall filter.
-- Every document, passage or derived, needs every source it depends on to be the
-- reader's own or to have the reader in its audience (search_reader_in_audience).
-- Every other source rule (tenant, lifecycle, delegation, ACL, connection readers
-- and freshness, quarantine) applies unchanged. Mirrors
-- agenthub.search_permissions.predicate and EnterpriseStore._document_allowed.
-- Two context keys narrow retrieval and never grant anything: `reach` keeps a
-- 'project' search to that workspace plus passages whose audience is the whole
-- organization; `recall_filter` (a list of workspace ids, already capped by what
-- the request may read) keeps a document when at least one of the sources it
-- depends on is in a listed workspace.
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
    recall_filter TEXT[] := CASE WHEN jsonb_typeof(context->'recall_filter')='array'
        THEN ARRAY(SELECT jsonb_array_elements_text(context->'recall_filter')) END;
BEGIN
    IF context IS NULL OR tenant_id IS NULL OR actor_id IS NULL THEN RETURN false; END IF;
    IF context->>'reach'='project' AND requested_project IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM enterprise_documents ed WHERE ed.id=target
            AND (ed.source_project=requested_project OR ed.representation!='raw'
                OR (ed.raw_shared>0 AND (SELECT bool_and(required.dependency_count>0
                        AND search_audience_is_organization(policy.definition,tenant_id))
                    FROM search_document_policies required
                    LEFT JOIN search_policies policy ON policy.id=required.policy_id
                    WHERE required.document_id=ed.id) IS TRUE))) THEN
        RETURN false;
    END IF;
    -- The `read` recall filter. source_project is the one workspace of every source
    -- (NULL when they span several): then any one source in a listed workspace keeps
    -- the document. This only ever returns false; it admits nothing by itself.
    IF recall_filter IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM enterprise_documents ed WHERE ed.id=target
            AND (ed.source_project=ANY(recall_filter) OR (ed.source_project IS NULL AND EXISTS(
                SELECT 1 FROM enterprise_dependencies kept_dep
                JOIN enterprise_sources kept ON kept.id=kept_dep.source_id
                WHERE kept_dep.document_id=ed.id AND kept.external_project=ANY(recall_filter))))) THEN
        RETURN false;
    END IF;
    RETURN EXISTS(SELECT 1 FROM knowledge_documents d
        JOIN enterprise_documents ed ON ed.id=d.document_id
        WHERE d.document_id=target AND ed.tenant=tenant_id AND ed.active=1
        AND (review IS NULL OR d.document_id=ANY(review))
        -- Passages need `read`. raw_shared is a prefilter only.
        AND (ed.representation!='raw' OR (reads AND (ed.raw_owner=actor_id OR ed.raw_shared>0)))
        AND d.lifecycle='active' AND ((ed.tenant=tenant_id AND ed.active=1 AND d.lifecycle='active'
            AND (ed.internal_project=ANY(scopes::text[])
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
                        OR search_reader_in_audience(policy.definition,tenant_id,actor_id,delegated))
                    -- Recall in a labelled session: the item's audience contains the session's.
                    AND (session_label IS NULL OR search_session_admits(policy.definition,session_label))
                ) IS TRUE)
            FROM search_document_policies required
            LEFT JOIN search_policies policy ON policy.id=required.policy_id
            LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
            WHERE required.document_id=d.document_id)) OR d.document_id=ANY(releases)));
END $fn$;

DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_reader_in_audience(jsonb,text,text,text[]) SET search_path = %I, pg_temp',current_schema());
    EXECUTE format('ALTER FUNCTION search_session_admits(jsonb,jsonb) SET search_path = %I, pg_temp',current_schema());
    EXECUTE format('ALTER FUNCTION search_document_allowed(text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_document_allowed(text) FROM PUBLIC;

-- The share forms and the two share lookups have no caller left.
DROP FUNCTION IF EXISTS search_reader_in_audience(jsonb,text,text,text[],boolean);
DROP FUNCTION IF EXISTS search_session_admits(jsonb,jsonb,boolean);
DROP FUNCTION IF EXISTS search_passage_session_granted(text,text,text,text[],text[]);
DROP FUNCTION IF EXISTS search_passage_granted(text,text,text);
