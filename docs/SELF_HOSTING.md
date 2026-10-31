# Self-hosting

The service uses PostgreSQL 17 with pgvector. The local development setup uses
`pgvector/pgvector:0.8.6-pg17` and the Moto S3 emulator. These are disposable test
services; a cloud provider is not selected automatically.

`scripts/dev_services.py start --state DIRECTORY` creates an isolated container
namespace, restrictive application/router roles and a private profile. It seeds
synthetic identities, not real source material. It prints profile and manifest
paths without credentials. `stop` pauses only containers carrying its exact
ownership label; a later `start` resumes them without resetting database state,
permissions or credentials. `destroy` explicitly removes those containers and their
ephemeral databases; use a new state directory afterward. Private state files
remain available for inspection or explicit deletion. Do not commit them.

The API uses application credentials; migration/provisioning credentials remain
in a separate operator file. `runtime.json` must be mode 0600 or 0400. Runtime and
worker configuration are explicit. Startup does not enable model calls, source
collection or curation. Finite workers consume PostgreSQL jobs; repeat them as needed.
`runtime.json` also sets per-principal and per-tenant write limits (`ingest_limits`)
and the organization's capture notice (`capture_notice`); operators quarantine or
bulk-withdraw a principal's content with `vaelius principal-*` commands. See
[shared-memory governance](MEMORY_GOVERNANCE.md).

## Container

Build from the public source tree:

```sh
docker build -t vaelius:local .
# Substitute your runtime UID/GID and an already prepared private profile:
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user UID:GID --tmpfs /tmp:rw,nosuid,size=256m \
  -p 127.0.0.1:8080:8080 -v /absolute/private/profile:/profile:rw vaelius:local
```

The image contains installed packages, no operator credentials, original sources,
development tests or model caches. Configure network-reachable PostgreSQL/S3
endpoints in a separate container profile. Host loopback addresses do not refer to
host services from inside a container. A profile using an in-container endpoint
override must name exactly the intended local service network. Do not silently
rewrite identity-broker issuer URLs.

