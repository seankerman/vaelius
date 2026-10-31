# Privacy model

This is the single description of who can read what in a Vaelius service, written
for security reviewers. It states the rules the service enforces, who controls each
of them, when they are evaluated and what they do not cover. Details and commands
live in the linked documents. The design behind the rules is in
[the access model](ACCESS_MODEL.md); where this page describes shape (what
exists and how it fits together) that document describes the full specification.

## Summary

- Every request is made by an authenticated **principal** in one **tenant**.
  Nothing crosses tenants.
- Captured content belongs to a **workspace**. A workspace has an **access
  list**: entries of a subject (a person, a group or the whole organization) and
  a **role** (`reader` < `writer` < `admin`).
- An item is either **private** (its owner only) or shared with the workspace
  **audience** (everyone the access list reaches as a reader). Its owner may also
  share it to a named principal or group. The per-item `private` / `team` /
  `organization` ladder is gone.
- Groups come in three sources: `github` (teams synced from a GitHub App
  installation, PR C), `scim` (identity-provider groups) and `local` (managed in
  Vaelius). Membership is **materialized** as a transitive principal set; no check
  walks the graph.
- A **repository** links to one workspace by normalized remote. A capture whose
  remote is linked elsewhere is refused; one with no remote is accepted on the
  person's writer role alone.
- One predicate, *reader ∈ item audience*, is implemented in Python, in the
  indexed SQL search filter and in row-level security, with parity tests.
- A captured agent session has a **fixed label**: the audience of the workspace it
  starts in. It may read and recall only content whose audience contains that
  audience (no read up). The guardrail hook denies other workspaces' folders; the
  service filters recall in the same three layers. The person can narrow a session
  explicitly ("this workspace and B"); captures then carry B.
- Owned content is never deleted by a role change or by offboarding. Withdrawal,
  deletion, legal hold and subject erasure continue to apply.

## Identity

A principal is a person (or, for delegation, a service acting for a person)
registered in a tenant. Principals authenticate with credentials issued after an
identity-provider login (OAuth 2.0 with PKCE and token introspection) or an
operator enrollment. A credential carries **actions**, such as `read`, `ingest`,
`source_read`, `correct`, `withdraw`, `share`, `manage` and `policy`; every route
needs a specific action, and a route that is not classified is refused. Identity,
credential state and delegation are checked again at the final delivery
checkpoint, so a revoked credential or a deactivated principal stops receiving
content immediately.

A **delegation** lets one principal act for another with a subset of that person's
actions, and only inside a listed set of **delegated workspaces**. Delegated
credentials never carry the `manage` action and never read anything the
represented person reaches through the organization entry.

See [enterprise identity and authorization](ENTERPRISE_AUTH.md).

## Workspaces, groups and roles

- A **workspace** is a body of shared memory with an access list. It keeps the
  id of the project it replaced, so stored ids never change.
- An **access-list entry** names a subject (`principal`, `group` or
  `organization`) and a role. A principal's role in a workspace is the highest
  role any entry gives them (a direct entry, an entry for any group they belong
  to transitively, or the organization entry). Roles never subtract: there are
  no deny entries.
- A **group** has one of three sources — `github`, `scim` or `local` — and an
  activity and `sync_state` (`current`, `stale`, `failed`). A `scim` group
  awaiting reconcile is `stale`. A stale or failed synced group contributes
  **no members** until the next good sync, so a sync outage can only take access
  away. SCIM group ids are `scim-` + the first 24 hex of a SHA-256 of the
  external id.
- A **local group's creator** is its first admin and a member. A `local` group
  that belongs to a workspace (`groups.workspace`) is managed by that workspace's
  admins; otherwise by its own admins.

### Roles and effective actions

| Role | Can do, in that workspace |
| --- | --- |
| `reader` | Read items whose audience is the workspace; give feedback |
| `writer` | Also capture and contribute (ingest, correct, curate), withdraw and share their **own** items |
| `admin` | Also manage the access list, repository links and workspace settings, and moderate |

