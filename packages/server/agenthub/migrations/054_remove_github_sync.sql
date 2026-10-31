-- Remove GitHub team sync and the GitHub identity link (owner decision; migration
-- 050 added them). Vaelius takes nothing from GitHub: team repository permissions
-- are not memory audiences, and syncing them was accident-prone.
--
-- Every `github` group goes, with its access-list entries, its nesting edges in
-- other groups, its closure rows and every share made to it (revoked, kept for
-- audit). Removing a group can only remove access, never add it. Groups that
-- contained one, and the workspaces that named one (directly or through such a
-- group), are re-materialized with the 048 refresh functions, which also move
-- their epochs so clients refresh their descriptors. Local and SCIM groups, and the
-- last-admin rule over them, are unchanged: a synced group never counted as an
-- admin. Then the GitHub tables and the `github` group source are dropped.
DO $migration$
DECLARE
    tenant_row RECORD;
    removed TEXT[];
    parents TEXT[];
    named TEXT[];
BEGIN
    FOR tenant_row IN SELECT DISTINCT tenant FROM groups WHERE source='github' LOOP
        SELECT array_agg(id) INTO removed FROM groups WHERE tenant=tenant_row.tenant AND source='github';
        SELECT coalesce(array_agg(DISTINCT group_id),'{}') INTO parents FROM group_edges
            WHERE tenant=tenant_row.tenant AND member_kind='group' AND member=ANY(removed)
            AND NOT group_id=ANY(removed);
        SELECT coalesce(array_agg(DISTINCT workspace),'{}') INTO named FROM workspace_entries
            WHERE tenant=tenant_row.tenant AND subject_kind='group' AND subject=ANY(removed);
        DELETE FROM workspace_entries WHERE tenant=tenant_row.tenant AND subject_kind='group'
            AND subject=ANY(removed);
        DELETE FROM group_edges WHERE tenant=tenant_row.tenant AND member_kind='group' AND member=ANY(removed);
        DELETE FROM group_closure WHERE tenant=tenant_row.tenant AND group_id=ANY(removed);
        UPDATE raw_source_grants SET revoked=extract(epoch FROM now())
            WHERE tenant=tenant_row.tenant AND grantee_kind='group' AND grantee=ANY(removed) AND revoked IS NULL;
        DELETE FROM groups WHERE tenant=tenant_row.tenant AND id=ANY(removed);
        -- Containing groups (and their ancestors), then every workspace they or the
        -- removed groups were named in.
        PERFORM access_refresh_groups(tenant_row.tenant, parents);
        PERFORM access_refresh_workspaces(tenant_row.tenant, named);
    END LOOP;
END $migration$;

DROP INDEX IF EXISTS groups_github_freshness;
DROP TABLE IF EXISTS github_webhook_deliveries;
DROP TABLE IF EXISTS github_team_members;
DROP TABLE IF EXISTS github_teams;
DROP TABLE IF EXISTS github_installations;
DROP TABLE IF EXISTS identity_link_flows;
DROP TABLE IF EXISTS identity_links;
ALTER TABLE groups DROP CONSTRAINT IF EXISTS groups_source;
ALTER TABLE groups ADD CONSTRAINT groups_source CHECK (source IN ('scim','local'));
