# Changelog

Notable changes to `vaelius-client` and `vaelius-server`, which are released
together under one version. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The release workflow publishes the section whose heading matches the tag.

## [Unreleased]

### Security

- **Unscoped reads are denied, not narrowed.** In a labelled Claude Code session
  the guardrail hook now denies reads of existing paths outside every scoped
  folder (a repository or folder without `.vaelius`, a dotfile, another volume)
  with one line: add a `.vaelius`, or run `vaelius-client session use private`.
  A private session may still read them. Before, such a read was allowed and
  silently made the rest of the session private. Tooling, caches, system paths,
  temp and Claude Code configuration are unchanged; read-only, off and unlabelled
  sessions are unaffected; the taint stays as the backstop for reads the hook
  cannot see.
- **Read-only chats, enforced by the service** (migration 053,
  `read_only_sessions`). `vaelius-client session read-only`, or the new MCP tool
  `read_only_session` when the person asks, makes the rest of a chat write nothing,
  one-way until `/clear` or a new chat. The service refuses every write route and
  tool with `403 read_only_session` for a connection carrying
  `X-Vaelius-Read-Only: 1` or a `read_only` session label, and for any request that
  names a registered chat or MCP connection id. `capture = "read-only"` folders
  get the same refusal. `push-session` refuses chats that ran read-only.

- Guardrails: both profiles and both managed-settings templates ask before the agent
  changes its own configuration (`~/.claude/CLAUDE.md`, settings, skills, agents,
  commands, hooks, output styles, reference, `~/.claude.json`, Codex `config.toml`,
  `hooks.json`, `AGENTS.md`), with `permissions.ask` rules and, for Bash writes
  (redirections, `tee`, `sed -i`, `cp`/`mv` onto them), an `ask` from the
  `guardrail-check` hook. `~/.claude/plans/` stays writable. Agent sign-in files
  (`~/.claude/.credentials.json`, `~/.claude.json`, `~/.codex/auth.json`) are
  never read: deny rules plus the hook, Bash included. Re-run
  `install --claude-code` to add the rules. Reading `~/.claude/skills`, `agents`,
  `commands`, `hooks` and `output-styles` no longer makes a session private.
- Runtime base image updated to Python 3.12.16 (`python:3.12.16-slim-trixie`,
  CVE-2026-115557), and `joserfc` to 1.7.6 (GHSA-9pg5-8c7x-5hfx).
- Claude Code's built-in auto memory (`~/.claude/projects/<project>/memory/`) is
  switched off in captured repositories: `install --claude-code` writes
  `autoMemoryEnabled: false` into each enrolled or scoped project's
  `.claude/settings.local.json` (receipt-tracked; uninstall restores the earlier
  value), both managed-settings templates set it, and a session-start status line
  says when it is still on. Reading an auto-memory folder now makes the session
  private for capture; tool results, task output and session files under
  `~/.claude/projects/<project>/` keep the #40 rule.
- Owner-private preferences follow the private-item rule: they reach only private
  sessions (and unlabelled requests), and a delivered or MCP-fetched preference is
  carried as private for taint, so an echo in a shared session is not captured for
  its audience.
- The MCP header helper sends the label of the session last active in its folder
  (recorded by the hooks in the profile, 0600), only ever narrower than the folder's
  own label. A private or narrowed session's `search_memory` no longer under-returns.

- Capture discovery ignores a scope file at the home folder, above it, or directly
  in a top-level personal folder, the same rule the origin labels already applied
  (one shared predicate). A session started outside any repository under a stray
  `~/.vaelius` or `~/Documents/.vaelius` is no longer captured; the person sees a
  one-time status line naming the ignored file.