A credential's effective actions in a workspace are its credential actions **∩**
the actions the role implies:

| Role | Implied actions |
| --- | --- |
| `reader` | `read`, `feedback`, `source_read` |
| `writer` | reader's, plus `ingest`, `correct`, `curate`, `withdraw`, `share` |
| `admin` | writer's, plus `moderate`, `manage` |

- **Ownership survives role changes.** An owner can always find, read, withdraw,
  delete and make private their own items. Only widening (clearing `private`,
  granting a share) needs the `writer` role, a non-delegated credential, the
  `share` action and `owner_sharing = allowed`.
- **Shares are not role-gated for the reader.** A grantee reads a shared item
  with the credential action `read` alone.
- A delegated credential has a role only in its delegated workspaces and never
  through the organization entry.

### Tenant policy

Three tenant settings sit on `enterprise_organizations`:

| Setting | Values | Default | Meaning |
| --- | --- | --- | --- |
| `workspace_creation` | `anyone`, `admins` | `anyone` | Who may create a new workspace. `admins` restricts it to principals with the tenant `settings_admin` flag |
| `widest_entry` | `none`, `people_and_groups`, `organization` | `organization` | The widest entry an access list may use. `people_and_groups` suspends every organization entry; `none` keeps only `principal` entries with role `admin` (every workspace is private to its admins) |
| `owner_sharing` | `allowed`, `lower_only` | `allowed` | `lower_only` keeps owners' lowering, deletion and revocation but refuses raising and granting |

Lowering `widest_entry` **does not delete** entries; it suspends every entry
wider than the setting at query time, so lowering hides at once and raising
restores. Creating an entry wider than `widest_entry` is refused
(`entry_wider_than_tenant_allows`). A settings administrator sets these on
`POST /enterprise/v3/tenant-policy`, audited.

### Last-admin rule and the settings-admin override

A workspace that has an admin entry whose subject is a principal, or a `local`
group with an active member, keeps at least one: a change that removes the last
counting admin is refused with `last_admin`. Synced groups (`github`, `scim`)
never count, so a sync failure never orphans a workspace.

Workspaces without any such admin — including every workspace produced by the
migration (section 11) — are managed by tenant settings administrators (the
`settings_admin` flag and the `settings` action) as a **break-glass override**,
audited as `workspace_admin_override`. The override never gives content access;
adding themselves as a reader is an ordinary, audited entry change.

## What is stored

| Content | What it is | Read with |
| --- | --- | --- |
| Conversation passages | Exact spans of a captured source after deterministic credential redaction: search results, cited evidence, surrounding conversation context | `read` |
| Curated and derived documents | Reviewed notes, optional model-curated knowledge, native document sections | `read` |
| Retained originals | The complete canonical source and uploaded original documents, unaltered by passage indexing | `read` and `source_read` (and ownership or an originals share) |

Redaction is pattern based and not universal; see
[data and security boundaries](DATA_BOUNDARIES.md).

## Item visibility

### Item privacy and the workspace audience

Every captured item belongs to one workspace. It is either `private` (its owner
only) or shared with the workspace **audience** — every principal that holds at
least `reader` through any entry of the access list. There are no other
per-item levels.

A new item is **private** when any of these is true at capture:

- no sharing request on the event (older clients' default);
- a legacy `team` request against a workspace that has an organization entry;
- a private capture connection;
- the person's `contributions_default` is `private`;
- the workspace's `item_default` is `private` (used by workspaces whose project
  ceiling before migration 048 was `private`).

A request of `organization` (what new clients send for a scope file that names
`workspace =`) or `team` elsewhere means "inherit the workspace audience". The
service never promotes it past the access list.

### Why an item is private

- **Owner opt-out** (`enterprise_sources.private`): the author set it, now or at
  capture. Clearing it is a widening and needs `share`, the `writer` role, a
  non-delegated credential and `owner_sharing = allowed`.
- **`origin_private`** (section "origin and carried workspaces"): the capture
  touched content the workspace must not store. No share can widen it, by
  design: it protects someone else's content.
