-- Membership is binary: `member` (read and write from the workspace's repositories)
-- or `admin` (a member who also manages membership and settings) (owner decision,
-- docs/ACCESS_MODEL.md). The `reader` < `writer` < `admin` ladder of migration 048
-- collapses:
--
-- * principal and group entries with `reader` or `writer` become `member`; `admin`
--   stays `admin`;
-- * the organization entry is the workspace's visibility, not a membership: it only
--   ever lets everyone in the tenant read. It is stored as `reader`, and an older
--   `writer` or `admin` organization entry becomes `reader` (everyone keeps reading;
--   writing needs membership).
--
-- The rank numbers do not change (1 read, 2 member, 3 admin), so the read predicate
-- in Python (agenthub.workspace_access), the indexed SQL filter
-- (agenthub.search_permissions) and row-level security (search_workspace_role over
-- workspace_access and org_role) keep their exact shape; only the role name to rank
-- mapping changes. Every workspace is re-materialized, which also moves its epoch so
-- clients refresh their descriptors.
ALTER TABLE workspace_entries DROP CONSTRAINT IF EXISTS workspace_entries_role;
UPDATE workspace_entries SET role='member' WHERE subject_kind IN ('principal','group') AND role IN ('reader','writer');
UPDATE workspace_entries SET role='reader' WHERE subject_kind='organization' AND role<>'reader';
ALTER TABLE workspace_entries ADD CONSTRAINT workspace_entries_role CHECK (
    (subject_kind='organization' AND role='reader')
    OR (subject_kind IN ('principal','group') AND role IN ('member','admin')));

CREATE OR REPLACE FUNCTION access_role_rank(role TEXT) RETURNS SMALLINT LANGUAGE sql IMMUTABLE AS $fn$
    SELECT (CASE role WHEN 'reader' THEN 1 WHEN 'member' THEN 2 WHEN 'admin' THEN 3 ELSE 0 END)::smallint
$fn$;

SELECT access_refresh_workspaces(o.id,NULL) FROM enterprise_organizations o;

-- The organization entry only reads: 0 none, 1 everyone in the tenant reads.
ALTER TABLE enterprise_projects DROP CONSTRAINT IF EXISTS enterprise_projects_org_role;
ALTER TABLE enterprise_projects ADD CONSTRAINT enterprise_projects_org_role CHECK (org_role IN (0,1));
