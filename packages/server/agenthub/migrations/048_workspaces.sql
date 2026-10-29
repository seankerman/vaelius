-- Workspaces, groups, access lists and repository links (docs/ACCESS_MODEL.md, PR A).
-- Applied migrations stay immutable. This migration is deliberately conservative:
-- nothing becomes readable by anyone who could not read it before.
--
-- * A project becomes a workspace with the SAME id (enterprise_projects is kept as
--   the workspace table; name = id). Its explicit members and the directory (SCIM)
--   groups mapped to it become a local group `<project>-members`, which gets one
--   `writer` entry. No organization entry and no admin entry is created: tenant
--   administrators manage migrated workspaces until they add admins.
-- * Every existing item becomes private to its owner (enterprise_sources.private=1),
--   and every existing raw grant and share is dropped. Owners and admins re-share.
-- * The person, project and tenant settings map to their nearest equivalents
--   (contributions_default, item_default, widest_entry); the old columns are frozen.
-- * The single read predicate "reader is in the item's audience" replaces the
--   sharing ladder in row-level security (search_reader_in_audience); the Python
--   oracle (agenthub.workspace_access) and the indexed SQL filter
--   (agenthub.search_permissions) mirror it.

-- Tenant policy ---------------------------------------------------------------
ALTER TABLE enterprise_organizations ADD COLUMN IF NOT EXISTS workspace_creation TEXT NOT NULL DEFAULT 'anyone'
    CONSTRAINT enterprise_organizations_workspace_creation CHECK (workspace_creation IN ('anyone','admins'));
ALTER TABLE enterprise_organizations ADD COLUMN IF NOT EXISTS widest_entry TEXT NOT NULL DEFAULT 'organization'
    CONSTRAINT enterprise_organizations_widest_entry CHECK (widest_entry IN ('none','people_and_groups','organization'));
UPDATE enterprise_organizations SET widest_entry=CASE max_sharing WHEN 'private' THEN 'none'
    WHEN 'team' THEN 'people_and_groups' ELSE 'organization' END;

-- Workspaces (the project table keeps every id) ------------------------------------
ALTER TABLE enterprise_projects ADD COLUMN IF NOT EXISTS name TEXT;
UPDATE enterprise_projects SET name=id WHERE name IS NULL;
ALTER TABLE enterprise_projects ALTER COLUMN name SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS enterprise_projects_name ON enterprise_projects(tenant,name);
-- A workspace inserted without a name (older code paths, snapshot imports) is named by its id.
CREATE OR REPLACE FUNCTION enterprise_projects_default_name() RETURNS TRIGGER LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.name := coalesce(NEW.name,NEW.id);
    RETURN NEW;
END $fn$;
DROP TRIGGER IF EXISTS enterprise_projects_default_name ON enterprise_projects;
CREATE TRIGGER enterprise_projects_default_name BEFORE INSERT ON enterprise_projects
    FOR EACH ROW EXECUTE FUNCTION enterprise_projects_default_name();
ALTER TABLE enterprise_projects ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE enterprise_projects ADD COLUMN IF NOT EXISTS created DOUBLE PRECISION;
-- The organization entry's role, denormalized: 0 none, 1 reader, 2 writer, 3 admin.
ALTER TABLE enterprise_projects ADD COLUMN IF NOT EXISTS org_role SMALLINT NOT NULL DEFAULT 0
    CONSTRAINT enterprise_projects_org_role CHECK (org_role BETWEEN 0 AND 3);
ALTER TABLE enterprise_projects ADD COLUMN IF NOT EXISTS item_default TEXT NOT NULL DEFAULT 'shared'
    CONSTRAINT enterprise_projects_item_default CHECK (item_default IN ('shared','private'));
-- Changes whenever anything that decides access to the workspace changes.
ALTER TABLE enterprise_projects ADD COLUMN IF NOT EXISTS epoch BIGINT NOT NULL DEFAULT 1;
UPDATE enterprise_projects p SET item_default='private' FROM enterprise_organizations o
    WHERE o.id=p.tenant AND coalesce(p.max_sharing,o.default_max_sharing)='private';