The image declares a Docker `HEALTHCHECK` that calls the unauthenticated `/health`
liveness endpoint on port 8080 with the image's own Python, so it needs no shell or
curl. It checks only that the API answers; `/ready` checks the database. Kubernetes
ignores `HEALTHCHECK` (use `httpGet` probes on the same paths), Podman/Buildah drop it
unless built with `--format docker`, and a different `--port` needs a matching
`--health-cmd` or `--no-healthcheck`. The check uses plain HTTP with `Host:
127.0.0.1`, so a production profile with `tls: "direct"`, or an `allowed_hosts` list
without `127.0.0.1`, needs the same override (see HTTPS below). Released images are on
`ghcr.io` with signed provenance and SBOMs; pull them by digest and verify them as in
[RELEASING.md](../RELEASING.md#verifying-a-release).

For production, mount secrets or use workload identity for S3; configure HTTPS
(below), [provider OAuth](ENTERPRISE_AUTH.md), tenant routing and lifecycle policies. The backend MCP endpoint
uses those same identity and permission controls. [Provider configuration and
hosting seams](../packages/server/docs/MODEL_PROVIDERS.md) document API models,
local Codex-login execution and explicit source-object adapters.

## HTTPS and production transport

The container listens with plain HTTP on port 8080. That is only acceptable on
loopback or behind a TLS-terminating proxy or load balancer. Bearer tokens, refresh
tokens and source evidence must never cross a network in clear text. A profile
declares production in `runtime.json`; the API then refuses to start unless it also
declares how TLS is provided and an exact `allowed_hosts` list (no wildcards):

```json
{"allowed_hosts": ["memory.example.org"],
 "allowed_origins": [],
 "deployment": {"mode": "production", "tls": "proxy",
                "trusted_proxies": ["10.0.0.0/16"]}}
```

- `tls: "proxy"` lists the proxy addresses or networks (`0.0.0.0/0` and `*` are
  rejected). Only those peers may set `X-Forwarded-Proto`. The proxy must terminate
  TLS 1.2 or later, overwrite rather than append `X-Forwarded-Proto` and
  `X-Forwarded-For`, and keep the public `Host`. Do not publish the container port
  anywhere except to the proxy.
- `tls: "direct"` names absolute `certfile` and `keyfile` paths. The key file must
  be mode 0600 or 0400. The service then serves HTTPS itself.

In production, every request except `/health` and `/ready` must arrive over HTTPS,
or it is refused with `403 https_required`. Responses carry
`Strict-Transport-Security`. All responses carry `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer` and a deny-all `Content-Security-Policy`. Without `deployment`, the
profile is development: plain HTTP with a warning when bound beyond loopback.

`/health` is unauthenticated liveness only. Build identity is available at
`GET /enterprise/v3/service-status` to credentials holding the `audit` or
`settings` action.

`/ready` answers `{"ready": true|false, "provider_dispatch": "off"}` (`200` or `503`)
and never names a tenant. One readiness sweep (the control database, then each
active tenant up to `max_stores`) runs at most every 10 seconds; probes in between,
or while a sweep is running, get the last result. A probe therefore cannot make the
service open every tenant store per request, and readiness can lag a change by up to
10 seconds. Set probe periods to 10 seconds or more.

Unauthenticated routes are `/health`, `/ready`, OAuth discovery and the credential
exchanges under `/enterprise/v3/auth/` (`begin`, `complete`, `renew`, `revoke`,
`aws-sts`). None of them reveals whether a tenant exists or a token is valid (see
[enterprise authorization](ENTERPRISE_AUTH.md#actions-and-route-authorization)); rate-limit
them at the proxy. Every other route requires a credential and a matching action. The MCP endpoint applies the MCP SDK's DNS-rebinding protection
to the same `allowed_hosts`/`allowed_origins`, in addition to the API host check.

## Recovery and upgrades

Keep database/profile/object backups together with their revision provenance.
Use additive migrations and the matching client/server release. Run tenant and
control-plane migrations, then reapply application-role grants, before starting a
new release. For example, `043_action_grants.sql` keeps existing credentials'
feedback and curation abilities, and `044_control_login_requests.sql` adds a control
table the control role must be able to insert into and delete from. Stop old workers
before changing provider identities or processing configuration; reconstruct
authorized observer context when checkpoints cannot be resumed.

`python -m agenthub.cloud_recovery --help` exposes restore/reconciliation operations.
Recovery must retain denial/deletion journals and recheck permissions; a restored
snapshot must not resurrect withdrawn content. Backups age out within your
retention commitment; export the content-free deletion journal separately and run
`cloud_recovery replay-deletions` on a restored copy before reconciling it. Schedule
`cloud_maintenance retention-run --apply` for operator retention policies. See
[data lifecycle](DATA_LIFECYCLE.md) for retention, legal holds and subject erasure. Never rehearse destructive recovery
against a user's live database. [Source-first rollback](../packages/server/docs/SOURCE_FIRST.md)
documents the raw-passage hold required before returning to an older binary.

## Backfilling past conversations

`vaelius backfill` imports conversation history that already lives where the service
runs, such as a transcript archive or another memory service's export. It is an
operator command. People push their own earlier conversations from their computers
one at a time instead ([client guide](CLIENT.md#pushing-a-past-conversation)); there
is no bulk client import.

```sh
vaelius backfill --profile /absolute/private/profile --tenant acme \
  --input /absolute/export --map /absolute/private/backfill-map.json            # dry run: counts only
vaelius backfill --profile ... --tenant acme --input ... --map ... --apply --max-seconds 3600
vaelius backfill --profile ... --tenant acme --refresh-connections              # at least every 30 days
```

**Input.** One JSON file (`.jsonl`) or a folder of them, read in name order. Each
line is one event:

```json
{"session": "chat-0001", "timestamp": "2026-03-01T17:04:05Z", "author": "alice@example.org",
 "workspace": "maple-repo", "role": "user", "text": "Where is the import file kept?"}
{"session": "chat-0001", "timestamp": "2026-03-01T17:04:09Z", "author": "alice@example.org",
 "workspace": "maple-repo", "role": "tool", "tool": "Read", "paths": ["src/import.py"], "text": "def load(): ..."}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `session` | yes | The conversation; events of one session become one capture session |
| `timestamp` | yes | ISO 8601 text or epoch seconds, stored as the time the event occurred |
| `author` | yes | The author as the source names them; mapped to a principal |
| `workspace` | yes | The workspace as the source names it; mapped to a workspace |
| `role` | yes | `user` (a prompt), `assistant` (a reply) or `tool` (a tool call and its result) |
| `text` | yes | The text, or for `tool` the result |
| `tool`, `paths` | no | For `tool`: the tool name and up to 100 paths it named |
| `id` | no | A stable event id within the session; default: the event's position in its session |

A line with any other field (including any sharing field), another role or a value
of the wrong type is skipped and counted as `invalid`. Lines are at most 4 MB.

**Mapping file.** It names, for each input author and workspace, the principal and
workspace (name or id) they become:

```json
{"principals": {"alice@example.org": "alice", "bob@example.org": "bob"},
 "workspaces": {"maple-repo": "maple"}}
```

Nothing is guessed. An author or workspace missing from the file, a principal that
does not exist or is inactive, an unknown workspace, and an author who is not a
writer in the mapped workspace are skipped and counted (`unmapped_author`,
`unmapped_workspace`, `unknown_principal`, `unknown_workspace`, `not_writer`).

**Always owner-private.** The service cannot see the author's computer, so it cannot
apply scoped folders or origin labels; every imported item is therefore private to
its author, whatever the workspace default or the author's own setting. Each author
gets one connection per workspace (`backfill-...`, enrolled `private`), and no event
carries a sharing request. The command has no sharing options, and there is no
per-item sharing afterwards: imported conversations stay the author's personal
memory.

**Dry run, idempotent, resumable, rate limited.** Without `--apply` it prints counts
per principal and workspace and the skip counts, never content. With `--apply` each
event goes through the same intake as live capture (server redaction, the tenant's
write limits as the author, legal holds, retention and erasure), with a source id
made from the principal, workspace, session and position, so running it again
reports `duplicate` instead of writing twice. `--max-events` (default 5000) and
`--max-seconds` bound one run; a run that stops says so, and the next run continues.
Every run writes one operator audit row (`server_backfill`, `dry_run` or `applied`)
with the operating-system user that ran it and the counts; each item is also
audited as an ordinary ingest by its author, with `operator-backfill:<run>` as the
request id. Retention ages backfilled items from the time they were imported.

**Keeping them readable.** Like every connection, a backfill connection hides its
items, from their owner too, once its permission has not been observed for 30 days.
`--refresh-connections` re-observes the backfill connections of authors who are
still active writers in the workspace (an `--apply` run also refreshes the ones it
writes to); the others are left to expire. Schedule it with the other maintenance
commands.

## Upgrading to this release (workspaces, migration 048)

This release replaces the per-item sharing ladder with **workspace access
lists**. Every id stays the same, and no stored content is lost, but the
service applies very different rules after the migration runs. Plan a short
window for admins to set up access lists again.

- **Every existing item becomes private to its owner** at the migration and stays
  so (per-item sharing was removed by migration 055); new captures by writers
  inherit the workspace audience. By construction nothing is readable by anyone who
  could not read it immediately before, so no reader-set guard runs: the
  migration is irreversibly narrower.
- **Project members become writers.** Each project's explicit members and the
  SCIM groups mapped to it become a local group `<project>-members`, with each
  SCIM group nested inside it. The workspace gets one access-list entry:
  `group:<project>-members → writer`. No admin entry and no organization entry
  is created.
- **Nobody is a workspace admin after the upgrade.** Workspaces without a
  counting admin are managed by tenant settings administrators (the
  `settings_admin` flag) through the audited break-glass override until an
  admin is assigned. Do that from the operator CLI, for each workspace that
  needs one:

  ```sh
  vaelius workspace --profile /absolute/private/profile --tenant TENANT \
      --workspace maple --action set-entry \
      --subject-kind principal --subject alice --role admin
  ```

  The report lists workspaces without an admin:

  ```sh
  vaelius workspace --profile /absolute/private/profile --tenant TENANT
  ```

- **The `sharing-policy` command is replaced.** Project and tenant sharing
  ceilings and the person default now live on three settings
  (`workspace_creation`, `widest_entry`, `owner_sharing`) and on each
  workspace. Set tenant policy with `vaelius tenant-policy`:

  ```sh
  vaelius tenant-policy --profile ... --tenant TENANT \
      --workspace-creation anyone --widest-entry people_and_groups --owner-sharing allowed
  ```

  Workspace settings (name, `item_default`, `active`), repository links and
  access-list entries are managed with `vaelius workspace --action
  settings|link|unlink|set-entry|remove-entry` or by the workspace's admins
  from `vaelius-client workspace`.
- **Credentials and refresh sessions holding `share` also hold `manage`.**
  Migration 048 marks them with `legacy_actions` bit 4 so the owner's existing
  rotation keeps working. New enrollments issue `manage` explicitly.
- **Dropped tables.** `cloud_explicit_memberships`,
  `cloud_directory_memberships` and `cloud_directory_group_projects` are
  dropped. `enterprise_memberships` is replaced by a read-only view of the
  same name over `workspace_access` for one release, so existing operator
  scripts keep working while they are updated.
- **Clients.** `vaelius-client` releases shipped with this server send
  `workspace` and a normalized Git `remote` on every capture. An earlier
  client that sends `project` is accepted (legacy alias). An earlier server
  rejects `workspace` and `remote` with `invalid_general_fields`, so deploy
  the server first.

See [the privacy model](PRIVACY_MODEL.md#upgrading-to-migration-048) and
[access reviews](ACCESS_REVIEW.md) for the rest of the behavioural change.
