-- GitHub App team sync and the device-flow identity link (docs/ACCESS_MODEL.md
-- section 3, PR C). agenthub.github_sync owns every write to these tables.
--
-- A GitHub team is a `github` group (id `gh-<team id>`). The App's view of each
-- team's members is kept as a snapshot keyed by the numeric GitHub user id; a
-- member becomes a group edge only through an active, device-flow-verified
-- identity link of an active principal. Unlinked members grant nothing.

-- Verified links between a principal and a GitHub account, by numeric user id
-- (logins can be renamed). One active link per account and per principal in a
-- tenant; unlinked and replaced links stay as history.
CREATE TABLE IF NOT EXISTS identity_links(
    tenant TEXT NOT NULL,
    provider TEXT NOT NULL CONSTRAINT identity_links_provider CHECK (provider IN ('github')),
    external_id TEXT NOT NULL CONSTRAINT identity_links_external_id CHECK (external_id ~ '^[1-9][0-9]{0,19}$'),
    principal TEXT NOT NULL,
    login TEXT NOT NULL CONSTRAINT identity_links_login CHECK (login ~ '^[A-Za-z0-9-]{1,39}$'),
    active SMALLINT NOT NULL DEFAULT 1 CONSTRAINT identity_links_active CHECK (active IN (0,1)),
    linked DOUBLE PRECISION NOT NULL,
    linked_by TEXT NOT NULL,
    deactivated DOUBLE PRECISION,
    deactivated_by TEXT,
    PRIMARY KEY(tenant,provider,external_id,linked),
    FOREIGN KEY(tenant,principal) REFERENCES enterprise_principals(tenant,id));
CREATE UNIQUE INDEX IF NOT EXISTS identity_links_account ON identity_links(tenant,provider,external_id) WHERE active=1;
CREATE UNIQUE INDEX IF NOT EXISTS identity_links_principal ON identity_links(tenant,provider,principal) WHERE active=1;

-- Pending device-flow authorizations. The device code never leaves the service;
-- the client holds only the opaque flow id. Rows are deleted when the flow ends.
CREATE TABLE IF NOT EXISTS identity_link_flows(
    tenant TEXT NOT NULL,
    id TEXT NOT NULL CONSTRAINT identity_link_flows_id CHECK (id ~ '^[0-9a-f]{32}$'),
    principal TEXT NOT NULL,
    credential_digest TEXT NOT NULL,
    device_code TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    next_poll DOUBLE PRECISION NOT NULL,
    expires DOUBLE PRECISION NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS identity_link_flows_principal ON identity_link_flows(tenant,principal);

-- Installations configured for this tenant and their sync health.
CREATE TABLE IF NOT EXISTS github_installations(
    tenant TEXT NOT NULL,
    installation BIGINT NOT NULL,
    org TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CONSTRAINT github_installations_state
        CHECK (state IN ('active','suspended','deleted')),
    last_success DOUBLE PRECISION,
    last_attempt DOUBLE PRECISION,
    last_error TEXT,
    next_reconcile DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY(tenant,installation));

-- Teams seen in the last reconcile, with their parent team (nesting).
CREATE TABLE IF NOT EXISTS github_teams(
    tenant TEXT NOT NULL,
    team_id BIGINT NOT NULL,
    installation BIGINT NOT NULL,
    slug TEXT NOT NULL,
    parent_team_id BIGINT,
    group_id TEXT NOT NULL,
    PRIMARY KEY(tenant,team_id));
CREATE INDEX IF NOT EXISTS github_teams_installation ON github_teams(tenant,installation);

-- Each team's current members as GitHub reports them (child-team members included).
CREATE TABLE IF NOT EXISTS github_team_members(
    tenant TEXT NOT NULL,
    team_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    login TEXT NOT NULL,
    PRIMARY KEY(tenant,team_id,user_id));
CREATE INDEX IF NOT EXISTS github_team_members_user ON github_team_members(tenant,user_id);

-- Webhook deliveries already applied (replay refusal), pruned after 30 days.
CREATE TABLE IF NOT EXISTS github_webhook_deliveries(
    tenant TEXT NOT NULL,
    delivery TEXT NOT NULL,
    event TEXT NOT NULL,
    received DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant,delivery));
CREATE INDEX IF NOT EXISTS github_webhook_deliveries_received ON github_webhook_deliveries(tenant,received);

-- Staleness sweeps look for current github groups by last successful sync.
CREATE INDEX IF NOT EXISTS groups_github_freshness ON groups(tenant,last_synced)
    WHERE source='github' AND sync_state='current' AND active=1;