CREATE INDEX IF NOT EXISTS enterprise_projects_organization_entry ON enterprise_projects(tenant,id)
    WHERE org_role>0 AND active=1;

-- The person's own default for new items (replaces max_sharing).
ALTER TABLE enterprise_principals ADD COLUMN IF NOT EXISTS contributions_default TEXT NOT NULL DEFAULT 'shared'
    CONSTRAINT enterprise_principals_contributions_default CHECK (contributions_default IN ('shared','private'));
UPDATE enterprise_principals SET contributions_default='private' WHERE max_sharing='private';

-- Groups ------------------------------------------------------------------------
-- github (PR C) and scim groups are synced and read-only here; local groups are
-- managed by their admins, or by the workspace's admins when `workspace` is set.
-- A group contributes members only while active, current and (when it belongs to
-- a workspace) that workspace is active.
CREATE TABLE IF NOT EXISTS groups(
    tenant TEXT NOT NULL,
    id TEXT NOT NULL CONSTRAINT groups_id CHECK (id ~ '^[A-Za-z0-9_-]{1,128}$'),
    name TEXT NOT NULL CONSTRAINT groups_name CHECK (name ~ '^[A-Za-z0-9_-]{1,128}$'),
    source TEXT NOT NULL CONSTRAINT groups_source CHECK (source IN ('github','scim','local')),
    external_id TEXT,
    display TEXT,
    workspace TEXT,
    sync_state TEXT NOT NULL DEFAULT 'current' CONSTRAINT groups_sync_state CHECK (sync_state IN ('current','stale','failed')),
    last_synced DOUBLE PRECISION,
    active SMALLINT NOT NULL DEFAULT 1,
    created_by TEXT,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,id),
    FOREIGN KEY(tenant) REFERENCES enterprise_organizations(id),
    FOREIGN KEY(tenant,workspace) REFERENCES enterprise_projects(tenant,id));
CREATE UNIQUE INDEX IF NOT EXISTS groups_name_unique ON groups(tenant,name);
CREATE UNIQUE INDEX IF NOT EXISTS groups_external ON groups(tenant,source,external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS group_edges(
    tenant TEXT NOT NULL,
    group_id TEXT NOT NULL,
    member_kind TEXT NOT NULL CONSTRAINT group_edges_member_kind CHECK (member_kind IN ('principal','group')),
    member TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CONSTRAINT group_edges_role CHECK (role IN ('member','admin')),
    added_by TEXT,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,group_id,member_kind,member),
    FOREIGN KEY(tenant,group_id) REFERENCES groups(tenant,id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS group_edges_member ON group_edges(tenant,member_kind,member);
-- Transitive principal members of every group, rewritten in the same transaction
-- as any edge change or sync. Checks never walk the graph at query time.
CREATE TABLE IF NOT EXISTS group_closure(
    tenant TEXT NOT NULL,
    group_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    PRIMARY KEY(tenant,group_id,principal));
CREATE INDEX IF NOT EXISTS group_closure_principal ON group_closure(tenant,principal);

-- Access lists -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace_entries(
    tenant TEXT NOT NULL,
    workspace TEXT NOT NULL,
    subject_kind TEXT NOT NULL CONSTRAINT workspace_entries_subject_kind
        CHECK (subject_kind IN ('principal','group','organization')),
    subject TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CONSTRAINT workspace_entries_role CHECK (role IN ('reader','writer','admin')),
    granted_by TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,workspace,subject_kind,subject),
    CONSTRAINT workspace_entries_organization_subject CHECK ((subject_kind='organization')=(subject='')),
    FOREIGN KEY(tenant,workspace) REFERENCES enterprise_projects(tenant,id));
CREATE INDEX IF NOT EXISTS workspace_entries_subject ON workspace_entries(tenant,subject_kind,subject);
-- Effective role from principal and group entries (never the organization entry),
-- maintained with the closure. principal_role comes from principal entries and
-- group_role from group entries, so the tenant's widest_entry applies at query time.
CREATE TABLE IF NOT EXISTS workspace_access(
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    workspace TEXT NOT NULL,
    principal_role SMALLINT NOT NULL DEFAULT 0,
    group_role SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY(tenant,principal,workspace));
CREATE INDEX IF NOT EXISTS workspace_access_workspace ON workspace_access(tenant,workspace);

-- Repository links: one normalized remote links to one workspace (per tenant).
CREATE TABLE IF NOT EXISTS workspace_repos(
    tenant TEXT NOT NULL,
    remote TEXT NOT NULL,
    workspace TEXT NOT NULL,
    linked_by TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,remote),
    FOREIGN KEY(tenant,workspace) REFERENCES enterprise_projects(tenant,id));
