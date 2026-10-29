# Access model: workspaces, groups and session labels

**Status: design, approved by the owner. Not yet implemented.** Three pull requests
implement it in order (PR A, PR B, PR C; see [the plan](#12-implementation-plan)).
Until PR A lands, the [privacy model](PRIVACY_MODEL.md) describes what the service
enforces. Each implementation PR updates that document, the
[data boundaries](DATA_BOUNDARIES.md), [client](CLIENT.md),
[enterprise identity](ENTERPRISE_AUTH.md), [access review](ACCESS_REVIEW.md) and
[data lifecycle](DATA_LIFECYCLE.md) pages for the part it ships.

This document is the specification those PRs follow. Where it says **must**, a PR
that does something else is wrong; where the owner left a point open, the choice
made here is marked **(decision)** and listed again in
[recorded decisions](#appendix-recorded-decisions).

## Summary

- **Workspaces** replace flat projects. A workspace has an **access list**: entries
  of a subject (a person, a group or the whole organization) and a role (`reader`,
  `writer`, `admin`). Fully private, chosen people, team, org-read/team-write and
  org read-write are all just access lists.
- **Groups** are one object type with a `source`: `github` (teams synced from a
  GitHub App installation), `scim` (identity-provider groups, today's directory
  groups) or `local` (managed in Vaelius). Synced groups are read-only in Vaelius.
- **Repositories link to one workspace** by normalized remote. Captures from a
  repository that is not linked to the named workspace are rejected.
- **Items inherit the workspace audience.** The only per-item options are `private`
  (owner only) and explicit shares to a person or group. The per-item
  `private`/`team`/`organization` ladder and its minimum of six terms go away.
- **Sessions have a fixed label**: the audience of the workspace they start in. A
  read is allowed only when the source's audience contains the session's audience
  (no read up). The PreToolUse hook denies file reads that fail; the server filters
  recall. A person can explicitly narrow a session to "this workspace and B", and
  capture follows. The #32 floating taint remains only as a backstop for reads the
  hook cannot see.
- **One predicate**, *reader ∈ item audience*, is implemented in Python, in the
  indexed SQL search filter and in row-level security, with parity tests.
- **Migration keeps every id and widens nothing**: no item becomes readable by
  anyone who cannot read it today. The migration proves this and refuses to commit
  otherwise.

## 1. Terms

| Term | Meaning |
| --- | --- |
| Principal | A person (or a service acting for one) in a tenant, as today |
| Workspace | A body of shared memory with an access list. Replaces the project; keeps the project's id |
| Entry | One line of an access list: `(subject, role)` |
| Subject | `principal:<id>`, `group:<id>` or `organization` |
| Role | `reader` < `writer` < `admin` |
| Audience of a workspace | Every principal that holds at least `reader` in it through any entry |
| Item | A captured source and everything derived from it: redacted passages, curated and derived documents. Originals are a separate tier of the same source |
| Item audience | Who may read an item (section 6) |
| Session audience | Who may read what a session captures (section 7) |
| Carried workspaces | Other workspaces whose content an item was built from; a reader must be in each of their audiences too. Today's `origin_projects` |

## 2. Workspaces and access lists

### Access lists

A workspace has one access list. Each entry names a subject and a role:

| Role | Can |
| --- | --- |
| `reader` | Read items whose audience is the workspace; give feedback |
| `writer` | Also capture and contribute (ingest, correct, curate), withdraw and share their **own** items |
| `admin` | Also manage the access list, linked repositories and workspace settings, and moderate |

A principal's role in a workspace is the **highest** role any entry gives them:
a direct entry, an entry for any group they belong to (transitively), or the
organization entry. An organization entry reaches every active principal of the
tenant who is not under an operator quarantine, as the `organization` level does
today. Roles never subtract: there are no deny entries.

The common modes are combinations, not separate settings:

| Mode | Access list |
| --- | --- |
| Fully private | `principal:me → admin` |
| Chosen people | `principal:me → admin`, `principal:bob → writer`, `principal:carol → reader` |
| Team | `principal:me → admin`, `group:platform → writer` |
| Org-read, team-write | `group:platform → writer`, `organization → reader` |
| Org read-write | `organization → writer` |
| Read-only group | `group:auditors → reader` (alongside any of the above) |

### Self-service and tenant policy

- Any principal whose credential holds `manage` (section 8) can create a workspace
  and becomes its first `admin` through a `principal` entry.
- Two tenant settings on `enterprise_organizations` restrict this:
  - `workspace_creation`: `anyone` (default) or `admins` (only principals with the
    tenant `settings_admin` flag);
  - `widest_entry`: `organization` (default), `people_and_groups` (organization
    entries are refused) or `none` (only `principal` entries with the `admin` role
    count: every workspace is private to its admins). Lowering it does **not**
    delete existing entries;
    it suspends every entry wider than the setting at query time, so lowering
    hides at once and raising restores. **(decision)** One ordered setting maps
    exactly onto today's tenant ceiling during migration.
- The existing `owner_sharing` setting (`allowed` or `lower_only`) keeps governing
  per-item shares (section 6).
- A workspace that has an `admin` entry whose subject is a principal or a `local`
  group with an active member keeps at least one: a change that would remove the
  last one is refused. **(decision)** A synced group can be an admin too, but it
  does not count towards this rule, because a sync failure (section 3) must never
  orphan a workspace. A workspace without any such admin (for example one created
  by the migration, section 11) is managed by tenant administrators, as projects
  are today.
- A tenant settings administrator (the `settings_admin` flag and the `settings`
  action) can manage any workspace's access list as a break-glass operation,
  audited as `workspace_admin_override`. That never lets them read content they
  could not read before; adding themselves as a reader is an ordinary, audited
  entry change. **(decision)**

### Workspace settings

| Setting | Values | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `[A-Za-z0-9_-]{1,120}`, unique per tenant | the id | What `.vaelius` names. Renaming keeps the id |
| `item_default` | `shared`, `private` | `shared` | New items are created private to their author unless they share them. Used by migrated projects whose ceiling was `private` (section 11) **(decision)** |
| `active` | 0, 1 | 1 | Archiving hides every item from everyone but its owner |

### The server is authoritative

Access lists, group memberships, repository links and roles live only in the
service and are read inside every check; nothing about access is cached between
requests on the server. The client caches an **advisory** descriptor
(section 5) so the guardrail hook can decide locally; the service re-decides
everything it stores or returns.

### Changes take effect on the next request

Adding an entry widens the audience of **every** item in the workspace, past and
future, as on GitHub, Google Drive and Notion. Removing an entry or a group member
narrows it on the next request, including results already delivered (delivery is
rechecked, as today). Carried workspaces (section 6) stop a later widening of one
workspace from exposing content that came from another.

Every access-list change is audited (`workspace_entry_add`,
`workspace_entry_remove`, `workspace_entry_role`) with the actor, subject, role
and the before/after audience size.

## 3. Groups

### One object type

| Source | Members come from | Edited in Vaelius | Nesting |
| --- | --- | --- | --- |
| `github` | A team in a GitHub organization where the Vaelius GitHub App is installed | No | Child teams, as GitHub reports them |
| `scim` | A group pushed by the identity provider (Okta, Entra ID). Today's directory groups | No | No (the providers do not push nested groups) |
| `local` | The group's admins in Vaelius | Yes, by the group's admins | Yes: a local group may contain principals and other groups of any source |

A synced group cannot be renamed, edited or deleted in Vaelius; it can only be
used as a subject. Removing a person at the source removes them on the next sync.
A `local` group is managed either by its own admins (principals with `admin` on the
group, set at creation to the creator; the same last-admin rule applies) or, when
it belongs to a workspace (`groups.workspace`), by that workspace's admins.

Group membership is **materialized**: `group_closure(tenant, group_id, principal)`
holds the transitive principal members of every group and is rewritten in the same
transaction as any edge change or sync. Cycles between local groups are refused at
edit time. Checks never walk the graph at query time.

### GitHub teams (PR C)

- **Identity link.** A person links their GitHub account once, at enrollment or
  later, with `vaelius-client link github`. The **server** starts a GitHub
  device-flow authorization for the App's client id (device flow enabled in the
  App's settings), the client shows the user code
  and verification address, the person approves in the browser, and the server polls
  for the user token, calls `GET /user`, stores the numeric user id and current
  login in `identity_links`, and discards the token. Completing the device flow
  proves the person controls that login; typing a login never links one. One GitHub
  account links to at most one principal per tenant. Unlinking is self-service or by
  a tenant administrator, and audited.
- **Sync.** The App needs organization *Members: read* and repository
  *Metadata: read*. It subscribes to `membership`, `team`, `organization` and
  `installation` webhooks. A webhook is only a **hint**: on any delivery for a team
  the server fetches that team's current member list
  (`GET /orgs/{org}/teams/{team_slug}/members`, which includes child-team members)
  and replaces the group's membership with it. State-based sync makes out-of-order
  or duplicate deliveries harmless. Deliveries are verified with the webhook secret
  (`X-Hub-Signature-256`) and de-duplicated by delivery id.
- **Reconcile.** Every 6 hours, and on demand (`vaelius groups sync`), the server
  lists every team of every installation and replaces all memberships. Teams that
  disappeared become inactive groups.
- **Unlinked members.** A GitHub member without a linked principal is kept as a
  pending member and grants nothing until they link.

### SCIM groups

Today's pipeline (`apply_directory_event`, `cloud_directory_resources`) stays the
ingestion path. What changes is the target: a directory group becomes a `scim`
group whose members are the bound principals of its SCIM members, instead of
being mapped straight to project memberships. An out-of-order event still puts
the group into `requires_reconcile`, which still makes it contribute no members
until reconciled.

### Stale sync fails safe

A synced group has `sync_state`: `current`, `stale` or `failed`.

| Condition | State | Effect |
| --- | --- | --- |
| Last successful GitHub reconcile older than `max_staleness` (tenant setting, default 24 hours) | `stale` | The group contributes **no members** to `group_closure` |
| Installation suspended or uninstalled, App credentials rejected, organization unreachable for longer than `max_staleness` | `failed` | Same |
| SCIM group with an out-of-order event awaiting reconcile | `stale` | Same (today's behavior) |
| Next successful sync | `current` | Membership is rebuilt from the source |

So a sync outage can only take access away, never leave a removed person in.
Because a synced group never counts as a workspace's last admin, admins keep access
and can see the state: the workspace and group views, the access report and the
service status show `group_stale` with the last successful sync time. Every state
change is audited (`group_sync`, with `applied`, `stale` or `failed`).

## 4. Repository linking

- A workspace lists linked repositories by **normalized remote**: `host/path`, all
  lower case, with the scheme, user, port and a trailing `.git` or `/` removed.
  `https://example.com/Example-Org/Maple.git`, `ssh://git@example.com:22/example-org/maple`
  and the scp form `git@example.com:example-org/maple.git` all become
  `example.com/example-org/maple`. Paths may have more than two segments (subgroups).
- **One repository links to one workspace**: the remote is the primary key of
  `workspace_repos`. Linking a remote that is already linked elsewhere is refused.
  Linking and unlinking need `admin` on the workspace and are audited
  (`repo_link`, `repo_unlink`).
- **The client sends the remote with every capture** (`remote`, normalized as
  above). It uses the remote named `origin`, else the only remote, else none.
  **(decision)**
- **The server rejects a capture whose remote is not linked to the named workspace**
  (`409 repo_not_linked`). The client keeps such events queued and unshared, as it
  does for other rejected fields, and the status line says which admin action fixes
  it. A capture with no remote (a scoped folder that is not a Git repository) is
  accepted on the person's writer role alone.
- **Later (PR C):** for a remote in a GitHub organization where the App is installed, the
  server checks that the capturing person's linked GitHub account can read the
  repository (`GET /repos/{owner}/{repo}/collaborators/{username}/permission`,
  cached for 15 minutes). A person without a linked account, or without read access,
  is rejected with `403 repo_access_unverified`.

## 5. The scope file

### Format

```toml
# .vaelius
workspace = "maple"      # a workspace name the person can see
capture = "on"           # on | read-only | off

[capture]                # optional table form, as today
mode = "on"
allowed_paths = ["../shared-fixtures"]
allowed_mcp_servers = ["docs"]
```

- The current [scoped-folder](DATA_BOUNDARIES.md#scoped-folders) allowlist rules
  (#40) apply unchanged: any folder holding a scope file is in scope with its
  subtree; the nearest scope file wins; files at home, above home or directly at a
  top-level personal folder are ignored; Claude Code's own `~/.claude` state for
  in-scope folders is in scope; and `Context.scope_of(path)` in `agentclient.origin`
  returns the scope label of any path. Sections 5 and 7 build on that function.
- **Legacy keys** are still read:

  | Key | Mapping |
  | --- | --- |
  | `project` | The workspace with that id. If `workspace` is also present and differs, the file is invalid and fails closed (capture and recall off), as any invalid file does today |
  | `sharing` | A request, as today, and therefore an upper bound. A file **without** `workspace` keeps today's meaning: `private` or absent → captures are private; `team` → items take the workspace audience when the workspace has no organization entry, otherwise private; `organization` → the workspace audience. In a file **with** `workspace`, only `sharing = "private"` is honored (captures private); other values are ignored with a one-time warning. **(decision)** A legacy file never shares more than it does today; writing `workspace =` is the explicit opt-in |
  | `originals` | Ignored with a one-time warning. Originals are shared only by explicit grant (section 6) |
  | `mode` (legacy names) | As today |

### The audience descriptor

The guardrail hook must decide without a network call per read, so the client
keeps a descriptor per workspace, refreshed from the server
(`GET /enterprise/v3/workspaces/{id}/descriptor`, needs `read`):

```json
{"workspace": {"id": "maple", "name": "maple"}, "epoch": 812,
 "role": "writer", "item_default": "shared", "contributions_default": "shared",
 "contains_session_audience": [{"id": "handbook", "name": "handbook"},
                               {"id": "platform-docs", "name": "platform-docs"}],
 "readable": [{"id": "birch", "name": "birch"}, {"id": "handbook", "name": "handbook"},
              {"id": "platform-docs", "name": "platform-docs"}],
 "repos": ["example.com/example-org/maple"],
 "fetched_at": 1793000000, "refresh_after": 1793000900, "expires_at": 1793086400}
```

`contains_session_audience` lists the workspaces the person can read whose audience
is a superset of this workspace's audience (section 7). `epoch` changes whenever
anything that feeds the descriptor changes (entries, group membership, settings).

- **Where it lives (decision).** In the client profile
  (`workspaces/<id>.json`, mode 0600), **not** in the checked-in `.vaelius`. The
  descriptor is per person (it lists what *that* person may read), it changes
  whenever an access list does, and anyone who can edit the repository can edit the
  scope file. An allow decision must not rest on repository-writable content. The
  scope file carries only `workspace` and `capture`.
- **Refresh.** At SessionStart, then in the background whenever `refresh_after`
  (15 minutes) has passed, and immediately when a capture response reports a newer
  `epoch`.
- **Expiry.** After `expires_at` (24 hours) without a refresh, the hook allows reads
  only inside the session's own workspace and denies other workspaces' folders with
  a message saying the descriptor could not be refreshed (fail safe).
- **The server wins on any mismatch.** Captures, recall and narrowing are re-decided
  on the server from current state. When the server's answer differs from the
  descriptor (a lower role, a missing link, a new epoch), the client refreshes the
  descriptor and the status line says what changed, for example
  `vaelius: you are now a reader in maple — capture is off`.

## 6. Item visibility

### Audience of an item

```
audience(item) =
    {owner}
  ∪ ( ( (workspace_audience(item.workspace)   if not item.private)
      ∪ grantees(item) )
      ∩ ⋂ { workspace_audience(w) : w ∈ item.carried } )
  — and nobody but the owner when item.origin_private
```

- **Inherit.** By default an item's audience is its workspace's audience.
- **`private`** (owner only) is a personal opt-out, set at capture or later.
  Setting it needs `withdraw` or `share`; clearing it is a widening and needs
  `share`, the `writer` role, a non-delegated credential, `owner_sharing = allowed`,
  and an item that is not `origin_private`.
- **Explicit shares** name a principal or a group, for a source, a capture session
  (including later captures in it) or a connection, as today's grants do, and
  cover passages and optionally originals. A share stands in for the workspace
  audience only; every ordinary source rule still applies. Shares are governed by
  `owner_sharing` exactly as today.
- **Carried workspaces** are what `origin_projects` is today: every other
  workspace whose content the session that produced the item had read (section 7).
  A reader other than the owner must be in each of their audiences, so later
  widening one workspace cannot expose another's content. At most 16; a session that
  would carry more becomes private.
- **`origin_private`** comes from the backstop taint (section 7) or a legacy
  `origin_ceiling = 'private'`: owner only, and no share can widen it. As today,
  there is deliberately no override, because it protects other people's content.
- **The person's own setting** survives as `contributions_default`: `shared`
  (default for new principals) or `private`. `private` makes every new item of that
  person private and makes their sessions private (section 7). Switching to
  `private` can also mark their existing items private (an explicit option of the
  command, previewed, then applied). It never widens anything.
- **The per-item ladder and its minimum go away.** Today's terms map as follows:

  | Term of the #28 minimum | Becomes |
  | --- | --- |
  | Person setting | `contributions_default` (opt-out only) |
  | Scope-file request | Legacy keys only (section 5) |
  | Project ceiling | The workspace access list |
  | Tenant ceiling | `widest_entry` |
  | Connection visibility | A connection enrolled `private` makes its items private; otherwise its items inherit the workspace **(decision)** |
  | Origin ceiling and other projects | `origin_private` and carried workspaces |

### Originals

Originals keep their separate tier: readable by the owner, or by a grantee of an
explicit share that includes originals, and in both cases only with the credential
action `source_read`. There is no originals level any more; sharing passages never
shares originals.

### Curated and derived documents

A derived document's audience is the intersection of the audiences of every
source it depends on. Every derived document is capped this way; the
pre-047 connection-visibility rule (`level_capped = 0`) is retired by the migration
(it can only narrow).

### Reader rules that stay

Tenant isolation, active principal, withdrawal and deletion, legal holds, operator
quarantine (held content stays with its author; quarantined readers lose the
organization and share paths), delegation limits, connection reader lists and
freshness, source ACL freshness and moderation all apply on top, unchanged.
Reviewed releases (operator `policy` action, `backend_releases`) stay an explicit
operator path, unchanged.

## 7. Fixed session labels

The model follows Bell–LaPadula: **no read up, no write down**, applied to
audiences. It is intentional, not automatic: a session never silently drifts to a
narrower audience because of what it read.

### Session audience

- A session takes its label when it starts, from the scope label of its starting
  folder (`scope_of(cwd)`):
  - folder in workspace W with `capture = "on"`: session audience =
    `workspace_audience(W)`, or `{me}` when `contributions_default` is `private`,
    `item_default` is `private`, or a legacy `sharing` maps to private (section 5);
  - `capture = "read-only"` or `"off"`, or no workspace: the session captures
    nothing and the label rule is **not** applied (only the credential and
    personal-folder denies are). There is no capture to protect. **(decision)**
- The label does not change when the session `cd`s elsewhere. Only `/clear` or a new
  conversation starts a new label; compaction keeps it.
- A session that started without a workspace stays uncaptured; `use` (below) is
  refused there, because it may already have read anything. **(decision)**

### The read rule

A read is allowed only if **audience(source) ⊇ session audience**: every person who
can read what this session captures could already read the source.

- Superset is decided by the server on current, materialized membership
  (extensional), not by comparing entry lists. An organization entry contains every
  session audience. **(decision)**
- The client learns the result through the descriptor:
  `contains_session_audience` lists the workspaces W with
  `workspace_audience(W) ⊇ session audience`, computed once per (workspace, epoch).
  When the session audience is `{me}` (a private session, from the start or after
  the backstop), every workspace the person can read qualifies; the descriptor also
  lists those (`readable`).
- Every allowed read of another workspace's content adds that workspace to the
  session's carried set, so captured items carry it (section 6). The hook decides
  what the agent may read; the carried set decides who may read what was captured.
  If the hook wrongly allows a read (a stale descriptor), the carried workspace
  still limits the capture to people who can read both; **confidentiality of
  captured content does not depend on the hook's superset decision being right.**
- No write down holds by construction: captured items are stored with the session
  audience, never wider. File edits into other folders are not policed by this
  model beyond the existing personal-folder and credential rules (a stated limit).

### What the rule applies to

| Path or item (`scope_of` category) | Decision |
| --- | --- |
| `credential` (anywhere) | **Deny** (unchanged) |
| `personal` (outside every scoped folder) | **Deny** (unchanged) |
| `project` / `scoped` in the session's workspace, or in a workspace the session is narrowed to | Allow |
| `project` / `scoped` in workspace W listed in `contains_session_audience` | Allow; W is carried |
| `project` / `scoped` in any other workspace, including one the person cannot read or that does not exist | **Deny** |
| `claude_state` | As the folder it belongs to |
| `claude_config`, `tooling`, `temp`, `system`, `allowed_path` | Allow, nothing carried |
| `unscoped_repo`, `unknown` (an existing path outside every scoped folder) | Allow; the session becomes **private** (backstop) |
| `missing` | Allow (no content) |
| Recursive search rooted outside a single scoped folder | As `unknown`, unless it contains a personal folder (deny) |
| Recalled memory | Server-filtered (below) |

**Unscoped, unknown paths (decision, as the owner recommended).** They cannot be
classified, so denying them would block ordinary work (a dotfile, a sibling
checkout without a scope file). They are allowed, and the backstop makes the rest
of the session private, with the #32 status line. A person who wants such a folder
shared adds a scope file to it.

### Enforcement

- **Files.** The PreToolUse guardrail hook applies the table above to file tools
  (Read, Grep, Glob, LSP, NotebookRead, and the paths named in Bash commands, using
  the existing command parser). A failing read returns `permissionDecision: deny`
  with a plain message. It never returns `ask`: the replay in #40
  found that 92 of 95 real sessions (96.8%) read outside their own scope under the
  earlier rules, so a prompt per such read would interrupt nearly every session. The
  message names the folder, the two workspaces and the way forward, for example:

  ```text
  vaelius: denied — ~/src/birch belongs to workspace "birch", which fewer people can
  read than this session's workspace "maple". Ask the person to run
  `vaelius-client use birch` if this session should use it; capture will then be
  limited to people in both.
  ```

- **Recall.** Every recall request (search, context injection, MCP tools) carries the
  session label `{workspace, narrowed, private}`. The server computes the set of
  workspaces whose audience contains the session audience and returns only items
  whose audience does: non-private items whose workspace and every carried workspace
  are in that set, shared items whose share subject's audience contains the session
  audience, and the person's own private items only in a private session.
  Preferences (the person's own standing instructions) are always delivered, as
  today. Clients that do not send a label keep today's #32 behavior.
- **Backstop.** The #32 floating taint stays, demoted to the cases the hook cannot
  see: programs that open files without naming them (`make`, test runners,
  scripts), MCP servers other than Vaelius, non-public web addresses and unknown
  tools. Those make the session private (`origin_private`) exactly as today.

### Explicit narrowing

- The person runs `vaelius-client use <workspace | path | private>`, or tells the
  agent to call the Vaelius MCP tool `use_workspace` (served by the Vaelius MCP
  server, so it is never captured). The server checks that the person can read the
  target and returns the new descriptor.
- The session audience becomes the **intersection**: `audience(A) ∩ audience(B)`, or
  `{me}` for `private`. The target is added to the session's narrowed (and carried)
  set, every later capture carries it, and the status line says so:
  `vaelius: session uses maple ∩ birch — capture limited to people in both`.
- Narrowing is one-way within a session; `/clear` starts over. Narrowing is always
  safe for confidentiality (it only shrinks who can read the capture), so the agent
  may call the tool, but the denial message tells it to ask the person first,
  keeping the narrowing intentional.

### On the wire

Capture events keep the #32 `origin_label` contract, re-read in workspace terms:
`projects` holds the carried workspace ids (narrowed ones included) and
`ceiling: "private"` means `origin_private`. New clients never send `ceiling:
"team"`; the server keeps accepting it from old clients (section 9). Events add
`workspace` (alias of `project`) and `remote` (section 4).

## 8. Roles and credential actions

Credential actions keep their meaning; a role bounds what they do inside one
workspace:

```
effective actions in W = credential actions ∩ actions implied by role(W)
```

| Role | Implied actions in that workspace |
| --- | --- |
| `reader` | `read`, `feedback`, `source_read` |
| `writer` | reader's, plus `ingest`, `correct`, `curate`, `withdraw` and `share` on the person's **own** items |
| `admin` | writer's, plus `moderate`, `manage` (access list, repository links, settings) |

- `source_read` is in `reader` because it only ever works together with ownership or
  an explicit share of originals; it is never a path to anyone else's content.
- **Ownership survives role changes (decision).** A person can always find, read,
  withdraw, delete and make private their own items, whatever their current role or
  membership; only widening (`share`, clearing `private`) needs the `writer` role.
  This keeps owner self-service and erasure working after someone leaves a team.
- **Shares are not role-gated for the reader.** A grantee reads a shared item with
  the credential action `read` alone, as today.
- **New credential action `manage`** gates creating workspaces and every admin-role
  operation. It joins default interactive enrollment and the default provider
  scopes (`vaelius:manage`). Credentials and refresh sessions issued before the
  migration that hold `share` also hold `manage` (`legacy_actions` bit 4, following
  the precedent of bits 1 and 2); stored action lists are not rewritten. Delegations
  cannot include it. **(decision)**
- **Delegations stay workspace-scoped.** `enterprise_delegations.projects` lists
  workspace ids (unchanged values). A delegated credential acts only in those
  workspaces, with `delegation actions ∩ role of the represented person`.
- **Tenant-level powers are unchanged**: `settings_admin`, `settings`, `policy`,
  `audit`, `legal_hold`, operator commands and the access-review roles.

## 9. Enforcement

### One predicate

```
can_read(reader r, item i) =
    ordinary_source_rules(r, i)                 -- tenant, lifecycle, delegation, ACL,
                                                --   connection readers/freshness, quarantine
  ∧ 'read' ∈ actions(r.credential)
  ∧ ( i.owner = r
    ∨ ( ¬i.origin_private
      ∧ ( (¬i.private ∧ role(r, i.workspace) ≥ reader)
        ∨ (shared_with(i, r) ∧ ¬quarantined(r)) )
      ∧ ∀ w ∈ i.carried : role(r, w) ≥ reader
      ∧ legacy_team_origin(r, i) ) )

role(r, w) = max( workspace_access[w, r],
                  org_role(w)  if r active ∧ ¬quarantined(r) ∧ ¬delegated(r.credential) )
             — entries wider than the tenant's widest_entry are ignored
legacy_team_origin(r, i) = i.origin_ceiling ≠ 'team'
                           ∨ (direct_access(r, i.workspace) ∧ ∀ w ∈ i.carried : direct_access(r, w))
direct_access(r, w) = a workspace_access row exists (a principal or group entry, not the org entry)
```

`legacy_team_origin` keeps the exact meaning of an origin ceiling of `team` stored
before the migration ("members of the project and of every carried project"). New
captures never produce it.

A derived document needs `can_read` for every source it depends on. Originals need
`can_read`, `source_read`, and ownership or a share that includes originals.

It is implemented three times, as now, and a parity test keeps them in agreement:

1. **Python oracle**: `agenthub.workspace_access` (replacing the sharing parts of
   `agenthub.project_sharing`), used by `_visible_source`, `_document_allowed` and
   `agenthub.raw_access`.
2. **Indexed SQL filter**: `agenthub.search_permissions.predicate`, used to rank,
   authorize and re-validate at delivery.
3. **Row-level security**: `search_document_allowed`, with a helper
   `search_reader_in_audience(definition, tenant, actor)` replacing
   `search_passage_shared`, `search_origin_reaches` and the visibility clause, and
   `search_passage_granted` extended to group grantees. The restricted reader role
   applies it to every ranking query, so a denied row never leaves the database.
   The RLS function computes roles from the tables itself (one indexed probe per
   dependency); it never trusts a role list passed in the request context.

### Data structures for scale

The aim: per-request work proportional to the number of workspaces and groups a
reader touches, per-row work constant, and nothing ever enumerated per user per
item.

| Structure | Holds | Size bound |
| --- | --- | --- |
| `groups`, `group_edges` | Group objects and direct edges | Groups × direct members |
| `group_closure(tenant, group_id, principal)` | Transitive principal members, rebuilt transactionally on change or sync. Index `(tenant, principal)` | Σ group sizes |
| `workspace_entries` | The access lists | Entries |
| `workspace_access(tenant, workspace, principal, role)` | Effective role from principal and group entries (not the org entry), maintained with the closure. Primary key `(tenant, principal, workspace)` | Σ non-org audience sizes (for example 1,000 workspaces × 50 people = 50,000 rows) |
| `enterprise_projects.org_role` | The org entry's role, denormalized | One column |
| `enterprise_sources.external_project` | The item's workspace (the id is unchanged, so no rewrite) | Existing |
| `enterprise_sources.private`, `origin_projects`, `origin_ceiling` | The rest of the item audience | Existing or one small column |
| `enterprise_documents.source_project`, `raw_shared` | Workspace and an "audience is not private" prefilter (`raw_shared` becomes 0 private, 1 workspace) | Existing |
| `search_policies` | Interned policy definitions, as today; keys `project` (workspace), `owner`, `private`, `origin_projects`, `origin_ceiling` replace `visibility` and `sharing` | Distinct audiences, not items |

Per request the server computes once:

- `R` = workspaces where the reader's role ≥ `reader`: one indexed range scan of
  `workspace_access` plus the org-entry workspaces (a partial index on `org_role`);
- `G` = the reader's groups from `group_closure`.

The search filter is then `source_project = ANY(R) AND (private = 0 ...)`, carried
sets checked with `origin_projects <@ R`, the owner path by `raw_owner`, and shares
through the existing `raw_granted` prefilter plus a `raw_source_grants` probe over
`G`. With millions of passages, the filter stays index-driven: B-tree on
`(tenant, source_project)` for lexical candidates, and pgvector's iterative HNSW scan
(already enabled, `hnsw.iterative_scan`) for vector candidates. Recall's session
filter (section 7) is the same shape with `R` replaced by the session's superset
list. Superset lists are computed per (workspace, narrowed set, epoch) with set
difference over `workspace_access` and cached server-side until the epoch changes.

`internal_project` (the `enterprise:` scope digest) stays as a coarse partition
key for the knowledge pipeline, derived from `R`; it is never the authorization
decision.

## 10. Access reviews, offboarding and lifecycle

### What changes

- **The access report** lists, per principal, every workspace they can read with
  their effective role and **how**: a direct entry, which group (with its source and
  sync state), or the organization entry. New line kinds: `workspace_entry` (subject,
  role, granted by, created), `group_membership` (local only; revocable here),
  `synced_group_membership` (reported, revoked at the source), `identity_link`,
  `repo_link`. `project_grant` lines become `workspace_access` lines.
- **New flags:** `admin_role` (privileged), `org_wide_entry`, `group_stale`
  (access derived from a group that is not `current`), `pending_identity_link`.
- **Revoking** a `workspace_entry` removes the entry; a local `group_membership`
  removes the edge; a `synced_group_membership` records the decision with outcome
  `change_at_source`, and the reviewer may also remove the group's entry from the
  workspace.
- **Offboarding** additionally removes the principal's direct entries, removes them
  from local groups, deactivates their identity links, and lists in `remaining` every
  workspace where they were the last principal admin (a tenant administrator must
  assign one). Synced group memberships disappear on the next sync; deactivation
  already ends access before that.

### What does not change

Owned content is never deleted by a review or by offboarding. Withdrawal, deletion,
owner self-service deletion, scheduled retention, legal holds, subject erasure and
the deletion journal work per source exactly as today. Owner retention and the
`forget` selection take a workspace where they took a project (same ids). Archiving
a workspace hides its items from everyone but their owners; purging a workspace's
content uses the existing purge per source.

## 11. Migration from the current model

Migration `048_workspaces.sql` plus a Python step run in one transaction under
the projection lock.

### Mapping

| Today | After |
| --- | --- |
| Project | Workspace with the **same id**; `name` = id |
| Explicit and directory members of project P | A `local` group `P-members` belonging to workspace P (managed by P's admins) holding the explicit members and nesting each `scim` group mapped to P; entry `group:P-members → writer` |
| Project ceiling (or tenant default) `organization` | Also `organization → reader` |
| Project ceiling `team` | Nothing more (members are the audience) |
| Project ceiling `private` | `item_default = private` |
| Tenant ceiling `organization` / `team` / `private` | `widest_entry = organization` / `people_and_groups` / `none` |
| Directory group | `scim` group, same external id |
| `cloud_directory_group_projects` row | Nesting edge from the `scim` group into `P-members` |
| Raw grant to project P | Grant to `group:P-members` |
| Person `max_sharing` | `private` → `contributions_default = private`; `organization` → `shared`; `team` → `shared`, unless the person is a member of a workspace that gets an organization entry, then `private` **(decision)** |
| Connection visibility | `private` stays private; `team` on a connection whose workspace gets an organization entry becomes `private` **(decision)**; otherwise inherits |
| Who administers each workspace | Nobody gets an `admin` entry: an admin is also a reader, so that would widen access. Tenant administrators manage migrated workspaces, as operators manage projects today, until they add admins. The report lists them. **(decision)** |

### Items

For every source the migration computes the **current effective level** under
today's rules (stored level capped by every current ceiling, as
`ProjectSharingMixin._current_level` does) and then:

| Current effective level | New item |
| --- | --- |
| `private` | `private = 1` |
| `team`, workspace without an org entry | inherits |
| `team`, workspace with an org entry | `private = 1` plus a passage share to `group:P-members` |
| `organization` | inherits |
| Originals level `team` or `organization` | An originals share to `group:P-members` |

Origin columns are never touched (the 046 trigger still guards them); an
`origin_ceiling` of `private` reads as `origin_private`. Every derived document gets
`level_capped = 1`. Policy definitions change, so the migration rebuilds the
`search_policies` projection (`search_rebuild_permissions()`).

### The invariant and its proof

> **No item, passage, derived document or original becomes readable by anyone who
> cannot read it immediately before the migration.**

- **Test.** `test_access_migration.py` builds tenants covering every combination
  of: stored level × person setting × project ceiling (own or inherited) × tenant
  ceiling × connection visibility and reader list × origin ceiling × carried projects
  × grants (principal, project; passages, originals; owner- or policy-issued; both
  `owner_sharing` values) × quarantine × delegation × derived documents with
  `level_capped` 0 and 1 × originals level × `source_read`. It records the full
  reader × item relation through all three old layers, migrates, records it again
  through all three new layers, and asserts `after ⊆ before` for every layer, plus
  `after = before` except for an explicitly listed set of expected narrowings (for
  example `level_capped = 0` documents and organization-level originals that become
  member shares).
- **Runtime guard.** The migration computes, for each distinct old policy
  definition (not per item), the reader set before and after with the existing
  `explain` enumerator, and **aborts the transaction** if any after-set is not a
  subset of its before-set. It writes a content-free report: counts of items per
  mapping row, items narrowed and why, people whose `contributions_default` became
  `private`, connections narrowed. `vaelius migrate-access --dry-run` produces the
  same report without committing.

### Kept, changed, dropped

Reference counts are lines, from `git grep -c` at the base of this document.

| Object | Fate |
| --- | --- |
| `enterprise_projects` | **Kept** as the workspace table (renaming it would touch every query for no behavior change); adds `name`, `created_by`, `created`, `org_role`, `item_default` |
| `enterprise_memberships` (48 lines in 21 files; 43 in 18 non-test) | **Dropped**, replaced by `workspace_access`. A read-only view of the same name over `workspace_access` stays for one release for operator scripts |
| `cloud_explicit_memberships` (11 lines, 4 files) | **Dropped**: rows become `P-members` edges |
| `cloud_directory_memberships` (5 lines, 3 files) | **Dropped**: replaced by `group_closure` |
| `cloud_directory_group_projects` (4 lines, 3 files) | **Dropped**: rows become nesting edges |
| `cloud_directory_users`, `cloud_directory_resources`, `cloud_directory_events` | **Kept**: the SCIM adapter's state |
| `max_sharing` on principals, projects and organizations, `default_max_sharing` (140 lines in 23 files; 105 lines in 12 non-test files: `project_sharing.py`, migrations 042/045/046, `cloud_local.py`, `search_permissions.py`, `access_review.py`, `cloud_api.py`, research `cloud_live.py`, client `cli.py`, `cloud_contract.py`, `capture_notice.py`) | **Dropped** from every check; columns kept frozen for one release, then removed. Replaced by `contributions_default`, entries and `widest_entry` |
| `enterprise_sources.sharing`, `sharing_requested`, `originals_sharing`, `originals_requested` | **Frozen** (history only), removed with the above. `private` is added |
| `enterprise_sources.origin_*` | **Kept**, unchanged, still fixed by trigger |
| `raw_source_grants` (33 lines, 8 files) | **Changed**: `grantee_kind` adds `group`; `project` rows are rewritten to the members group and the kind is then refused |
| `enterprise_sharing_changes` | **Kept**; `tier`/levels extended with `private`/`shared` for new changes |
| `search_policies`, `search_source_policies`, `search_document_policies` | **Kept**; definitions change (rebuild) |
| `search_passage_shared`, `search_origin_reaches` | **Dropped**, replaced by `search_reader_in_audience` |
| `search_document_allowed`, `search_passage_granted` | **Changed** |
| `search_permissions.SHARE`, `LEVEL`, `ORIGIN_TERMS`, `ORIGIN_MEMBERS`, `ORIGIN_REACHES`, `CEILINGS` | **Replaced** by one `AUDIENCE` clause |
| `ProjectSharingMixin` (`project_sharing.py`): ceilings, `_current_level`, `_passage_shared`, `_level_reaches`, `_origin_reaches`, `set_project_sharing`, `set_tenant_sharing_*`, `sharing_policy_status` | **Replaced** by `workspace_access.py`; `_passage_readers` (explain) kept, re-implemented on the new predicate |
| `_readable_scopes`, `_scope` (35 lines, 14 files) | **Changed**: derived from `R` |
| `vaelius sharing-policy` command | **Replaced** by `vaelius workspace` and `vaelius tenant-policy` |
| `vaelius-client sharing --set LEVEL` | **Changed**: `--set shared|private` |
| `delegations.projects`, `origin_projects`, `external_project` values | **Unchanged** (same ids) |

## 12. Implementation plan

Each PR updates the user documentation for what it ships and must pass CI.

### PR A: groups, workspaces, access lists, repo linking, migration

Server: migration 048; `workspace_access.py`; the three predicate layers; group
store (local and `scim`); workspace, entry, settings, group and repo-link routes
(`/enterprise/v3/workspaces/*`, `/enterprise/v3/groups/*`) with route-action
classification; `manage` action; capture checks (writer role, linked remote);
`contributions_default`; the migration report and guard. Client: `workspace` and
legacy keys in `.vaelius`, `remote` on captures, `vaelius-client workspace` and
`group` commands, `sharing --set shared|private`.

Acceptance tests:

1. Parity: Python, SQL filter and RLS agree on `can_read` across every role, entry
   kind, group nesting depth, `private`, shares (principal, group), carried
   workspaces, `origin_private`, legacy `team` origin, quarantine, delegation and
   `widest_entry` value.
2. Migration: the invariant test above passes; the runtime guard aborts on a
   deliberately widened fixture.
3. Every mode in the section 2 table yields the expected reader set.
4. Self-service: any principal with `manage` creates a workspace and is its admin;
   `workspace_creation = admins` refuses others; `widest_entry` suspends and restores
   entries at query time; the last-admin rule refuses the removal; a synced group
   never satisfies it.
5. Roles: a reader cannot ingest, correct, share or moderate even with those
   credential actions; a writer cannot manage; an owner demoted to reader can still
   find, withdraw, delete and make private their own items, but cannot share.
6. Repo linking: normalization table cases; one remote cannot link to two
   workspaces; a capture with an unlinked remote is rejected and stays queued in the
   client; a capture with no remote is accepted for a writer.
7. Groups: cycles refused; closure is correct after add, remove and nesting edits;
   a `scim` group in `requires_reconcile` contributes nobody.
8. Scale (a scripted benchmark whose results are recorded in the PR, not part of
   the default CI run): on a generated tenant with at least 1,000,000 passages,
   1,000 workspaces and 10,000 principals, a search runs the same number of SQL statements for an
   owner, a member and an organization reader, and `EXPLAIN` shows index use for
   the workspace filter.
9. Access report and offboarding show and remove the new line kinds.

### PR B: fixed session labels

Client: session label at SessionStart; descriptor fetch, cache, refresh and expiry;
the hook's read rule over `scope_of`; deny messages; `vaelius-client use` and the
MCP tool `use_workspace`; status lines; carried set on captures; taint demoted to
backstop. Server: descriptor route; superset computation and cache; recall filter by
session label; narrowing check.

Acceptance tests:

1. A session started in A reads A's folders; reads a folder of B where
   `audience(B) ⊇ audience(A)` and carries B; is **denied** (plain deny, never `ask`)
   a folder of C where it is not, and a folder naming an unknown workspace.
2. Credentials and personal folders are denied everywhere; `claude_state` follows
   its folder; temp, tooling, system and `allowed_paths` are allowed without
   carrying.
3. An unscoped existing path is allowed and makes the session private, with the
   status line.
4. `use B` makes the session `A ∩ B`; B's folders become readable; later captures
   carry B; a person who cannot read B is refused; `use` is refused in an unscoped
   session; `/clear` resets; compaction does not.
5. Recall in a shared session never returns the person's own private items or items
   whose audience does not contain the session audience; a private session receives
   everything the person can read.
6. Descriptor expiry denies other workspaces' folders and keeps the own workspace
   working; a server answer with a newer epoch refreshes it; a role downgrade turns
   capture off with a status line.
7. Widening workspace A after the fact does not expose B-derived items captured in
   an `A` session to the new A readers who cannot read B.
8. The hook makes no network call (a test with networking disabled).
9. `capture = "read-only"` sessions are not label-restricted and capture nothing.

### PR C: GitHub App team sync and device-flow identity link

Server: GitHub App configuration (App id, private key file, webhook secret file, all
re-read on change like the introspection secret); device-flow link; webhook receiver;
6-hourly reconcile; `sync_state` and `max_staleness`; repository-read verification
on capture. Client: `vaelius-client link github`.

Acceptance tests:

1. Device flow links the numeric GitHub user id; a second principal cannot link the
   same account; a typed login never links.
2. Signed webhooks trigger a state-based refetch; unsigned or replayed deliveries are
   refused; out-of-order deliveries converge to the source state.
3. A member removed at GitHub loses access after the webhook, or at the latest after
   the next reconcile.
4. With the GitHub fixture unreachable past `max_staleness`, the group contributes
   nobody, the workspace's principal admins keep access, and the report flags
   `group_stale`; a successful sync restores members.
5. Uninstalling the App marks its groups `failed` with the same effect.
6. A capture from a GitHub remote by a person whose linked account lacks read access
   is rejected (`403 repo_access_unverified`).
7. Child-team members reach a workspace that names the parent team.

## 13. Prior art

- **GitHub repository roles and visibility.** Same role ladder and the same
  "access list of people and teams, teams synced from the org" shape; organization
  visibility is our organization entry. GitHub does not label a session; we add
  that because agents carry content between repositories.
- **Google Drive sharing.** Items inherit the folder's (here, workspace's)
  audience and can be shared further to people or groups; "restricted" items are
  our `private`. Drive allows any editor to widen; we keep widening to owners of
  their own items and to workspace admins.
- **Notion teamspaces.** Open, closed and private teamspaces map to org-read,
  group-only and fully private workspaces; teamspace owners are our admins.
- **Zanzibar-style relationships.** Entries and group edges are relation tuples
  (`workspace#reader@group#member`), and `group_closure` plus `workspace_access` is
  a materialized Leopard-style index. We keep it in PostgreSQL rather than an
  external engine because the hard query is search-time filtering of millions of
  passages inside one ranking statement: an external check API would mean
  per-candidate calls or exporting the reader's reachable set anyway, and would
  lose row-level security as a second line of defense.
- **Bell–LaPadula / MLS.** Session labels are BLP's no-read-up and no-write-down,
  with audiences as labels ordered by superset. As in MLS systems, raising a
  session's level is not possible; it can only be narrowed, explicitly.
- **Information-flow control.** Purview-style floating labels (labels that follow
  content and tighten automatically) are kept only as the backstop for reads the
  hook cannot see; the primary control is the explicit, fixed label.

## 14. Open questions

1. **Should `moderate` follow the role alone?** It stays a non-default credential
   action intersected with the `admin` role, as specified above. Making admin imply
   it (adding it to default enrollment) would remove a manual step for every
   workspace admin, at the cost of making a privileged action default.
2. **First capture from an unlinked repository.** Section 4 rejects it until an
   admin links the remote. An alternative is to let a workspace admin's own first
   capture link it, with a confirmation; this is friendlier for new workspaces but
   makes linking implicit.
3. **`max_staleness` default.** 24 hours balances outage tolerance against how long
   a removal can be missed when webhooks are lost. Some tenants may want 1 hour.

## Appendix: recorded decisions

Points the owner's brief left open, decided in this document:

| Point | Decision | Section |
| --- | --- | --- |
| Tenant restriction knob | One ordered setting, `widest_entry` (`none` < `people_and_groups` < `organization`), applied at query time | 2 |
| Orphaned workspaces | Last-admin rule; synced groups never count; workspaces with no admin are managed by tenant administrators | 2 |
| Tenant administrator | Break-glass access-list management, audited; no content access | 2 |
| Private-ceiling projects | `item_default = private` workspace setting | 2, 11 |
| Stale or failed sync | The group contributes nobody until the next good sync | 3 |
| Which remote | `origin`, else the only remote, else none | 4 |
| Capture without a remote | Accepted on the writer role | 4 |
| Descriptor location | Client profile, not the checked-in `.vaelius` | 5 |
| Legacy `sharing` | Kept as an upper bound; legacy files never share more than today | 5 |
| Connection visibility | `private` → items private; otherwise inherit | 6 |
| Read-only, off and unscoped sessions | No label rule; nothing is captured | 7 |
| Superset semantics | Extensional, on current materialized membership, computed server-side | 7 |
| Unknown workspace named by a scope file | Deny | 7 |
| Unscoped, unknown paths | Allow; backstop makes the session private | 7 |
| Ownership after demotion | Owner rights survive; widening needs `writer` | 8 |
| New action | `manage`, default, legacy bit 4 from `share` | 8 |
| Person and connection settings of `team` in an org-wide workspace | Become `private` at migration | 11 |
| Admins after migration | None; tenant administrators manage migrated workspaces | 11 |
| Migrated membership | One `P-members` local group per workspace, nesting the mapped `scim` groups | 11 |
