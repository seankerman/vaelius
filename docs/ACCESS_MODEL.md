# Access model: workspaces, groups and session labels

**Status: PR A (migration 048: groups, workspaces, access lists, repository
linking and the single read predicate), PR B (migration 049: fixed session
labels, the read rule in the guardrail hook and the recall filter) and PR C
(migration 050: GitHub App team sync and the device-flow identity link) were
implemented; **PR C was removed again by migration 054** (owner decision: Vaelius
stays separate from GitHub). The repository-read check of section 4 is dropped with it.
Since migration 053: reads of folders without a scope file are denied in a labelled
session, workspace visibility is chosen on the server at creation (default org), and
chats can be made read-only (sections 2 and 7).** Three pull requests implement the
design in order (PR A, PR B, PR C; see [the plan](#12-implementation-plan)). The
[privacy model](PRIVACY_MODEL.md) describes what the service enforces today; where
PR A or PR B resolved a point left open here, the text below says so and the
[recorded decisions](#appendix-recorded-decisions) list it. Each implementation PR updates that document, the
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
- **Groups** are one object type with a `source`: `scim` (identity-provider groups, today's directory
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
  anyone who cannot read it today. As implemented, every existing item becomes
  private to its owner and members become writers, so nothing can widen
  (section 11).

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
  and becomes its first `admin` through a `principal` entry. Admins are the creator
  plus whoever an admin adds with the `admin` role.
- **Visibility is chosen at creation, on the server only (migration 053).**
  `vaelius-client workspace create <name>` (route `POST
  /enterprise/v3/workspaces/create`) defaults to **org**: an `organization → reader`
  entry, so everyone signed in to the tenant can read it (there is no
  internet-public tier). The command prints `visible to everyone in the org (use
  --private to restrict)`. `--private` (`visibility: private`; no reason needed) is
  only the creator as admin plus whoever the admins add. `widest_entry` still caps
  org: an explicit org is refused (`entry_wider_than_tenant_allows`) and the default
  falls back to private, saying so. Changing it later is an admin operation:
  `vaelius-client workspace set-visibility <name> private|org` (`POST
  /enterprise/v3/workspaces/visibility`), or the entry commands below. Private to org
  reports `items_now_visible_to_org` (the workspace's active shared items with no
  narrower origin); org to private needs no confirmation. Both return
  `newly_unreadable` (section 7). The first session in a folder whose workspace is
  org-visible (including a newly linked repository) shows the person, once, that it
  is visible to everyone in the org. There is no visibility key in the scope file: a
  `visibility` there is ignored with a one-time warning for the person. `workspace
  show`, `workspace list` and the descriptor report `visibility` (`org` while an
  organization entry exists).
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

### GitHub teams (removed, migration 054)

PR C synced GitHub teams into `github` groups through a GitHub App and linked
people to GitHub accounts with the device flow. **Migration 054 removed both**
(owner decision): team repository permissions are not the same thing as memory
audiences, and the sync was accident-prone. The migration deletes every `github`
group with its entries, nesting edges, closure rows and shares (removing a group
only removes access), re-materializes the groups and workspaces that used one,
drops the GitHub tables and restricts `groups.source` to `scim` and `local`. The
`gh-`/`gh_` group-name prefixes stay reserved so nothing that named a removed group
can attach to a new one. A `github` section in `runtime.json` now stops the service
at startup (`github_sync_removed_delete_the_github_section`).

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
| SCIM group with an out-of-order event awaiting reconcile | `stale` | The group contributes **no members** to `group_closure` |
| Next successful sync | `current` | Membership is rebuilt from the source |

So a sync outage can only take access away, never leave a removed person in.
Because a synced group never counts as a workspace's last admin, admins keep access
and can see the state: the workspace and group views, the access report and the
service status show `group_stale` with the last successful sync time. Every state
change is audited (`group_sync`, with `applied`, `stale` or `failed`).

## 4. Repository linking

A workspace does not need a repository. Any folder whose scope file names the
workspace captures into it when the person is a writer there; linking is an extra
safeguard for folders that are Git repositories. It is internal to Vaelius: it
compares normalized remotes and never calls GitHub's API, and nothing about who can
read a workspace is derived from GitHub (owner decision).

- A workspace lists linked repositories by **normalized remote**: `host/path`, all
  lower case, with the scheme, user, port and a trailing `.git` or `/` removed.
  `https://example.com/Example-Org/Maple.git`, `ssh://git@example.com:22/example-org/maple`
  and the scp form `git@example.com:example-org/maple.git` all become
  `example.com/example-org/maple`. Paths may have more than two segments (subgroups).
- **One repository links to one workspace**: `(tenant, remote)` is the primary key
  of `workspace_repos` (per tenant, so one tenant can neither block nor probe
  another tenant's remotes). Linking a remote that is already linked elsewhere is
  refused (`409`). Linking and unlinking need `admin` on the workspace (and the
  credential action `manage`) and are audited (`repo_link`, `repo_unlink`).
- **The client sends the remote with every capture** (`remote`, normalized as
  above). It uses the remote named `origin`, else the only remote, else none.
  **(decision)**
- **The server rejects a capture whose remote is not linked to the named workspace**
  (`409 repo_not_linked`). The client keeps such events queued and unshared, as it
  does for other rejected fields, and the status line says which admin action fixes
  it. A capture with no remote (a scoped folder that is not a Git repository) is
  accepted on the person's writer role alone.
- **An admin's first capture links the repository (decision, resolving open
  question 2).** When the remote is linked to no workspace and the capturing person
  is an `admin` of the named workspace with a non-delegated credential holding
  `manage`, the capture links it, audited as `repo_link` / `automatic`, and the
  capture receipt carries a `notice` the client shows on the status line. A
  non-admin's capture from an unlinked remote is still rejected.
- **Dropped (migration 054):** PR C deferred a check that the capturing person's
  linked GitHub account could read the repository. With the GitHub link removed it
  is not planned: linking stays a comparison of normalized remotes inside Vaelius.

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
  top-level personal folder are ignored, both for labelling and for capture
  discovery (one shared predicate), so they never activate capture; Claude Code's own `~/.claude` state for
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
keeps a descriptor per session label, refreshed from the server. **(decision, PR B)**
The route is `POST /enterprise/v3/workspaces/descriptor` with the session label
`{workspace, narrowed, private, sharing}` as the body (needs `read` and a role in
every workspace it names), because a narrowed session's list depends on the
narrowed set, not on one workspace. A narrowed entry may be a workspace name or id,
or a linked remote:

```json
{"workspace": {"id": "maple", "name": "maple"}, "epoch": 812,
 "role": "writer", "item_default": "shared", "contributions_default": "shared",
 "session": {"workspace": "maple", "narrowed": [], "private": false},
 "contains_session_audience": [{"id": "handbook", "name": "handbook"},
                               {"id": "platform-docs", "name": "platform-docs"}],
 "readable": [{"id": "birch", "name": "birch"}, {"id": "handbook", "name": "handbook"},
              {"id": "platform-docs", "name": "platform-docs"}],
 "narrower": [{"id": "birch", "name": "birch", "hidden_items": 12,
               "missing_readers": {"count": 3, "names": ["carol", "platform (group)"],
                                   "more": 0}}],
 "repos": ["example.com/example-org/maple"],
 "fetched_at": 1793000000, "refresh_after": 1793000900, "expires_at": 1793086400}
```

`contains_session_audience` lists the workspaces the person can read whose audience
is a superset of this workspace's audience (section 7). `epoch` changes whenever
anything that feeds the descriptor changes (entries, group membership, settings).
`narrower` lists the readable workspaces that do not contain the session audience,
with who of that audience they leave out and how many shared memories recall leaves
out (section 7, "Why can't this session read X?").

- **Where it lives (decision).** In the client profile
  (`workspaces/<id>.json` for a workspace's own label, and per session in
  `session-labels.json`, mode 0600), **not** in the checked-in `.vaelius`. The
  descriptor is per person (it lists what *that* person may read), it changes
  whenever an access list does, and anyone who can edit the repository can edit the
  scope file. An allow decision must not rest on repository-writable content. The
  scope file carries only `workspace` and `capture`.
- **`visibility`** is not a scope-file key: it is ignored with a one-time warning,
  because who can read a workspace is set on the server (section 2). A `read` key
  (a recall list from an earlier draft, never shipped) is ignored the same way.
- **Refresh.** At SessionStart, then at the next prompt once `refresh_after`
  (15 minutes) has passed, and immediately when a labelled search response reports
  a newer `session_epoch`. **(decision, PR B)** The epoch is the sum of the tenant's
  workspace epochs, which every entry, group-membership, link and settings change
  moves; search responses carry it instead of capture receipts, because recall is
  the request a session makes on every prompt.
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
  `owner_sharing` exactly as today. **(decision)** A share covers the redacted
  passage tier and, when it includes them, originals; it never reaches a curated
  or derived document, which needs the inherited audience of every source (grants
  before PR A never covered derived documents either).
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

### Past conversations

Conversations from before capture enter only as **owner-private** items
(`private = 1`), whatever the workspace's `item_default` or the person's
`contributions_default`. The import itself never sets `origin_private`, so the owner
can share them afterwards with the ordinary owner controls (per session or per
source); only a pushed session's replayed taint can, exactly as live capture would:

- **Push** (`vaelius-client push-session`): one session at a time, by its author,
  only from a scoped folder where they are a writer. It is replayed through the
  origin labels and session taint as live capture, so its carried workspaces and any
  `origin_private` taint are recorded too, and it sends no sharing request.
- **Server backfill** (`vaelius backfill`, operator only): the service cannot
  evaluate the author's scoped folders or origin labels, so items are owner-private
  through a per-author connection enrolled `private` and no sharing request; the
  command has no sharing options. Authors and workspaces come only from the
  operator's mapping file, and the author must hold `writer` in the workspace.

Neither path can create an item with a wider audience than its owner alone.

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
| `claude_memory` (`~/.claude/projects/<name>/memory/`, Claude Code auto memory) | Allow; the session becomes **private** (backstop). Any earlier session, whatever it read, may have written it |
| `claude_config`, `tooling`, `temp`, `system`, `allowed_path` | Allow, nothing carried |
| `unscoped_repo`, `unknown` (an existing path outside every scoped folder), and Claude Code state of a folder that maps to no scoped folder | **Deny** (one line), unless the session is already private (since migration 053) |
| `missing` | Allow (no content) |
| Recursive search rooted outside a single scoped folder | As `unknown` (deny), unless it contains a personal folder (deny) |
| Recalled memory | Server-filtered (below) |

**Unscoped, unknown paths (owner decision, migration 053).** In a labelled session
they are **denied** instead of narrowing the session: allowing them made one stray
read turn the whole rest of a session private, silently for everyone else. The
hook answers with one line and never `ask`:

```text
vaelius: ~/src/sibling isn't in a Vaelius folder (no .vaelius). Add one, or run
'vaelius-client session use private' to read it in a private session.
```

A session that is already private (`use private`, a private workspace or setting,
or the backstop) may read them. Unchanged: credentials and personal folders are
denied; scoped folders the session may read, Claude Code configuration and state
allowed by #40/#47, tooling, caches, system paths and temporary folders are allowed
with no restriction; read-only, off and unlabelled sessions get no label rule. The
#32 taint stays only as the backstop for reads the hook cannot see (below).
(Before migration 053 such reads were allowed and made the session private.)

### Enforcement

- **Files.** The PreToolUse guardrail hook applies the table above to file tools
  (Read, Grep, Glob, LSP, NotebookRead, and the paths named in Bash commands, using
  the existing command parser). A failing read returns `permissionDecision: deny`
  with a plain message. It never returns `ask`: the replay in #40
  found that 92 of 95 real sessions (96.8%) read outside their own scope under the
  earlier rules, so a prompt per such read would interrupt nearly every session. The
  message names the folder, the two workspaces and the way forward, for example:

  ```text
  vaelius: denied — ~/src/birch: birch isn't visible to carol, dave and 1 more (in
  this session's audience, maple). The person can run 'vaelius-client session use
  birch' for this session, or ask birch's admin to add them.
  ```

  (As implemented, one line; the names come from the descriptor's `narrower`
  summary, see "Why can't this session read X?" below. Against a service without
  that summary the earlier wording stays: "is visible to fewer people than this
  session".) A workspace the person cannot read, an unknown one
  and a scope file that does not parse get their own one-line denials. After the
  descriptor expires the message says it could not be refreshed. Edit tools are
  not policed by the label; Bash paths use the origin-label command parser.

- **Recall.** Every recall request (search, context injection, MCP tools) carries the
  session label `{workspace, narrowed, private}`. The server computes the set of
  workspaces whose audience contains the session audience and returns only items
  whose audience does: non-private items whose workspace and every carried workspace
  are in that set, shared items whose share subject's audience contains the session
  audience, and the person's own private items only in a private session.
  The person's own private preferences reach only a private session, like their
  other private items (since migration 051; before it they were always delivered).
  Clients that do not send a label keep today's #32 behavior.
  **(decisions, PR B)** The label travels as `session_label` in the body of the
  recall routes (search, evidence, context, timeline, project history and episode)
  and, for the MCP connection, as the `X-Vaelius-Session-Label` header the header
  helper derives from the connection's folder. The header helper cannot tell which
  conversation uses the connection, so MCP recall uses the label a session starting
  in that folder gets; a later narrowing or private session only widens what may be
  recalled, so this is never wider than the session's own label. A share qualifies
  when its grantee is a group whose members contain the session audience (or the
  person alone, which makes the session private); the person's own private items
  never qualify through a share. **(Since migration 051)** private preferences and
  "always load" remembered items follow the same rule (see "Remembered items and
  preferences" below), and the header helper narrows the folder's label by the
  label of the session last active in that folder, recorded by the hooks; the
  composition can only narrow it (`agentclient.session_label.compose`). The filter is a fourth term of the one predicate in
  all three layers (`_session_admits`, `search_permissions.SESSION`,
  `search_session_admits`), computed on every request; nothing is cached on the
  server. The label replaces the cross-project `scope = "all"` search option of #28:
  with a label, recall reaches every workspace in the superset list: the current
  workspace's results ranked first, then org workspaces the session may read, and the
  person's own private items only in a private session (private-team memory stays out
  of org sessions by the same rule). The MCP tool no longer offers `scope`, and
  unlabelled HTTP requests keep it. A read-only chat's label (`read_only`) is treated
  as private for recall: it writes nothing, so it may recall everything the person
  can read.
- **Backstop.** The #32 floating taint stays, demoted to the cases the hook cannot
  see: programs that open files without naming them (`make`, test runners,
  scripts), MCP servers other than Vaelius, non-public web addresses and unknown
  tools. Those make the session private (`origin_private`) exactly as today.

### Explicit narrowing

- The person runs `vaelius-client session use <workspace | path | remote | private>`
  (`vaelius-client use` is a shorthand), or tells the agent to call the Vaelius MCP
  tool `use_workspace` (served by the Vaelius MCP server, so it is never captured).
  The server checks that the person can read the target and returns the new
  descriptor. **(decision, PR B)** Without `--session`, the command narrows the
  session active most recently in the current folder's scoped folder (or the one
  named by `CLAUDE_CODE_SESSION_ID` when run from the agent's shell). The MCP
  tool's result is applied by the client's PostToolUse hook for that session,
  which then refreshes the descriptor for the full narrowed label.
- The session audience becomes the **intersection**: `audience(A) ∩ audience(B)`, or
  `{me}` for `private`. The target is added to the session's narrowed (and carried)
  set, every later capture carries it, and the status line says so:
  `vaelius: session uses maple ∩ birch — capture limited to people in both`.
- Narrowing is one-way within a session; `/clear` starts over. Narrowing is always
  safe for confidentiality (it only shrinks who can read the capture), but it stays
  intentional: the denial says what the person can run, and the tool's description
  lets the agent offer it and forbids calling it unless the person asks or agrees.

### Read-only chats (migration 053)

- `vaelius-client session read-only` makes the rest of the current chat
  read-only: nothing from it is written any more, not even private items. The agent
  may switch a chat **to** read-only with the MCP tool `read_only_session` when the
  person asks; there is no tool or command to switch back. Only `/clear` or a new
  chat resets it. The status line says `vaelius: read-only — nothing from this chat
  is saved`, and when switched mid-chat adds that earlier captures stay until the
  person runs `vaelius-client forget --session current` (the existing "forget this
  session").
- **Enforced by the service.** The client stops capturing and tells the service the
  chat's session id and the folder's recent MCP connection ids (`POST
  /enterprise/v3/sessions/read-only`, kept as digests in `read_only_sessions`,
  migration 053; one-way). The MCP connection carries it through the per-folder
  label record (`read_only`, which, like every record field, can only narrow) and
  the `X-Vaelius-Read-Only: 1` header. The service refuses **every** write route and
  tool (capture parts and sources, boundaries, receipts, remember, feedback,
  corrections, withdrawals, shares, owner changes, workspace administration) with
  `403 read_only_session` and one clear message when the request carries the header
  or a `read_only` label, or names a registered session (the MCP connection, a
  capture's conversation, a boundary or a receipt). Reads keep working.
- Folders with `capture = "read-only"` get the same refusal: the header helper sends
  the header for them and the hooks register the chat at session start.
- `push-session` refuses a chat that ran read-only (recorded in the profile's
  `read-only-sessions.json`, which outlives `/clear`).
- A read-only chat captures nothing, so the label read rule does not apply to it
  (as for `capture = "read-only"` folders), and recall treats it as private: it may
  recall everything the person can read.

### Why can't this session read X?

Repo1's workspace is read by Team A; repo2's by Team A plus Carol. A repo2 session's
audience includes Carol, who cannot read repo1, so the session may not read repo1
and recall leaves repo1 out. The rule is unchanged; **(decision)** Vaelius explains
it instead of only refusing:

- **Missing readers.** For each workspace the person can read that does not contain
  the session audience (at most 64), the descriptor's `narrower` entry carries
  `missing_readers`: `count`, the people of the session audience who are not
  readers of it, and `names`, at most three labels with `more` for the people they
  do not cover. **Name-disclosure rule:** labels come only from the access list of
  the session's own workspace, which every reader of it already sees in `workspace
  show`: a principal entry by id, else the group entry (by group name) that brings
  the person in. People reached only through the organization entry are counted,
  never named. Only people in the session audience who are *not* readers of the
  target are considered, so nothing about the target's own members is revealed
  beyond "these people from your audience are not in it" (mutation-tested in
  `test_workspace_access`).
- **The denial** names them and both fixes in one line (above). It stays a plain
  deny, never `ask`, and uses only the cached descriptor (no network call per read).
- **Recall.** `hidden_items` counts the workspace's active, non-private sources
  (a partial index, migration 052). The first prompt of a session whose descriptor
  lists any shows a status line for the
  person only (`systemMessage`, never `additionalContext`), once per session:
  `vaelius: 12 memories from repo1 hidden (not visible to carol); 'vaelius-client
  session use repo1' to include them`.
- **Access-list changes.** Adding or removing an entry and adding or removing a
  local group member return `newly_unreadable` (workspace names) and
  `newly_unreadable_note` when the change ends `audience(T) ⊇ audience(S)` for a
  pair of workspaces the caller can read, so sessions of S can no longer read T.
  Checked when the caller reads at most 200 workspaces (otherwise the note says it
  was not checked). SCIM group sync logs the same finding on the server.
- **The agent.** The MCP tool `use_workspace` says the agent may offer it after
  such a denial and must never call it unless the person asks or agrees.

The ways forward: add the missing people as readers of repo1; `session use repo1`
for one session (capture is then limited to people in both); or keep the work
separate and start a session in repo1.

A folder with no scope file at all is denied for a different reason: Vaelius cannot
tell who may read it. Add a scope file that names a workspace, or run `session use
private` for a session whose capture stays with the person.

### On the wire

Capture events keep the #32 `origin_label` contract, re-read in workspace terms:
`projects` holds the carried workspace ids (narrowed ones included) and
`ceiling: "private"` means `origin_private`. New clients never send `ceiling:
"team"`; the server keeps accepting it from old clients (section 9). Events add
`workspace` (alias of `project`) and `remote` (section 4).

### Remembered items and preferences

- **Remembered items** (`agenthub.remember`, migration 051) are what a person asks
  Vaelius to keep: an owner-private source holding their own words and one
  owner-reviewed note over it, the same item `submit_memory_candidate` installs.
  Writing needs `ingest` and the writer role in the workspace that stores it (the
  person's explicit command is the review, so not `curate`); a new version is a
  lifecycle correction and removal a lifecycle withdrawal of the item's source,
  applied for the item's owner only. The item's audience is its source's: private
  until the owner shares it with the ordinary owner controls. Nothing here is a new
  permission path.
- **Always load.** An item marked so is delivered at session start and with recall
  to every session whose audience admits it (`_document_allowed` under the session
  label): an owner-private one only to the owner's private sessions, a shared one to
  sessions of its workspace's readers. `personal` items are considered in every
  workspace of their owner, not only the one that stores them.
- **Preferences** (`cloud_preferences`, the observer's extracted defaults) are the
  owner's private memory too: since migration 051 they reach only private sessions
  and unlabelled requests, like other private items. Organization-required
  preferences are not private and are always delivered.
- **Echoes.** Delivered preference and remembered-item cards, and MCP preference
  results, carry `"sharing": "private"` (or the item's level); the client's taint
  carries them like any recalled item, so restating one in a session limits that
  session's capture to the people who could read it.

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

**Moderation follows the admin role (decision, resolving open question 1).** A
workspace `admin` moderates with a credential holding `manage`; `moderate` is no
longer a separately granted action for workspace purposes. Credentials that
already carry `moderate` keep working (an admin holding `moderate` but not
`manage` can still moderate), and `moderate` never becomes a default.

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
| `workspace_access(tenant, principal, workspace, principal_role, group_role)` | Effective role from principal entries and from group entries (never the org entry), maintained with the closure; kept apart so `widest_entry` applies at query time. Primary key `(tenant, principal, workspace)` | Σ non-org audience sizes (for example 1,000 workspaces × 50 people = 50,000 rows) |
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
list. Superset lists are computed per request with one set-difference statement
over `workspace_access` and `group_closure` (counting, per candidate workspace, the
session audience members it covers); **(decision, PR B)** they are not cached, in
keeping with "nothing about access is cached between requests" (section 2).

`internal_project` (the `enterprise:` scope digest) stays as a coarse partition
key for the knowledge pipeline, derived from `R`; it is never the authorization
decision.

## 10. Access reviews, offboarding and lifecycle

### What changes

- **The access report** lists, per principal, every workspace they can read with
  their effective role and **how**: a direct entry, which group (with its source and
  sync state), or the organization entry. New line kinds: `workspace_entry` (subject,
  role, granted by, created), `group_membership` (local only; revocable here),
  `synced_group_membership` (reported, revoked at the source),
  `repo_link`. `project_grant` lines become `workspace_access` lines.
- **New flags:** `admin_role` (privileged), `org_wide_entry`, `group_stale`
  (access derived from a group that is not `current`).
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

Migration `048_workspaces.sql` runs in one transaction. **(decision, by the owner)**
There was no production data to preserve when PR A shipped, so the migration is
deliberately conservative and cheap instead of mapping every sharing level: it
keeps every id and every item, and makes everything private until owners re-share
it or administrators add entries. By construction no reader set can grow, so the
per-policy reader-set comparison, the abort-if-wider guard and
`vaelius migrate-access --dry-run` described in earlier drafts are not needed.

### Mapping

| Today | After |
| --- | --- |
| Project | Workspace with the **same id**; `name` = id |
| Explicit members of project P | Principal members of a `local` group `P-members` that belongs to workspace P (managed by P's admins) |
| Directory group mapped to P (`cloud_directory_group_projects`) | A `scim` group with the same external id (id `scim-` + 24 hex digits of its SHA-256), nested in `P-members`; a group awaiting reconcile is `stale` and contributes nobody |
| The members group | One entry `group:P-members → writer` |
| Project ceiling `organization` | **No** organization entry; administrators add one if they want it |
| Project ceiling (or tenant default) `private` | `item_default = private` |
| Tenant ceiling `organization` / `team` / `private` | `widest_entry = organization` / `people_and_groups` / `none` |
| Person `max_sharing` | `private` → `contributions_default = private`; otherwise `shared` |
| Who administers each workspace | Nobody gets an `admin` entry. Tenant administrators manage migrated workspaces (break-glass, audited) until they add admins |
| Every existing item | `private = 1` (owner only) |
| Every existing raw grant or share | Dropped |
| Credentials and refresh sessions holding `share` | Also hold `manage` (`legacy_actions` bit 4) |
| Derived documents | All capped by the audience of every source (`level_capped = 1`) |

Origin columns are never touched (the 046 trigger still guards them). Policy
definitions change (`private` replaces `sharing` and `visibility`), so the
migration rebuilds the `search_policies` projection. SQLite snapshot imports
(`agenthub.cloud_migration`) map memberships and items the same way.

### Kept, changed, dropped

| Object | Fate |
| --- | --- |
| `enterprise_projects` | **Kept** as the workspace table; adds `name`, `created_by`, `created`, `org_role`, `item_default`, `epoch` |
| `enterprise_memberships` | **Dropped**, replaced by `workspace_access`. A read-only view of the same name stays for one release for operator scripts |
| `cloud_explicit_memberships`, `cloud_directory_memberships`, `cloud_directory_group_projects` | **Dropped**: rows became group edges |
| `cloud_directory_users`, `cloud_directory_resources`, `cloud_directory_events` | **Kept**: the SCIM adapter's state |
| `max_sharing` on principals, projects and organizations, `default_max_sharing` | **Dropped** from every check; columns kept frozen for one release |
| `enterprise_sources.sharing`, `sharing_requested`, `originals_sharing`, `originals_requested` | **Frozen** (history only); `private` is added |
| `enterprise_sources.origin_*` | **Kept**, unchanged, still fixed by trigger |
| `raw_source_grants` | **Changed**: `grantee_kind` is `principal` or `group`; `project` is refused |
| `enterprise_sharing_changes` | **Kept**; levels extended with `shared` |
| `search_passage_shared`, `search_origin_reaches` | **Dropped**, replaced by `search_reader_in_audience` (with `search_workspace_role`) |
| `search_document_allowed`, `search_passage_granted` | **Changed** |
| `search_permissions.SHARE`, `LEVEL`, `ORIGIN_*`, `CEILINGS` | **Replaced** by one `AUDIENCE` clause |
| `ProjectSharingMixin` (`project_sharing.py`) | **Replaced** by `workspace_access.py` |
| `vaelius sharing-policy` | **Replaced** by the operator commands `workspace` and `tenant-policy` |
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
2. Migration: a pre-048 schema with representative rows migrates to same-id
   workspaces whose members are writers through `P-members` (directory groups
   nested), with no organization or admin entry, every item owner-only and every
   grant dropped (section 11).
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

**Implemented** (migration 049). Client: session label at SessionStart; descriptor fetch, cache, refresh and expiry;
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
   status line. (Superseded by migration 053: it is denied unless the session is
   already private.)
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

### PR C: GitHub App team sync and device-flow identity link (removed)

Shipped as migration 050 and removed by migration 054 (section 3). Its tests were
removed with it; `test_access_migration` checks that a former `github` group grants
nothing after the migration.

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

## 14. Open questions (resolved)

1. **Should `moderate` follow the role alone?** Resolved: yes. Workspace admins
   moderate (with `manage`); `moderate` is no longer a separate grant for workspace
   purposes, and credentials that already carry it keep working (section 8).
2. **First capture from an unlinked repository.** Resolved: an admin's first capture
   from a repository linked nowhere links it automatically, audited, and the status
   line says so; a non-admin's capture is still rejected (section 4).
3. **`max_staleness` default.** Resolved for PR C's GitHub sync, which migration 054
   removed; SCIM groups go `stale` only while an out-of-order event awaits reconcile.

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
| Unscoped, unknown paths | Deny with one line unless the session is private (migration 053; earlier: allow and make the session private) | 7 |
| Ownership after demotion | Owner rights survive; widening needs `writer` | 8 |
| New action | `manage`, default, legacy bit 4 from `share` | 8 |
| Admins after migration | None; tenant administrators manage migrated workspaces | 11 |
| Migrated membership | One `P-members` local group per workspace, nesting the mapped `scim` groups | 11 |
| Migrated items and grants (PR A, by the owner) | Every existing item private, every existing grant dropped, no organization entries, no reader-set guard | 11 |
| Repository link key (PR A) | `(tenant, remote)` | 4 |
| First capture from an unlinked repository (PR A) | An admin's capture links it, audited, with a status-line notice | 4 |
| Moderation (PR A) | Follows the admin role with `manage`; older `moderate` credentials keep working | 8 |
| Shares and derived documents (PR A) | Shares cover passages and originals, never derived documents | 6 |
| Group admins (PR A) | A local group's admins are also its members; the creator is the first admin | 3 |
| Partitions (PR A) | `internal_project` partitions narrow retrieval only and never decide access; a delegated credential searches only its delegated workspaces | 9 |
| Legacy `team` request in a workspace with an organization entry (PR A) | Private, decided on the stored entry even while `widest_entry` suspends it | 5 |
| Descriptor route (PR B) | `POST /enterprise/v3/workspaces/descriptor` with the session label, not a per-workspace `GET` | 5 |
| Descriptor epoch (PR B) | Sum of the tenant's workspace epochs; labelled search responses carry it as `session_epoch` | 5 |
| Session audience privacy (PR B) | Decided by the service: label `private`, a private sharing request, `contributions_default`, `item_default`, or an audience of the person alone | 7 |
| Which session `use` narrows (PR B) | `--session`, else `CLAUDE_CODE_SESSION_ID`, else the session active most recently in this scoped folder | 7 |
| MCP recall label (PR B) | The connection folder's label in `X-Vaelius-Session-Label`; never wider than the session's own | 7 |
| MCP recall label (migration 051) | Claude Code gives the header helper no session ID, so the folder's label is narrowed by the label of the session last active there (last writer wins, 12 h, removed at its SessionEnd); never wider than the folder's | 7 |
| Preferences and remembered items (migration 051) | Private ones reach only private sessions; writing a remembered item needs `ingest`, not `curate` | 7 |
| Shares in a labelled recall (PR B) | Qualify when the grantee group's members contain the session audience; own private items only in a private session | 7 |
| `scope = "all"` (PR B) | Folded into the label: labelled recall reaches the superset list; removed from the MCP tool | 7 |
| Superset caching (PR B) | Computed per request, never cached | 9 |
| GitHub team sync and identity link (PR C, migration 050) | Removed by migration 054: `github` groups, their entries and shares deleted, tables dropped, `gh-`/`gh_` names stay reserved; the repository-read check is dropped | 3, 4 |
| Workspace visibility (migration 053, by the owner) | Chosen at creation, default org (everyone signed in to the tenant; capped by `widest_entry`), `--private` to restrict; changed only on the server by admins; never derived from GitHub; a scope-file `visibility` (or `read`) is ignored | 2 |
| Recall of a read-only chat (by the owner) | Treated as a private session: the person's full readable set, since it writes nothing | 7 |
| Read-only chats (migration 053) | One-way per chat; the service refuses every write for the header, a `read_only` label or a registered session id | 7 |