CREATE INDEX IF NOT EXISTS workspace_repos_workspace ON workspace_repos(tenant,workspace);

-- Items -----------------------------------------------------------------------------
-- private: owner only, apart from explicit shares. Every existing item is private.
ALTER TABLE enterprise_sources ADD COLUMN IF NOT EXISTS private SMALLINT NOT NULL DEFAULT 0
    CONSTRAINT enterprise_sources_private CHECK (private IN (0,1));
UPDATE enterprise_sources SET private=1;
-- raw_shared is now "the passage's own source is not private" (0 private, 1 workspace).
UPDATE enterprise_documents SET raw_shared=0,raw_granted=0;
-- Every derived document is capped by the audience of every source it depends on.
UPDATE enterprise_documents SET level_capped=1 WHERE level_capped=0;

-- Shares: grantees are principals or groups; existing grants are dropped.
DELETE FROM raw_source_grants;
DO $grants$
DECLARE item RECORD;
BEGIN
    FOR item IN SELECT c.conname FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
        WHERE c.contype='c' AND c.connamespace=current_schema()::regnamespace
        AND c.conrelid='raw_source_grants'::regclass AND a.attname='grantee_kind'
    LOOP
        EXECUTE format('ALTER TABLE raw_source_grants DROP CONSTRAINT %I',item.conname);
    END LOOP;
END $grants$;
ALTER TABLE raw_source_grants ADD CONSTRAINT raw_source_grants_grantee_kind
    CHECK (grantee_kind IN ('principal','group'));

-- Owner sharing changes record shared/private from now on.
DO $changes$
DECLARE item RECORD;
BEGIN
    FOR item IN SELECT c.conname FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
        WHERE c.contype='c' AND c.connamespace=current_schema()::regnamespace
        AND c.conrelid='enterprise_sharing_changes'::regclass AND a.attname IN ('from_level','to_level','requested')
    LOOP
        EXECUTE format('ALTER TABLE enterprise_sharing_changes DROP CONSTRAINT %I',item.conname);
    END LOOP;
END $changes$;
ALTER TABLE enterprise_sharing_changes ADD CONSTRAINT enterprise_sharing_changes_levels CHECK (
    from_level IN ('private','team','organization','shared') AND to_level IN ('private','team','organization','shared')
    AND requested IN ('private','team','organization','shared'));

-- legacy_actions bit 4: credentials and refresh families issued before this
-- migration that hold `share` (explicitly, or through bit 2) also hold `manage`.
UPDATE enterprise_credentials SET legacy_actions=legacy_actions|4
    WHERE actions::jsonb ? 'share' OR (legacy_actions&2=2 AND actions::jsonb ? 'withdraw');
UPDATE enterprise_credential_families SET legacy_actions=legacy_actions|4
    WHERE actions::jsonb ? 'share' OR (legacy_actions&2=2 AND actions::jsonb ? 'withdraw');

-- Maintenance functions ---------------------------------------------------------------
CREATE OR REPLACE FUNCTION access_role_rank(role TEXT) RETURNS SMALLINT LANGUAGE sql IMMUTABLE AS $fn$
    SELECT (CASE role WHEN 'reader' THEN 1 WHEN 'writer' THEN 2 WHEN 'admin' THEN 3 ELSE 0 END)::smallint
$fn$;

