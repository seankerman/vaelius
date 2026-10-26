# Privacy model

This is the single description of who can read what in a Vaelius service, written
for security reviewers. It states the rules the service enforces, who controls each
of them, when they are evaluated and what they do not cover. Details and commands
live in the linked documents.

## Summary

- Every request is made by an authenticated **principal** in one **tenant**.
  Nothing crosses tenants.
- Captured content has one sharing **level** on a single ladder:
  `private` < `team` < `organization`.
- A source's level is the **minimum** of the author's own setting, the
  repository's request, the operator's project and tenant ceilings, the capture
  connection's visibility and an origin ceiling. Any one of them can keep content
  private; none of them alone can share it.
- The level is stored when the source is captured, and every ceiling is applied
  again on **every** request. Lowering any ceiling hides content on the next
  request; raising one never exposes content stored at a lower level.
- Unredacted **originals** have their own level, private by default and never
  above the level of their redacted passages.
- Retrieval returns the **current project** first. Other projects' content arrives
  only when it is shared with the whole organization, as a separate, labelled tier,
  unless the caller explicitly asks for every project it may read.
- Every delivered item says where it came from (project, author, capture time) and
  carries a machine-readable origin label (project and current sharing level).
- Every person can find, delete, re-share and share what they created themselves,
  without an operator (see [owner controls](#owner-controls)). Curated and derived
  documents registered after migration 047 follow the same levels as passages.

## Identity

A principal is a person (or, for delegation, a service acting for a person)
registered in a tenant. Principals authenticate with credentials issued after an
identity-provider login (OAuth 2.0 with PKCE and token introspection) or an
operator enrollment. A credential carries **actions**, such as `read`, `ingest`,
`source_read`, `correct`, `withdraw` and `policy`; every route needs a specific
action, and a route that is not classified is refused. Identity, credential state
and delegation are checked again at the final delivery checkpoint, so a revoked
credential or a deactivated principal stops receiving content immediately.

A **delegation** lets one principal act for another with a subset of that person's
actions and only inside named projects. Delegated credentials never receive
organization-wide content (see below).

See [enterprise identity and authorization](ENTERPRISE_AUTH.md).

## Tenants, projects and membership

- A **tenant** is an organization. Every table and every policy check is tenant
  scoped.
- A **project** usually corresponds to a repository or a body of work. A principal
  is a **member** of a project through an explicit operator grant or a directory
  group mapped to the project. Membership can be revoked at any time; it is checked
  on every request, never cached.
- A **capture connection** is owned by one principal, belongs to one project and
  has a visibility (`private`, `team` or `organization`) chosen when it is enrolled.
  Its owner can narrow it and restrict it to listed readers; it cannot be widened.

## What is stored

| Content | What it is | Read with |
| --- | --- | --- |
| Conversation passages | Exact spans of a captured source after deterministic credential redaction: search results, cited evidence, surrounding conversation context | `read` |
| Curated and derived documents | Reviewed notes, optional model-curated knowledge, native document sections | `read` |
| Retained originals | The complete canonical source and uploaded original documents, unaltered by passage indexing | `read` and `source_read` |

Redaction is pattern based and not universal; see
[data and security boundaries](DATA_BOUNDARIES.md).

## The sharing ladder

| Level | Who can read, besides the author |
| --- | --- |
| `private` | nobody |
| `team` | active members of the source's project |
| `organization` | every active principal of the tenant, except principals under an operator quarantine; members of the project as for `team` |

"Organization" excludes deactivated principals (refused at authentication and
again by the policy itself), quarantined principals, credentials without `read`
(capture-only or backend credentials) and delegated credentials, which stay inside
their delegated projects and never receive the organization scope. No further
condition applies to `organization` beyond those of `team`: there is no waiting
period and no requirement on how the content was captured.

Every ordinary source rule applies on top of the level: tenant isolation,
withdrawal and deletion, legal holds, operator quarantine of the author (held
content stays with its author), delegation limits, connection reader lists and
connection freshness.

## Effective sharing: the minimum of every term

```
effective = min(person setting, scope-file request, project ceiling,
                tenant ceiling, connection visibility, origin ceiling,
                ceiling of every other project the content carries)
```

| Term | Set by | Where | Default |
| --- | --- | --- | --- |
| Person setting | the author, for all of their own content | `vaelius-client sharing --set LEVEL` (`POST /enterprise/v3/sharing`); stored as `enterprise_principals.max_sharing` | `private` |
| Scope-file request | whoever can edit the repository | `.vaelius`: `sharing` (passages) and `originals`; sent with each captured event ([client](CLIENT.md#repository-scope-files)) | `private` |
| Project ceiling | the operator | `vaelius sharing-policy --project P --max-sharing LEVEL`; a project without its own value inherits the tenant default | tenant default |
| Tenant ceiling | the operator | `vaelius sharing-policy --tenant-ceiling LEVEL`; caps every project and person | `organization` for new tenants |
| Connection visibility | the connection owner, at enrollment | connection enrollment; can only be narrowed | chosen at enrollment |
| Origin ceiling | what the captured content touched | the client's origin label, stored per source as `origin_ceiling` ([below](#origin-labels-and-session-taint)) | `organization` (no effect) when the event only touched its own project |
| Other projects' ceilings | each other project's operator | `origin_projects` on the source: the current ceiling of each project whose content it carries; an unknown or inactive one counts as `private` | none |

The tenant default for projects (`vaelius sharing-policy --max-sharing LEVEL`
without `--project`) is `organization` for tenants created after migration 045.

A scope file is a request, never a grant: anyone who can edit a repository can edit
its scope file, and the minimum means the file can only lower what the person and
the operator allow. The person's own setting defaults to `private`, so nobody's
content is shared until that person chooses to share it, whatever a repository or
an operator configures.

Changing one's own setting requires a credential of that person with the `ingest`
or `policy` action; a delegated credential can read the setting but never change
it. Every change is audited.

## When it is evaluated

**At capture**, the service computes the effective level from the terms at that
moment and stores it per source (`enterprise_sources.sharing` and
`originals_sharing`), next to what was requested (`sharing_requested`,
`originals_requested`). A request above the effective level is audited
(`sharing_request` / `capped_<level>`).

**At every request**, the stored level is capped again by the current person
setting, project ceiling (or tenant default), tenant ceiling, connection visibility,
origin ceiling and the current ceiling of every other project the source carries.
The result decides who may read it; at `team` the reader must be an active member of
the source project and of every one of those other projects.

| Change | Effect |
| --- | --- |
| Author lowers their setting | Their shared content narrows on the next request, including results already delivered (delivery is rechecked) |
| Operator lowers a project ceiling, the tenant default or the tenant ceiling | The same, for every source the ceiling covers |
| Connection owner narrows a connection | The same, permanently; passages are re-indexed |
| Membership revoked, principal deactivated or quarantined | That principal loses the content on the next request |
| Any ceiling raised | Only content stored at the higher level becomes visible again. Content captured while a term was lower stays at the level it was stored with |
| Author changes the stored level of their own sources | Lowering hides them on the next request. Raising stores the new level, never above the origin ceiling, and every current ceiling still applies ([owner controls](#owner-controls)) |

So raising a setting applies to what is captured from then on, and lowering one is
a revocation that takes effect immediately.

## Originals and passages

Sharing passages never shares originals. An original has its own level
(`originals` in the scope file, `private` by default), capped by the same terms and
additionally by the passage level: an unredacted original is never visible more
widely than its redacted passages. Reading an original also requires the
`source_read` action, which is not part of default enrollment. An owner can still
grant a specific source or connection's originals to a principal or project
explicitly ([raw-access policy](DATA_BOUNDARIES.md#raw-access-policy)).

## Curated and derived documents

A curated or derived document (a reviewed note, model-curated knowledge, a native
document section) registered after migration 047 is capped like a passage: a reader
needs, for **every** source it depends on, to be that source's author or to be
reached by that source's current level (the same minimum of every term, evaluated
on every request). So a person whose own setting is `private` never has derived
documents readable by others, whatever the capture connection's visibility, and a
document drawn from a `team` source and an `organization` source is `team`. Every
ordinary source rule, including the connection's visibility and the origin (below),
still applies on top: a curated document never reaches anyone a source it depends
on could not reach by its origin label, so a summary of a tainted passage stays as
narrow as the passage.

Documents registered before migration 047 keep the earlier rule (the capture
connection's visibility, with every source still visible to the reader, and the
origin), so upgrading hides nothing. When an author changes the stored level of a source
([owner controls](#owner-controls)), every document depending on it moves to the
capped rule; that can only narrow it.

A legacy capture (`POST /enterprise/v1/sources`) has no scope file: its visibility
is its sharing request. A native document upload or a document or record connector
item asks to share at its connection's visibility unless the upload names a lower
`sharing`. The author's own setting and every ceiling still cap all of them.

## Enforcement

One rule, three implementations that a regression test keeps in agreement:

1. the indexed SQL predicate (`agenthub.search_permissions`), used to authorize
   and re-validate results at delivery;
2. the PostgreSQL row-level security function `search_document_allowed` and its
   helper `search_passage_shared` (migration 045), applied by a restricted
   database role to every ranking query, so a denied row never leaves the database;
3. the Python oracle (`_visible_source`, `_document_allowed` and
   `agenthub.raw_access`) used for originals, evidence and explicit releases.

Grants and the cap on derived documents (migration 047) are enforced in the same
three places: `search_permissions.DOCUMENT_GRANTED`/`SOURCE_GRANTED` and the
derived-document clause of `predicate`, the row-level security functions
`search_document_allowed` and `search_passage_granted`, and
`EnterpriseStore._document_allowed` with `ProjectSharingMixin._passage_granted`;
`test_owner_controls.py` keeps them in agreement.

Ceilings are read from their tables inside each check; nothing about sharing is
cached between requests. The stored level of a passage's own source is also copied
to `enterprise_documents.raw_shared` (0, 1, 2) as a prefilter that lets other
people's private passages be skipped cheaply; the full check still decides.

## Project-scoped retrieval and the organization tier

Search, the MCP `search_memory` tool and automatic context injection take the
current project from the request (the client's project binding).

| Scope | Returns |
| --- | --- |
| `project` (default) | Content of the current project the reader may read, then, as a separate later tier, content from other projects whose current level is `organization` |
| `all` (explicit: MCP/HTTP `scope = "all"`) | Additionally, content from other projects the reader may read through membership or authorship |

Without a current project nothing is narrowed. The scope narrows retrieval only;
it never grants anything, and every item still passes the full permission check.
Results are delivered tier by tier, keeping relevance order within a tier. In the
lexical channel current-project matches fill the bounded candidate pool before any
other project's, so a large shared corpus elsewhere cannot crowd them out.

## Provenance and origin labels

Every delivered item carries `provenance` (author handle, project, capture time,
source type, original or derived wording) and `origin`, a machine-readable label:

```json
{"origin": {"project": "maple", "sharing": "team"}, "tier": "project"}
```

`origin.sharing` is the item's current level (the lowest over the sources it
depends on); `origin.project` is `null` when the sources span several projects, and
`origin.projects` (present only when there are any) lists every other project whose
content the item carries, from spanning sources or their own origin labels.
Against a current project each search item also has a `tier` (`project`,
`other_project` or `organization`), and any item from outside the current project
carries a human-readable line, for example
`"from": "birch · alice · 2026-10-01"`. Both appear in the HTTP response, the MCP
tool output and the injected context. Provenance is read after authorization and
never widens delivery. See [shared-memory governance](MEMORY_GOVERNANCE.md).

## Origin labels and session taint

A scope file decides capture for a whole session, but a tool call can read content
the repository's audience must not see: a document in the person's home folder, a
credentials file, a mail or chat MCP server, another project's repository, or
recalled memory with a narrower audience. The client labels every captured event
with what it touched and keeps the most restrictive label for the rest of the
session (session taint), because that content is now in the agent's context. The
label carries:

- an **origin ceiling**: `private` for personal folders, credentials, other volumes,
  repositories without a scope file, MCP servers other than Vaelius, non-public web
  addresses, private recalled memory and unknown tools; no restriction for the
  project itself, temporary and tool folders, read-only system paths, the public web
  and the scope file's allowlists;
- **other projects** whose content it carries: another repository with its own scope
  file, or recalled team memory from another project. Instead of making the session
  private, the event is tagged with that project; it is shared no wider than every
  tagged project allows, and at `team` only with people who are members of all of
  them;
- a **reason category** (`personal_content`, `mcp_server`, `other_project`, ...),
  never the matched path, command or server name.

The service validates the label, applies the ceiling and every tagged project's
ceiling as terms of the minimum at capture and again at every request, and stores
it per source (`origin_ceiling`, `origin_reason`, `origin_projects`). The origin also
caps **curated and derived documents**: a document built from a tainted passage
reaches a reader other than that passage's owner only as far as the passage's origin
allows, whatever its connection's visibility. This is the only sharing term that
applies to them; person, project and tenant ceilings still do not. The origin is
fixed: a database trigger refuses any change to it, and because the query-time
check reads the stored origin, no later change of a person setting, project or
tenant ceiling, or even of the stored level itself, can widen it. Recalled items
report the projects they carry in `origin.projects`, so a session that restates them
inherits the same restriction.

Only a new session (`/clear`, a new conversation) starts clean; compaction keeps the
label because its summary is written from the restricted content. The person sees
one line when a session becomes private or spans another project, for example
`vaelius: this session read personal files (~/Documents/...) — capture is now private`.
The full rules, the scope-file allowlists and the known limits (a program can read
anything without naming a path; commands such as `aws` or `gh` that name no outside
path are not restricted) are in
[origin labels and session taint](DATA_BOUNDARIES.md#origin-labels-and-session-taint).

## Owner controls

Every person manages the content they created (`enterprise_sources.owner`: the
author, or the represented person for delegated capture) themselves, from the
client (`vaelius-client mine | forget | sharing | share | unshare`, see
[client](CLIENT.md)), the agent (MCP tools) or HTTP (`/enterprise/v3/my-data/*`,
see [route actions](ENTERPRISE_AUTH.md#actions-and-route-authorization)). Nothing
here acts on anyone else's content.

| Control | Needs | Rule |
| --- | --- | --- |
| Find | `read` | The caller's sources or sessions, filterable by project, session, capture time and current level. Each source reports its current effective level and which terms of the minimum hold it there (`source`, `person`, `project`, `tenant`, `connection`, `origin`) |
| Delete | `withdraw` | One or more sources, a session, a project or a date range, through the service's single purge and deletion journal ([data lifecycle](DATA_LIFECYCLE.md)). A legal hold wins: held sources are hidden and preserved, and reported as held |
| Lower a stored level | `withdraw` or `share` | Applies on the next request |
| Raise a stored level | `share`, not delegated, tenant allows owner sharing | Stored at most at the origin ceiling; capped at every request by the person, project, tenant, connection and origin terms; the response says which term caps it |
| Grant | `share` (or the operator `policy` action), not delegated, tenant allows owner sharing | Names one principal or the active members of one project for a source, a capture session (including later captures in it) or a connection; passages, and optionally originals |
| Revoke, list grants | `share`, `withdraw` or `policy`; listing needs `read` | Takes effect on the next request |

Bulk, destructive and widening operations are two-step: the first call is a preview
with a `confirm_token` bound to the exact selection, the second applies it. MCP
tools require the `confirm` argument (`false` or the token), and they are served by
the Vaelius MCP server, so client capture never records them.

Every change is recorded: stored-level changes in `enterprise_sharing_changes`
(who, which source, passages or originals, from, to, what was asked for, which term
capped it) and the audit log (`source_sharing`, `source_originals_sharing`);
deletions as `owner_delete` audit rows and journal entries; grants in
`raw_source_grants` (issuer, scope, grantee, tiers, created, revoked, revoked by) and
`owner_grant` audit rows.

### Grants

A grant is an explicit list of readers rather than a level. It stands in for the
visibility and level checks of the sources it covers and for the reader's project
scope, so a granted passage reaches its reader in any project they search. Every
other rule still applies: tenant, withdrawal and deletion, delegation limits,
connection reader lists and freshness, the author's operator quarantine, and
membership of the named project at the moment of reading. A quarantined reader
receives nothing through a grant. Originals additionally need the reader's
`source_read` action, as before.

Grants are governed by the tenant's `owner_sharing` setting rather than by the
level ceilings: `allowed` (default) or `lower_only`, under which owners can still
lower, delete and revoke but cannot raise or grant, and grants that owners issued
stop applying at once. Grants issued with the operator `policy` action are not
affected.

**Origin.** A grant never carries content beyond its origin: a source whose origin
is `private` (for example a session that recalled another person's private memory)
is never readable through a grant, and a `team` origin only by readers who are
members of the source project and of every project the source carries. The
preview counts covered sources the origin keeps from the grantee.

There is deliberately **no override**, not even with a confirmation. The origin
ceiling protects someone other than the author: the people whose restricted
content was recalled into the session. The author cannot consent for them, and an
override would let anyone launder restricted content by recalling it and granting
it. An author who owns the original content can share that content itself.

### Project moderation

A project member whose credential has the `moderate` action can withdraw (hide)
anyone's sources in that project, together with everything derived from them
(`POST /enterprise/v3/moderation/withdraw`, preview and confirm as above, with a
short reason). Moderation never deletes and never reads originals; the author still
sees the source as hidden by a moderator (who, when, why) and can still delete it.
Each withdrawal is journaled and audited (`moderate_withdraw`). `moderate` is never
part of default enrollment or default provider scopes, cannot be delegated, and is
reported as privileged in access reviews. It is the hook for per-project roles: a
later change can grant it per project instead of per credential.

## Retention, erasure and legal hold

Withdrawal blocks a source and everything derived from it; deletion purges source,
derived content, vectors and retained objects. Scheduled retention, legal holds and
subject erasure use the same purge, and a legal hold turns a deletion into a
withdrawal until it is released. Sharing levels never extend retention. See
[data lifecycle](DATA_LIFECYCLE.md).

## Access reviews

The content-free access report lists, per principal, credentials and actions
(calling out `source_read`), project memberships and their origin, delegations,
connection reader grants, reviewed-release grants, last activity and the person's
own sharing setting (`max_sharing`). Reviewers approve or revoke each entitlement;
revocations take effect immediately. See [access reviews](ACCESS_REVIEW.md).
`vaelius sharing-policy` reports the ceilings, how many people chose each level
and, per project, how many sources are stored at each level and how many requests
were capped.

## Limits

- The service operator is trusted. Database administrators can read stored content
  regardless of application policy.
- Sharing levels govern reads through Vaelius. They do not recall copies a reader
  already made, or text an agent produced from delivered content outside a captured
  session. Inside a captured session, origin labels carry a delivered item's
  restriction into what the agent captures next.
- Origin labels are a deterministic best effort from what a tool call names; they
  are not a sandbox (see the limits in
  [data boundaries](DATA_BOUNDARIES.md#known-limits)).
- Redaction reduces exposure of credentials; it is not anonymization.

## Upgrading to migration 045

Migration 045 is additive and widens nothing:

- existing tenants keep their project default and get a tenant ceiling of `team`,
  the highest level that existed before; an operator raises it to allow
  `organization`;
- existing principals who own a source already stored as `team` get a person
  setting of `team`, so nothing currently visible disappears; every other existing
  principal, and every new one, starts at `private`;
- existing originals are `private`;
- policy definitions of existing sources are unchanged, so the search projection
  needs no rebuild.

## Upgrading to migration 047

Migration 047 is additive and widens nothing: curated and derived documents that
already exist keep the connection-visibility rule (`level_capped = 0`) until their
author changes a source they depend on; new ones are capped. Existing grants were
originals-only and stay so. Tenants allow owner sharing (`owner_sharing =
'allowed'`); an operator who wants lowering only sets `lower_only`. Interactive
enrollment now also grants `share`, and credentials issued before the migration
that hold `withdraw` also hold `share` (their stored action lists are not
rewritten; delegations never receive it).

A client that requests `organization` or `originals` needs a service with this
migration; an older service rejects those fields.

## Upgrading to migration 046

Migration 046 adds the origin columns, a trigger that keeps them fixed, and the
origin terms in the policy functions. Existing sources have no origin, so their
policy definitions and visibility are unchanged and no projection rebuild is needed.
Clients that send `origin_label` need a service with this migration; an older service
rejects the field and the client keeps such events queued, unshared, until the
service is upgraded. Deploy the service first.