- The `item_default` or `contributions_default` reasons above.

### Explicit shares

A share names one principal or one group, for a source, a capture session
(including later captures in it) or a connection; passages, and optionally
originals. Shares are governed by `owner_sharing` and apply to the redacted
passage tier (and, with `originals = 1`, to originals). A share **never**
reaches a derived document: derived documents need the inherited audience of
every source they depend on, and shares did not cover derived documents before
PR A either.

### Carried workspaces

An item's `origin_projects` is the set of **carried workspaces**: every other
workspace whose content the session that produced the item had read. A reader
other than the owner must be in each of their audiences too, so widening one
workspace cannot expose another's content. At most 16; a session that would
carry more becomes private.

### Legacy origin ceiling of `team`

Items stored before migration 048 with an origin ceiling of `team` ("members of
the project and of every carried project") keep their exact meaning: a non-owner
reader needs a **direct** (non-organization) role in the workspace and in every
carried workspace. New captures never produce it.

### Originals

Originals are a separate tier. A non-owner reads one only through an **originals
share** that includes it, and in both cases with the credential action
`source_read`. There is no originals level any more; sharing passages never
shares originals.

### Derived and curated documents

A derived document's audience is the intersection of the audiences of every
source it depends on. Every derived document is capped this way
(`level_capped = 1` for all), so a summary of a `private` or `origin_private`
source stays as narrow as that source.

### Remembered items and preferences

A remembered item (`vaelius-client remember`, the MCP tool `remember`, an
`import-memory` entry) is an owner-private source with the person's own words and
one owner-reviewed note over it; `remembered_items` (migration 051) holds only its
metadata (owner, workspace, `personal`, `always_load`, versions, withdrawal). Its
audience is its source's: private until the owner shares it with the owner controls
above. "Always load" items are delivered at session start and with recall only to
sessions whose audience admits them. Preferences extracted by the observer are
owner-private in the same way. Both are carried as private by the client's session
taint when delivered, so a preference restated in a shared session does not become
readable by that session's audience. Withdrawal, deletion, legal holds and erasure
apply to the source as to any other.

### Reader rules that stay

Tenant isolation, active principal, withdrawal and deletion, legal holds,
operator quarantine (held content stays with its author; quarantined readers
lose the organization entry and share paths), delegation limits, connection
reader lists and freshness, source ACL freshness and moderation all apply on
top, unchanged. Reviewed releases (operator `policy` action,
`backend_releases`) stay an explicit operator path, unchanged.

## Repository linking

- A workspace lists linked repositories by **normalized remote** (`host/path`,
  lower case, no scheme/user/port, no trailing `.git` or `/`). Paths may have
  more than two segments. See
  [the repository-linking section](ACCESS_MODEL.md#4-repository-linking) for
  the normalization table.
- **One repository links to one workspace per tenant** (`workspace_repos`
  primary key `(tenant, remote)`, so two tenants cannot block or probe each
  other's remotes).
- The client sends the remote with every capture. A capture whose remote is not
  linked to the named workspace is refused with `409 repo_not_linked`; the
  client keeps the event queued, and the status line says which admin fix
  applies.
- An admin's first capture from a remote that is not linked anywhere **links it
  automatically** (role `admin`, credential `manage`, non-delegated), audited
  as `repo_link` / `automatic`, and the parts receipt carries a one-line
  `notice`. Linking and unlinking otherwise need `manage` and the admin role
  (routes `/enterprise/v3/workspaces/repos/link` and `/unlink`).
- A capture with no remote (a scoped folder that is not a Git repository) is
  accepted on the person's writer role alone.

## The predicate, in three layers

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
             — entries wider than widest_entry are ignored
```

A derived document needs `can_read` for every source it depends on; an original
also needs `source_read` and ownership or an originals share.

It is implemented three times and a parity test keeps them in agreement:

1. the **Python oracle** in `agenthub.workspace_access`
   (`WorkspaceAccessMixin._in_audience`, `_shared_with`), used by
   `EnterpriseStore._document_allowed`, `_visible_source` and
   `agenthub.raw_access`;
2. the **indexed SQL filter** in `agenthub.search_permissions.AUDIENCE`, with
   the reader's workspaces `R` and delegated set `D` computed once per request;
3. **row-level security** functions `search_reader_in_audience` and
   `search_document_allowed`, applied by the restricted search-reader role to
   every ranking query. A denied row never leaves the database.

Shares (`raw_source_grants`, grantee `principal` or `group` resolved through
`group_closure`) apply to passages and (with `originals = 1`) to originals; a
share never reaches a derived document.

## Project-scoped retrieval

`_readable_scopes` partitions the knowledge pipeline into: the reader's own
private partition, the shared partition of every active workspace they are in,
and the organization-visibility partition, for every credential (a delegated
credential only for its delegated workspaces). These partitions **narrow
retrieval**; they never decide access. The one predicate above is the final
check.

## Session labels and recall

Workspace **visibility** is chosen on the server when a workspace is created
(`private`: its admins and whoever they add; `org`: also an organization reader
entry, capped by `widest_entry`) and changed only by its admins on the server; it is
never set by a scope file or derived from GitHub.

A captured Claude Code session takes a fixed label when it starts: the workspace of
its starting folder, narrowed explicitly by the person (`vaelius-client session use
<workspace>` or the MCP tool `use_workspace`) and, when it becomes private (the
person's `contributions_default`, the workspace's `item_default`, a private
request, `use private`, or the backstop below), only the person. Sessions that are
read-only, off or outside every workspace have no label and capture nothing.

**Superset list.** For a label the service computes, on current materialized
membership, the set `S` of workspaces the reader can read whose audience contains
the session audience (`audience(A) ∩ audience(narrowed…)`), plus the groups whose
members contain it. It is extensional: an organization entry contains every session
audience except quarantined members, who must be direct members. Nothing is cached
between requests.

**Recall filter.** A recall request (prompt injection, `search_memory`, evidence,
context, timeline and history routes) that carries a label returns only items
whose audience contains the session audience:

```
session_admits(i) = private session
                  ∨ ( ¬i.origin_private
                    ∧ ( (¬i.private ∧ i.workspace ∈ S) ∨ shared_to_subject_containing(session audience) )
                    ∧ i.carried ⊆ S
                    ∧ (i.origin_ceiling ≠ 'team' ∨ (i.workspace, i.carried ⊆ S_direct)) )
```

It is the fourth term of the same predicate in all three layers
(`WorkspaceAccessMixin._session_admits`, `search_permissions.SESSION`, and
`search_session_admits` called from `search_document_allowed`, migration 049), with
parity tests. In a shared session the person's own private items are not recalled;
a private session recalls everything the person can read. The person's private
preferences and private "always load" remembered items follow the same rule: only
a private session receives them (organization-required preferences always arrive).
Requests without a label (older clients) keep today's behavior. The label
replaces the cross-project `scope = "all"` option: recall reaches every workspace in
`S`, the current one first, ranked only from the label's `read` list (the scope
file's `read`, default the current workspace, plus narrowed workspaces). `read`
narrows retrieval and is never an authorization input, so it cannot widen what is
returned; the MCP `search_memory` tool's `scope` broadens it for one search.

**Read rule.** The client caches an advisory descriptor (the service's `S` for the
session) in its profile and the PreToolUse guardrail hook denies reads of scoped
folders whose workspace is not in it, without a network call. The descriptor is
advisory: captured items carry every other workspace the session read, so
confidentiality of a capture does not depend on the hook being right. Since
migration 053 it also denies existing paths outside every scoped folder, unless the
session is already private, instead of letting them make the session private.

**Read-only chats.** A chat switched to read-only (`vaelius-client session
read-only`, or the MCP tool `read_only_session` when the person asks; one-way) and
a chat in a `capture = "read-only"` folder write nothing: the service refuses every
write route and tool for a connection that says read-only and for a session id
registered as read-only (`read_only_sessions`, migration 053, digests of tenant,
principal and session id). A registration only narrows; there is no route that
removes one.

## Provenance labels

Every delivered item carries `provenance` (author handle, workspace, capture
time, source type, original or derived wording) and an `origin` label on the
wire ladder, kept for the client:

- `private` — any depended-on source is private, or `origin_private`;
- `organization` — every depended-on source's audience is the whole
  organization;
- `team` — otherwise.

Provenance is read after authorization and never widens delivery.

## Owner controls

Every person manages the content they created (`enterprise_sources.owner`:
the author, or the represented person for delegated capture) themselves, from
the client (`vaelius-client mine | forget | sharing | share | unshare`, see
[the client guide](CLIENT.md)), the agent (MCP tools) or HTTP
(`/enterprise/v3/my-data/*`, see
[route actions](ENTERPRISE_AUTH.md#actions-and-route-authorization)). Nothing
here acts on anyone else's content.

| Control | Needs | Rule |
| --- | --- | --- |
| Find | `read` | The caller's sources or sessions, filterable by workspace, session, capture time and privacy. Each source reports its workspace, whether it is private, and why |
| Delete | `withdraw` | One or more sources, a session, a workspace or a date range, through the single purge and deletion journal ([data lifecycle](DATA_LIFECYCLE.md)). A legal hold wins: held sources are hidden and preserved, and reported as held |
| Set privacy | `withdraw` or `share` | `/enterprise/v3/my-data/sharing` takes `shared` or `private` (legacy `team` / `organization` mean `shared`). Clearing private needs `share`, the writer role, a non-delegated credential, `owner_sharing = allowed` and an item that is not `origin_private` |
| Grant | `share` (or operator `policy`), non-delegated, tenant allows owner sharing | Names a `principal` or a `group` for a source, session or connection; passages and optionally originals. `project` grantees are retired (`project_grantee_retired_use_group`) |
| Revoke, list grants | `share`, `withdraw` or `policy`; listing needs `read` | Takes effect on the next request |

Bulk, destructive and widening operations are two-step: the first call is a
preview with a `confirm_token` bound to the exact selection, the second applies
it. MCP tools require the `confirm` argument (`false` or the token), and they
are served by the Vaelius MCP server, so client capture never records them.

Every change is recorded: privacy and share changes in
`enterprise_sharing_changes` and the audit log (`source_sharing`,
`source_originals_sharing`); deletions as `owner_delete` audit rows and journal
entries; grants in `raw_source_grants` (issuer, scope, grantee kind and id,
tiers, created, revoked, revoked by) and `owner_grant` audit rows.

### Moderation follows the admin role

A principal with the `admin` role in a workspace and a credential holding
`manage` can withdraw (hide) anyone's sources in that workspace, together with
everything derived from them
(`POST /enterprise/v3/moderation/withdraw`, preview and confirm as above, with
a short reason). Credentials issued before PR A that hold the older `moderate`
action keep working.

Moderation never deletes and never reads originals; the author still sees the
source as hidden by a moderator (who, when, why) and can still delete it. Each
withdrawal is journaled and audited (`moderate_withdraw`). `manage` is never
part of a delegation and is reported as privileged in access reviews.

## Retention, erasure and legal hold

Withdrawal blocks a source and everything derived from it; deletion purges
source, derived content, vectors and retained objects. Scheduled retention,
legal holds and subject erasure use the same purge, and a legal hold turns a
deletion into a withdrawal until it is released. Access-list changes never
extend retention. See [data lifecycle](DATA_LIFECYCLE.md).

## Access reviews

The content-free access report lists, per principal, every workspace they can
read with their effective role and how (an entry, which group with its source
and sync state, or the organization entry), credentials and actions (calling
out `source_read`, `manage`, `moderate`, `policy`), connection reader grants,
reviewed-release grants, local-group memberships, repository links and the
person's own `contributions_default`. Reviewers approve or revoke each
entitlement; revocations take effect immediately. See
[access reviews](ACCESS_REVIEW.md).

## Limits

- The service operator is trusted. Database administrators can read stored
  content regardless of application policy.
- Access lists govern reads through Vaelius. They do not recall copies a reader
  already made, or text an agent produced from delivered content outside a
  captured session. Inside a captured session, origin labels and carried
  workspaces carry a delivered item's restriction into what the agent captures
  next.
- Origin labels are a deterministic best effort from what a tool call names;
  they are not a sandbox (see the limits in
  [data boundaries](DATA_BOUNDARIES.md#known-limits)).
- The session read rule covers file tools and the paths shell commands name.
  Programs that open files without naming them are covered only by the backstop
  (the session becomes private), and file edits into other folders are not policed
  by the label beyond the personal-folder and credential rules.
- A read-only switch reaches an MCP connection that was already open through the
  connection ids the client registers; a connection opened by another chat in the
  same folder can be refused writes too until it reconnects.
- Redaction reduces exposure of credentials; it is not anonymization.

## Upgrading to migration 048

Migration 048 (PR A) keeps every id and changes nothing a reader could already
read. By construction it widens nothing, so there is no `migrate-access
--dry-run` or per-policy reader-set guard.

- **Every existing item becomes private to its owner**
  (`enterprise_sources.private = 1`). Owners re-share them, or an admin adds an
  entry that reaches the readers again. Every existing raw grant and share is
  dropped at the same time; this is why the invariant holds.
- **Members become writers.** Each project's explicit members and the SCIM
  groups mapped to it become a local group `<project>-members`, with each
  mapped SCIM group nested inside it. The workspace gets one access-list entry:
  `group:<project>-members → writer`. No admin entry and no organization entry
  is created.
- **Tenant settings administrators manage migrated workspaces** until they add
  admins, through the audited settings-admin override. The report lists
  workspaces without an admin (`remaining.workspaces_without_admin`).
- **Settings map to their nearest equivalents.** Project ceiling (or tenant
  default) `private` becomes workspace `item_default = private`; tenant ceiling
  `organization` / `team` / `private` becomes `widest_entry = organization` /
  `people_and_groups` / `none`. The person's `max_sharing` of `private` becomes
  `contributions_default = private`; any other value becomes `shared`.
- **Legacy `share` becomes `manage` through a bit.** Credentials and refresh
  sessions issued before migration 048 that held `share` (directly, or through
  the pre-047 `withdraw`-implies-`share` legacy bit) also hold `manage`
  (`legacy_actions` bit 4). Stored action lists are not rewritten; renewal
  still reports the recorded actions.
- **Dropped tables.** `cloud_explicit_memberships`,
  `cloud_directory_memberships` and `cloud_directory_group_projects` are
  dropped. `enterprise_memberships` is replaced by a read-only view of the
  same name over `workspace_access` for one release, so operator scripts keep
  working.
- **SQLite snapshot imports** (`cloud_migration`) map memberships the same way.

### What this means at the first login after the upgrade

Every captured source is still there and still searchable **by its owner**.
Nobody else sees anyone's content until the owner re-shares it, an admin adds
an entry that reaches them, or the settings administrator does on the admin's
behalf. The operator CLI commands for admin set-up are in
[self-hosting](SELF_HOSTING.md); the client commands are in
[the client guide](CLIENT.md).

## Upgrading to migration 047 (historical)

Migration 047 was additive: curated and derived documents that already existed
kept the connection-visibility rule (`level_capped = 0`) until their author
changed a source they depend on, and new ones were capped. Migration 048 caps
every derived document (`level_capped = 1`), so this is now the only rule.

## Upgrading to migration 046 (historical)

Migration 046 added the origin columns, a trigger that keeps them fixed, and
the origin terms in the policy functions. Clients that send `origin_label`
need a service with this migration.

## Upgrading to migration 045 (historical)

Migration 045 introduced the per-item sharing ladder, the person and tenant
ceilings, the originals level and the project ceiling. Migration 048 replaces
the ladder with workspace audiences, so these ceilings are no longer the
authority — see "Upgrading to migration 048" above.