-- Rewrite workspace_access and the denormalized organization role of some
-- workspaces (all of the tenant's when `targets` is NULL), and move their epoch.
CREATE OR REPLACE FUNCTION access_refresh_workspaces(tenant_id TEXT, targets TEXT[]) RETURNS VOID
LANGUAGE plpgsql AS $fn$
BEGIN
    IF targets IS NULL THEN
        SELECT coalesce(array_agg(id),'{}') INTO targets FROM enterprise_projects WHERE tenant=tenant_id;
    END IF;
    IF cardinality(targets)=0 THEN RETURN; END IF;
    DELETE FROM workspace_access WHERE tenant=tenant_id AND workspace=ANY(targets);
    INSERT INTO workspace_access(tenant,principal,workspace,principal_role,group_role)
    SELECT tenant_id,x.principal,x.workspace,max(x.principal_role),max(x.group_role) FROM (
        SELECT e.subject principal,e.workspace,access_role_rank(e.role) principal_role,0::smallint group_role
        FROM workspace_entries e WHERE e.tenant=tenant_id AND e.workspace=ANY(targets) AND e.subject_kind='principal'
        UNION ALL
        SELECT c.principal,e.workspace,0::smallint,access_role_rank(e.role)
        FROM workspace_entries e JOIN group_closure c ON c.tenant=e.tenant AND c.group_id=e.subject
        WHERE e.tenant=tenant_id AND e.workspace=ANY(targets) AND e.subject_kind='group'
    ) x GROUP BY x.principal,x.workspace;
    UPDATE enterprise_projects p SET org_role=coalesce((SELECT access_role_rank(e.role) FROM workspace_entries e
            WHERE e.tenant=p.tenant AND e.workspace=p.id AND e.subject_kind='organization'),0),epoch=p.epoch+1
        WHERE p.tenant=tenant_id AND p.id=ANY(targets);
END $fn$;

-- Rebuild the closure of some groups and of every group that contains them
-- (all of the tenant's groups when `changed` is NULL), then the workspaces whose
-- entries name any of them. Returns those workspaces.
CREATE OR REPLACE FUNCTION access_refresh_groups(tenant_id TEXT, changed TEXT[]) RETURNS TEXT[]
LANGUAGE plpgsql AS $fn$
DECLARE
    affected TEXT[];
    workspaces TEXT[];
BEGIN
    IF changed IS NULL THEN
        SELECT coalesce(array_agg(id),'{}') INTO affected FROM groups WHERE tenant=tenant_id;
    ELSE
        WITH RECURSIVE up(id) AS (
            SELECT unnest(changed)
            UNION
            SELECT e.group_id FROM group_edges e JOIN up ON e.tenant=tenant_id
                AND e.member_kind='group' AND e.member=up.id)
        SELECT coalesce(array_agg(DISTINCT id),'{}') INTO affected FROM up;
    END IF;
    DELETE FROM group_closure WHERE tenant=tenant_id AND group_id=ANY(affected);
    WITH RECURSIVE live AS MATERIALIZED (
        SELECT g.id FROM groups g LEFT JOIN enterprise_projects w ON w.tenant=g.tenant AND w.id=g.workspace
        WHERE g.tenant=tenant_id AND g.active=1 AND g.sync_state='current'
        AND (g.workspace IS NULL OR w.active=1)
    ), down(root,id) AS (
        SELECT live.id,live.id FROM live WHERE live.id=ANY(affected)
        UNION
        SELECT down.root,e.member FROM down JOIN group_edges e ON e.tenant=tenant_id AND e.group_id=down.id
            AND e.member_kind='group' JOIN live ON live.id=e.member
    )
    INSERT INTO group_closure(tenant,group_id,principal)
    SELECT DISTINCT tenant_id,down.root,e.member FROM down
    JOIN group_edges e ON e.tenant=tenant_id AND e.group_id=down.id AND e.member_kind='principal';
    SELECT coalesce(array_agg(DISTINCT workspace),'{}') INTO workspaces FROM workspace_entries
        WHERE tenant=tenant_id AND subject_kind='group' AND subject=ANY(affected);
    PERFORM access_refresh_workspaces(tenant_id,workspaces);
    RETURN workspaces;
END $fn$;

-- Migrate memberships ------------------------------------------------------------------
-- One local `<project>-members` group per project, holding the explicit members and
-- nesting each SCIM group mapped to the project; entry group -> writer.
INSERT INTO groups(tenant,id,name,source,workspace,created)
SELECT p.tenant,p.id||'-members',p.id||'-members','local',p.id,extract(epoch FROM now())
FROM enterprise_projects p ON CONFLICT DO NOTHING;
INSERT INTO groups(tenant,id,name,source,external_id,display,sync_state,active,created)
SELECT o.id,'scim-'||substr(encode(sha256(convert_to(r.external_id,'UTF8')),'hex'),1,24),
    'scim-'||substr(encode(sha256(convert_to(r.external_id,'UTF8')),'hex'),1,24),'scim',r.external_id,
    left(coalesce(r.payload_json::jsonb->>'display',''),200),
    CASE WHEN r.requires_reconcile=1 THEN 'stale' ELSE 'current' END,r.active,extract(epoch FROM now())
FROM cloud_directory_resources r CROSS JOIN enterprise_organizations o
WHERE r.kind='group' ON CONFLICT DO NOTHING;
-- SCIM group members: the bound, active principals of its SCIM members.
INSERT INTO group_edges(tenant,group_id,member_kind,member,created)
SELECT DISTINCT g.tenant,g.id,'principal',u.principal,extract(epoch FROM now())
FROM groups g JOIN cloud_directory_resources r ON r.kind='group' AND r.external_id=g.external_id
CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(r.payload_json::jsonb->'members','[]'::jsonb)) m(external)
JOIN cloud_directory_users u ON u.external_id=m.external
JOIN enterprise_principals who ON who.tenant=g.tenant AND who.id=u.principal AND who.active=1
WHERE g.source='scim' ON CONFLICT DO NOTHING;
INSERT INTO group_edges(tenant,group_id,member_kind,member,created)
SELECT DISTINCT g.tenant,p.id||'-members','group',g.id,extract(epoch FROM now())
FROM cloud_directory_group_projects x JOIN groups g ON g.source='scim' AND g.external_id=x.external_id
JOIN enterprise_projects p ON p.tenant=g.tenant AND p.id=x.project ON CONFLICT DO NOTHING;
-- Explicit members (directly enrolled, not derived from a directory group).
INSERT INTO group_edges(tenant,group_id,member_kind,member,created)
SELECT m.tenant,m.project||'-members','principal',m.principal,extract(epoch FROM now())
FROM enterprise_memberships m WHERE m.active=1
AND (EXISTS(SELECT 1 FROM cloud_explicit_memberships x WHERE x.project=m.project AND x.principal=m.principal)
     OR NOT EXISTS(SELECT 1 FROM cloud_directory_memberships d WHERE d.project=m.project AND d.principal=m.principal))
ON CONFLICT DO NOTHING;
INSERT INTO workspace_entries(tenant,workspace,subject_kind,subject,role,granted_by,created)
SELECT p.tenant,p.id,'group',p.id||'-members','writer','migration_048',extract(epoch FROM now())
FROM enterprise_projects p ON CONFLICT DO NOTHING;
UPDATE enterprise_projects SET created=extract(epoch FROM now()) WHERE created IS NULL;
SELECT access_refresh_groups(o.id,NULL) FROM enterprise_organizations o;

-- The membership tables are replaced by groups and access lists. A read-only view
-- keeps the old name for one release, for operator scripts.
DROP TABLE cloud_explicit_memberships;
DROP TABLE cloud_directory_memberships;
DROP TABLE cloud_directory_group_projects;
DROP TABLE enterprise_memberships;
CREATE VIEW enterprise_memberships AS
SELECT a.tenant,a.workspace AS project,a.principal,1 AS active FROM workspace_access a
JOIN enterprise_organizations o ON o.id=a.tenant
WHERE CASE o.widest_entry WHEN 'none' THEN a.principal_role=3 ELSE greatest(a.principal_role,a.group_role)>=1 END;

-- The read predicate -------------------------------------------------------------------
-- (role, direct) of one principal in one workspace, 0 none .. 3 admin. `direct` is
-- the role from principal and group entries; `role` also counts the organization
-- entry, which reaches active, non-quarantined principals through credentials
-- that are not delegated. Entries wider than the tenant's widest_entry are ignored.
-- A delegated credential has no role outside its delegated workspaces, and an
-- archived workspace gives nobody a role. Mirrors WorkspaceAccessMixin._roles.
CREATE OR REPLACE FUNCTION search_workspace_role(tenant_id TEXT, actor_id TEXT, workspace_id TEXT,
        delegated TEXT[], OUT role SMALLINT, OUT direct SMALLINT)
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    organization_role SMALLINT;
BEGIN
    role:=0;direct:=0;
    IF actor_id IS NULL OR workspace_id IS NULL
            OR (delegated IS NOT NULL AND NOT workspace_id=ANY(delegated)) THEN
        RETURN;
    END IF;
    SELECT CASE o.widest_entry WHEN 'none' THEN CASE WHEN a.principal_role=3 THEN 3 ELSE 0 END
                ELSE greatest(coalesce(a.principal_role,0),coalesce(a.group_role,0)) END,
            CASE WHEN o.widest_entry='organization' AND p.org_role>0 AND delegated IS NULL AND EXISTS(
                SELECT 1 FROM enterprise_principals reader WHERE reader.tenant=tenant_id AND reader.id=actor_id
                AND reader.active=1 AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                    WHERE held.tenant=reader.tenant AND held.principal=reader.id)) THEN p.org_role ELSE 0 END
        INTO direct,organization_role
        FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
        LEFT JOIN workspace_access a ON a.tenant=p.tenant AND a.principal=actor_id AND a.workspace=p.id
        WHERE p.tenant=tenant_id AND p.id=workspace_id AND p.active=1;
    direct:=coalesce(direct,0);
    role:=greatest(direct,coalesce(organization_role,0));