- Installed hook and MCP header-helper commands run Python with `-P`, so a project
  folder's own `agentclient` package can no longer run in place of the client.
  Installations made by 0.3.0 must re-run `install` (Codex) or
  `install --claude-code` (settings installation); older hooks say so at session
  start. See [the client guide](docs/CLIENT.md#hook-commands-and-the-project-folder).
- Hash-locked dependencies: `requirements-runtime.txt`, `requirements-build.txt`
  and `requirements-dev.txt` (pip-compile with `--generate-hashes`) replace
  `requirements-runtime.lock`. The image and CI install them with `--require-hashes`
  and build wheels without fetching an unverified build backend.
- `starlette` 1.9.0 (cloud extra and hashed locks) fixes six advisories; the
  server dispatches on the ASGI path rather than the Host-derived `request.url`.
- `certifi` 2026.10.19 removes the emSign root whose intermediate CA keys were
  compromised (GHSA-6q48-6c7w-9gw2).
- `setuptools` 84.1.0 (build backend pin and hashed locks) fixes a Windows path
  escape through `namespace_packages.txt` during legacy wheel installation
  (GHSA-xmj3-9gf7-h52w, Low).
- `filelock` 4.2.1 (hashed locks; pulled in by `huggingface-hub`) fixes a
  `ReadWriteLock` symlink or journal-file plant that could make the locking
  process overwrite or unlink its own files (GHSA-v4x8-3wr5-3wq8, High).
- Runtime base image updated to Python 3.12.15 (`python:3.12.15-slim-trixie`),
  fixing Python and OpenSSL vulnerabilities that the new image scan reported.
- CI audits the locked dependencies with osv-scanner and scans the runtime image
  with Grype, failing on fixable High/Critical findings. Reviewed, expiring
  exceptions live in `osv-scanner.toml`.
- Releases publish CycloneDX SBOMs for both wheels and the image, and Sigstore-signed
  GitHub build-provenance and SBOM attestations. See [RELEASING.md](RELEASING.md).
- Dependabot (weekly, grouped, 7-day cooldown), an OpenSSF Scorecard workflow, and
  least-privilege, credential-free checkouts in every workflow.

### Added

- `vaelius-client push-session`: push one of your own past Claude Code transcripts
  or Codex rollouts, one session per command, from a scoped folder where you are a
  writer. Replayed through the live origin labels, session taint and redaction;
  always owner-private; dry run (counts only) unless `--apply`; idempotent source
  ids from the session id and event position; `--list-recent` to pick one.
- `vaelius backfill`: operator import of conversation history already held
  server-side (a generic JSONL event stream plus a principal and workspace mapping
  file). Always owner-private, no sharing options, unmapped authors and workspaces
  skipped, dry run by default, idempotent, rate limited and audited;
  `--refresh-connections` keeps backfilled items readable. See
  [self-hosting](docs/SELF_HOSTING.md#backfilling-past-conversations).
- Remembered items (migration 051): `vaelius-client remember add|list|remove`, the
  MCP tools `remember`, `list_remembered` and `forget_remembered` (two-step confirm),
  and `POST /enterprise/v3/remember[/list|/forget]`. The person's own words, up to
  8 KB, owner-private until shared, versioned, withdrawable and audited; needs only
  `ingest`. "Always load" items arrive at session start and with recall where their
  audience allows. `submit_memory_candidate` without `source_ids` writes one; the
  old call shape is unchanged. `vaelius-client import-memory` turns a Claude Code
  auto-memory folder into remembered items (dry run and counts only by default).
- GitHub team sync (access model PR C, migration 050). A GitHub App installed on
  the organization turns teams into read-only `github` groups (`gh-<team id>`,
  named `gh_<org>_<slug>`), nested by parent team. Signed webhooks
  (`POST /enterprise/v3/github/webhook`: constant-time HMAC check, `401` on a bad
  signature, `409` on a replayed delivery id) trigger a re-read of current state;
  a reconcile every `reconcile_seconds` (default 6 h) fixes drift; a group with no
  successful sync for `max_staleness_seconds` (default 24 h) contributes no
  members. Installation tokens are minted with organization *Members: read* only.
  Operator commands `vaelius github status` and `vaelius github sync [--now]`.
  Setup: [enterprise identity](docs/ENTERPRISE_AUTH.md#github-app-team-sync).
- GitHub identity link: `vaelius-client github link` (or `--link-github` at
  enrollment) runs the GitHub device flow through the service, which stores only
  the numeric user id and login and discards the token. Team membership counts
  only for linked principals; renames keep the link; link, relink and unlink are
  audited; offboarding and access-review revocation deactivate it. The access
  report adds `identity_link` lines and a `github_groups` summary with
  `pending_identity_link`.

- Fixed session labels (PR B of [the access model](docs/ACCESS_MODEL.md#7-fixed-session-labels),
  migration 049). A Claude Code session started in a workspace folder with capture on
  takes that workspace's audience as its label. The PreToolUse guardrail hook denies
  reads of folders whose workspace is narrower, unreadable or unknown with one plain
  line (never a prompt) and no network call; the service filters recall (search,
  injection, evidence, context, timeline and history, and the MCP connection through
  `X-Vaelius-Session-Label`) to items whose audience contains the session audience,
  in the Python oracle, the indexed SQL filter and row-level security alike.
  `POST /enterprise/v3/workspaces/descriptor` returns the session's superset list,
  cached in the client profile with a 15-minute refresh and a 24-hour expiry.
  `vaelius-client session use <workspace|folder|remote|private>` (or `use`), the MCP
  tool `use_workspace` and `vaelius-client session status` narrow and show the label;
  narrowing lasts until `/clear`, shows on the status line and later captures carry
  the narrowed workspaces. The #32 floating taint remains only as the backstop for
  reads the hook cannot see; an allowed read only adds its workspace to what
  captures carry.

- Origin labels and session taint for agent capture. The client labels each
  captured tool event with what it touched (paths outside the repository, personal
  folders and credentials, other repositories, MCP servers, non-public web
  addresses, recalled memory) and keeps the most restrictive label for the rest of
  the session; the service stores it per source and applies it as the origin
  ceiling of the sharing minimum, so a session that read personal files is captured
  as private from that point on. Scope files can allow specific paths and MCP
  servers (`[capture] allowed_paths`, `allowed_mcp_servers`). Events carry an
  optional `origin_label`; deploy the service before clients.
- Scoped folders form the origin allowlist. Any folder holding a scope file, with
  its whole subtree and whether or not it is a Git repository, is in scope; the
  nearest scope file wins. A scope file at the home folder, above it, or directly
  at a top-level personal folder is ignored. Scoped folders are checked before the
  personal-folder rule, so repositories under `~/Documents` behave normally;
  credentials stay private everywhere, now including inside the project. Claude
  Code's own state (`~/.claude/projects/<folder>/`) follows the folder it belongs
  to, and its user configuration (`CLAUDE.md`, `reference/`, `plans/`,
  `settings*.json`, `plugins/`) does not restrict. Same-project folders and
  same-project recalled memory no longer change the label. A session-start line
  names the in-scope folders (`scope_context_notice`, on by default).
  `Context.scope_of(path)` returns a path's scope (root, project, sharing,
  category) for reuse.
- Docker `HEALTHCHECK` against the unauthenticated `/health` endpoint.
- One sharing ladder, `private` < `team` < `organization`, for conversation passages
  and, separately, for originals. Effective sharing is the minimum of the author's own
  setting (`vaelius-client sharing`, default private), the scope-file request, the
  project and tenant ceilings and the connection visibility, re-applied on every
  request. Retrieval is project-scoped, with other projects' organization-shared
  content as a labelled later tier and an explicit `scope = "all"`. Migration 045;
  see [the privacy model](docs/PRIVACY_MODEL.md).
- Owner self-service: every person can list (`vaelius-client mine`), delete
  (`forget`, including "forget this session"), re-share (`sharing --set` with a
  selection) and share with named people or projects (`share`, `unshare`) the
  content they created, without an operator; also as MCP tools that need an
  explicit `confirm` and HTTP routes under `/enterprise/v3/my-data`. Deletion uses
  the canonical purge and reports legal holds; raises are capped by every ceiling
  and never exceed the origin ceiling; every change and grant is audited. New
  `share` action (default enrollment) and `moderate` action (project members hide
  others' content). Curated and derived documents registered after migration 047
  follow the sharing levels like passages. See
  [owner controls](docs/PRIVACY_MODEL.md#owner-controls).
- Workspaces, groups and access lists (migration 048, PR A of the
  [access model](docs/ACCESS_MODEL.md)). Projects become **workspaces** with the
  same ids, each with an **access list** of `principal` / `group` /
  `organization` entries and a role (`reader` < `writer` < `admin`). Items are
  either **private** or shared with the workspace audience, plus optional
  explicit shares to a principal or a group — the per-item `private` / `team` /
  `organization` ladder is replaced by this. Groups come in three sources
  (`github` added in PR C, `scim`, `local`) with transitive membership
  materialized in `group_closure`; a stale or failed synced group contributes
  no members. Repositories link to one workspace per tenant by normalized Git
  remote; captures carry that remote, and the server refuses a capture whose
  remote is not linked with HTTP 409 `repo_not_linked`. An admin's first
  capture from an unlinked remote links it automatically. New HTTP routes
  under `/enterprise/v3/workspaces/*`, `/groups/*` and `/tenant-policy`, with a
  settings-admin break-glass override audited as `workspace_admin_override`.
- `manage` credential action, gating workspace and group administration (part
  of default interactive enrollment, provider OAuth scope `vaelius:manage`,
  AWS STS defaults). Delegations never carry `manage`.
- One read predicate, *reader ∈ item audience*, in three layers (Python,
  indexed SQL `AUDIENCE` filter, row-level security `search_reader_in_audience`
  and `search_document_allowed`), with parity tests. A teammate's search runs
  the same number of SQL statements as the owner's.

### Changed

- **Workspace visibility is chosen on the server at creation.** `vaelius-client
  workspace create NAME --visibility private|org` (and `POST
  /enterprise/v3/workspaces/create`) now require it; `org` adds an organization
  reader entry, capped by `widest_entry`. Admins change it with `workspace
  set-visibility NAME private|org` (`POST /enterprise/v3/workspaces/visibility`),
  which reports `newly_unreadable`. A `visibility` key in `.vaelius` is ignored
  with a one-time warning. Scripts that create workspaces must add the flag.
- **`read` in `.vaelius`**: the default recall scope (`read = ["ws-a", "ws-b"]` or
  `["*"]`). Without it, recall covers the current workspace only (before: every
  workspace the label allowed; add `read = ["*"]` to keep that). The service
  intersects it with the session label and uses it only to narrow retrieval. The
  MCP `search_memory` tool takes `scope: "all"` or workspace names to broaden one
  search; the "memories hidden" note only names workspaces in the list.

- **Role-bound credential actions.** A credential's effective actions in a
  workspace are its credential actions ∩ the actions the role implies
  (`reader`: `read`, `feedback`, `source_read`; `writer`: + `ingest`,
  `correct`, `curate`, `withdraw`, `share`; `admin`: + `moderate`, `manage`).
  Ownership survives role changes: owners always find, read, withdraw, delete
  and make private their own items; only widening needs the `writer` role and
  `share`.
- **Moderation now follows the workspace admin role**, and `moderate` is no
  longer a default action. A caller needs the `admin` role and the `manage`
  action; the route also accepts the older `moderate` action so credentials
  issued before migration 048 keep working.
- **`vaelius-client sharing --set` takes `shared` or `private`.** The older
  `team` and `organization` names stay as deprecated aliases of `shared`.
  Scope-file keys change too: `workspace` replaces `project` (both are read),
  and `originals` is ignored with a one-time warning. See
  [the client guide](docs/CLIENT.md#repository-scope-files).
- **Owner shares to a project are gone.** `/enterprise/v3/my-data/grant` and
  the client's `--with-project` option are refused as
  `project_grantee_retired_use_group`; use `--with-principal` or
  `--with-group` instead. Migration 048 rewrites existing project grants into
  grants to the project's `<project>-members` local group.
- **Access reviews.** New line kinds `workspace_access`, `workspace_entry`,
  `group_membership`, `synced_group_membership`, `repo_link`; new flags
  `admin_role`, `org_wide_entry`, `group_stale`, `suspended_by_tenant_policy`;
  new revocation outcomes `change_at_source`, `revoke_entry_or_membership`,
  `requires_workspace_admin`. Offboarding also removes direct workspace
  entries and local-group memberships; `remaining.workspaces_without_admin`
  lists workspaces where the person was the last counting admin. See
  [access reviews](docs/ACCESS_REVIEW.md).

### Removed

- The client-side bulk Codex backfill (`python -m agentclient.enterprise_backfill`)
  and its server wrappers `agenthub.historical_backfill` and
  `agenthub.historical_campaign`. They imported every allowlisted rollout without
  origin labels or session labels. Use `push-session` for one conversation, or
  `vaelius backfill` for history already held server-side.
- The MCP tool `search_memory` no longer takes `scope`: the session label decides
  which workspaces recall reaches. Unlabelled HTTP search requests still accept
  `"scope": "all"`.

- **`project_grant` lines in the access review**, replaced by
  `workspace_entry`, `group_membership` and `synced_group_membership`.
- **The `vaelius sharing-policy` operator command**, replaced by `vaelius
  workspace` (per-workspace: `show`, `set-entry`, `remove-entry`, `settings`,
  `link`, `unlink`, `create`) and `vaelius tenant-policy`
  (`--workspace-creation`, `--widest-entry`, `--owner-sharing`). See
  [self-hosting](docs/SELF_HOSTING.md#upgrading-to-this-release-workspaces-migration-048).
- Dropped tables `cloud_explicit_memberships`,
  `cloud_directory_memberships` and `cloud_directory_group_projects`;
  `enterprise_memberships` becomes a read-only view over
  `workspace_access` for one release.

### Upgrade note

Running migration 048 **keeps every stored source** but marks every existing
item `private = 1` and drops every existing raw grant and share. Nobody but
each item's owner sees that content again until the owner re-shares it, an
admin adds a workspace entry that reaches the readers, or a tenant settings
administrator does so on the admin's behalf. The migration creates no admin
entries by itself: each workspace is managed by tenant settings administrators
until one is set. Credentials and refresh sessions issued before the migration
that hold `share` also hold `manage` (`legacy_actions` bit 4), so operator
rotation is not required. Clients that send the new `workspace` or `remote`
fields against an earlier server are rejected with `invalid_general_fields`;
deploy the server first. The full migration contract is in
[the privacy model](docs/PRIVACY_MODEL.md#upgrading-to-migration-048) and
[self-hosting](docs/SELF_HOSTING.md#upgrading-to-this-release-workspaces-migration-048).

## [0.3.0] - 2026-10-09

Initial public alpha. See [release acceptance](docs/RELEASE_ACCEPTANCE.md).