END $fn$;

-- The reader is in the item's audience (one policy definition; the reader is not
-- checked against ordinary source rules here):
--   the owner; otherwise never when origin_private (origin_ceiling 'private');
--   the inherited path: not private and a role >= reader in the item's workspace;
--   the share path (via_share, the caller checked the share itself): no
--   workspace role needed;
--   both paths: a role >= reader in every carried workspace (origin_projects),
--   and for a legacy origin ceiling of 'team' a direct (non-organization) role in
--   the workspace and every carried workspace.
-- Mirrors agenthub.search_permissions.AUDIENCE and WorkspaceAccessMixin._in_audience.
CREATE OR REPLACE FUNCTION search_reader_in_audience(definition JSONB, tenant_id TEXT, actor_id TEXT,
        delegated TEXT[], via_share BOOLEAN)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    found RECORD;
    legacy BOOLEAN;
    carried TEXT;
BEGIN
    IF definition IS NULL OR actor_id IS NULL THEN RETURN false; END IF;
    IF definition->>'owner'=actor_id THEN RETURN true; END IF;
    IF definition->>'origin_ceiling'='private' THEN RETURN false; END IF;
    IF NOT via_share AND coalesce((definition->>'private')::boolean,false) THEN RETURN false; END IF;
    legacy:=coalesce(definition->>'origin_ceiling','')='team';
    IF NOT via_share OR legacy THEN
        SELECT * INTO found FROM search_workspace_role(tenant_id,actor_id,definition->>'project',delegated);
        IF (NOT via_share AND found.role<1) OR (legacy AND found.direct<1) THEN RETURN false; END IF;
    END IF;
    FOR carried IN SELECT jsonb_array_elements_text(coalesce(definition->'origin_projects','[]'::jsonb)) LOOP
        SELECT * INTO found FROM search_workspace_role(tenant_id,actor_id,carried,delegated);
        IF found.role<1 OR (legacy AND found.direct<1) THEN RETURN false; END IF;
    END LOOP;
    RETURN true;
END $fn$;

-- The audience of one policy definition is the whole organization (used only to
-- narrow retrieval reach; it grants nothing).
CREATE OR REPLACE FUNCTION search_audience_is_organization(definition JSONB, tenant_id TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $fn$
    SELECT definition IS NOT NULL AND NOT coalesce((definition->>'private')::boolean,false)
        AND NOT coalesce((definition->>'quarantined')::boolean,false)
        AND coalesce(definition->>'origin_ceiling','organization')='organization'
        AND EXISTS(SELECT 1 FROM enterprise_organizations o WHERE o.id=tenant_id AND o.widest_entry='organization')
        AND NOT EXISTS(SELECT 1 FROM (SELECT definition->>'project' AS id UNION ALL
                SELECT jsonb_array_elements_text(coalesce(definition->'origin_projects','[]'::jsonb))) named
            WHERE NOT EXISTS(SELECT 1 FROM enterprise_projects p WHERE p.tenant=tenant_id AND p.id=named.id
                AND p.active=1 AND p.org_role>0))
$fn$;

-- Invoker functions over tables the service role already reads: no privilege is
-- added, so the service may call them directly (explain, labels, audience sizes).
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_workspace_role(text,text,text,text[]) SET search_path = %I, pg_temp',current_schema());
    EXECUTE format('ALTER FUNCTION search_reader_in_audience(jsonb,text,text,text[],boolean) SET search_path = %I, pg_temp',current_schema());
    EXECUTE format('ALTER FUNCTION search_audience_is_organization(jsonb,text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
DROP FUNCTION IF EXISTS search_passage_shared(jsonb,text,text,integer);
DROP FUNCTION IF EXISTS search_origin_reaches(jsonb,text,text);

-- Policy definitions: `private` replaces `sharing` and `visibility`.
CREATE OR REPLACE VIEW search_source_policy_definitions AS
SELECT s.id AS source_id,jsonb_build_object(
    'tenant',s.tenant,'owner',s.owner,'project',s.external_project,'active',s.active,
    'has_revision',r.source_id IS NOT NULL,'connection',r.connection,
    'has_acl',a.source_id IS NOT NULL,'acl_state',a.state,'acl_until',a.valid_until
) || CASE WHEN s.principal_quarantine=1 THEN '{"quarantined":true}'::jsonb ELSE '{}'::jsonb END
  || CASE WHEN s.private=1 THEN '{"private":true}'::jsonb ELSE '{}'::jsonb END
  || CASE WHEN s.origin_ceiling IN ('private','team') THEN jsonb_build_object('origin_ceiling',s.origin_ceiling)
          ELSE '{}'::jsonb END
  || CASE WHEN s.origin_projects!='[]' THEN jsonb_build_object('origin_projects',s.origin_projects::jsonb)
          ELSE '{}'::jsonb END AS definition
FROM enterprise_sources s
LEFT JOIN backend_source_revisions r ON r.source_id=s.id
LEFT JOIN cloud_source_acl a ON a.source_id=s.id;

-- An explicit share of every source a passage depends on, to the reader or a
-- group the reader belongs to (group_closure). Never: revoked; owner-issued while
-- the operator restricts owner sharing; in an archived workspace; to a quarantined
-- reader. The audience's carried-workspace and origin rules are the caller's.
-- Mirrors agenthub.search_permissions.SOURCE_GRANTED and WorkspaceAccessMixin._shared_with.
CREATE OR REPLACE FUNCTION search_passage_granted(target TEXT, tenant_id TEXT, actor_id TEXT)
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
                            AND closure.principal=actor_id))))
            ),false));
END $fn$;
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_passage_granted(text,text,text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_passage_granted(text,text,text) FROM PUBLIC;

-- Migration 047's function with one audience predicate: every document, passage or
-- derived, needs every source it depends on to be the reader's own or to have the
-- reader in its audience (search_reader_in_audience); a passage may instead be read
-- through an explicit share of every source (search_passage_granted), which still
-- obeys the carried-workspace and origin rules. Every other source rule (tenant,
-- lifecycle, delegation, ACL, connection readers and freshness, quarantine) applies
-- unchanged. Mirrors agenthub.search_permissions.predicate and
-- EnterpriseStore._document_allowed. Retrieval reach (context key `reach`) narrows
-- a 'project' search to that workspace plus passages whose audience is the whole
-- organization; it never grants anything.
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
                ) IS TRUE)
            FROM search_document_policies required
            LEFT JOIN search_policies policy ON policy.id=required.policy_id
            LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
            WHERE required.document_id=d.document_id)) OR d.document_id=ANY(releases)));
END $fn$;
DO $setup$ BEGIN
    EXECUTE format('ALTER FUNCTION search_document_allowed(text) SET search_path = %I, pg_temp',current_schema());
END $setup$;
REVOKE ALL ON FUNCTION search_document_allowed(text) FROM PUBLIC;

SELECT search_rebuild_permissions();
