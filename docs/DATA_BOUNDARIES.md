# Data and security boundaries

Vaelius stores deterministically redacted source records and their provenance,
then indexes exact passages. Redaction removes recognizable credential patterns;
it is not universal secret detection or blanket anonymization. Useful permitted
person and location facts can remain. Unsupported source formats and capture gaps
remain explicit rather than being labeled complete.

## Where captured content is stored

| Location | What it holds | Redaction before write |
| --- | --- | --- |
| Client outbox `capture-outbox.sqlite` (0600, profile dir 0700) | Pending typed events until the service acknowledges them | Client redaction; `Outbox.put` refuses any value the detector would still change |
| Client `enterprise-capture-gaps.jsonl` (0600) | Gap diagnostics: time, session hash, event kind, reason, project, session and turn IDs | No content fields are written |
| Service `backend_parts` | Transport fragments of a multi-part event, only until reassembly completes; deleted on completion or rejection. Single-part events are never staged | Client redaction only (a fragment cannot be re-redacted safely); the reassembled event is re-redacted before use |
| Service `backend_source_revisions.payload`, `memories.body`, `source_event_metadata` | Canonical typed event and its searchable body | Server re-redaction with the shared detector |
| Retained conversation object (`cloud_conversation_segments`, S3 or file adapter) | The same canonical payload, checksum-bound to the PostgreSQL row | Server re-redaction; the object adapter also refuses a non-fixed-point payload |
| Legacy v1 intake (`/enterprise/v1/sources`) | Untyped source body | A detected credential rejects the whole event; nothing is written |
| Source passages (`memories` rows of kind `NativeRepresentation`, knowledge revisions, FTS and vectors) | Exact spans of the redacted body | Derived from redacted text; indexing refuses an unredacted source |
| Original documents (`backend_document_versions` + object) | Uploaded bytes, unaltered | Upload is **rejected** if the bytes, parsed text or metadata contain a detected credential; originals are never rewritten |
| Derived knowledge (curated/observer output) | Model or reviewer output | Cleaned with the same detector before acceptance |
| `enterprise_audit`, job/outbox `last_error`, receipts | IDs, operation names, dispositions, error codes | No content; persisted worker errors are passed through the detector |
| Backups (`agenthub.cloud_recovery`) | Every table and retained object | Inherit what the live store holds, including any staged fragments present at backup time |

Captured conversation content is redacted twice: by the client before hashing
and transport, and by the service before **any** row or object is written. The
service never trusts client redaction. When it changes anything it appends
`server_`-prefixed entries to the event's `redactions` manifest (field path and
rule IDs only, never the value) and marks the event `redacted`. A credential in an
identity field (connection, external ID, revision, project) cannot be rewritten
and the event is rejected instead. Older clients that miss a newer rule are
therefore still accepted with their content redacted, not rejected.

Intentionally uploaded documents are different by design: their originals keep
exact bytes so checksums and passage locations stay verifiable. Instead of being
rewritten, a document containing a detected credential is refused, and accepted
originals are owner-only by default (below).

## Detection: guarantees and limits

One module, `agentclient.secret_detection`, is used by client capture, service
intake, indexing and the operator scanner. The service pins its checksum in
`CLIENT_PIPELINE_PIN.json`, so a server cannot run with a different detector than
the release it was built against. Rules cover AWS access key IDs and secret
assignments; GitHub `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_` tokens;
Slack `xox*`/`xapp` tokens; Google `AIza` keys; Stripe `sk_`/`rk_` live, test and
prod keys; `sk-` style API keys (OpenAI, Anthropic); JWTs; PEM/OpenSSH/PGP private
key blocks (including truncated ones); credentials in connection URLs;
`Authorization`/`Cookie` headers and bearer tokens; credential-named assignments
(`password`, `secret`, `token`, `api_key`, `client_secret`, ...); and random-looking
values assigned to a credential-like name (any prefix, including camelCase). Several
formats are adapted from the MIT-licensed Gitleaks default rules; see `NOTICE`.

Limits, stated plainly:

- Detection is pattern- and heuristic-based. A credential in an unrecognized format,
  split across lines or fields, encoded (base64, URL-encoded), or written as prose
  ("the password is hunter two") is **not** guaranteed to be found.
- To keep false positives low on code and prose, references are not redacted
  (`os.environ[...]`, `${VAR}`, `getpass()`, `None`), and the high-entropy rule ignores
  hex digests, UUIDs, lowercase slugs, dotted names, paths and `public*` key names.
- Some false positives remain: any value assigned to `password`/`token`/... is
  redacted even when it is not secret, and `Authorization:` lines are redacted to
  the end of the line.
- Every quantifier is bounded and multi-line blocks are located without nested
  scans; regression tests bound 1 MB adversarial inputs.
- Rotate any credential that was ever captured. Redaction reduces exposure; it does
  not make a leaked credential safe.

## Raw-access policy

Two tiers, both rechecked at delivery:

1. **Redacted passages** — search results, cited evidence spans and conversation
   context. Need `read`. Raw-representation passages are visible to their source
   owner, and to others when the source is **in their workspace audience**, or
   through an explicit owner share
   ([below](#shared-conversation-passages-and-originals)). Other principals see
   derived knowledge and its citations through the normal search policy.
2. **Retained originals** — the complete canonical source (`/enterprise/v2/sources/`,
   `/enterprise/v1/sources/`) and original document bytes (`source-documents/download`).
   Need the credential action `source_read` **and** source ownership, or an
   explicit, active, owner-issued originals share. There is no originals level
   any more; sharing passages never shares originals. Document metadata
   (`describe`, `list`) stays at tier 1.

`source_read` is no longer part of default enrollment (`/enterprise/v3/auth/complete`
without operator `enrollment_actions`) or of the default OAuth scope mapping (a
provider without an explicit `actions` list). Operators who want users to download
originals list it explicitly. Grants never bypass tenant, lifecycle, delegation or
connection-reader checks, and a grantee still needs `source_read`:

```sh
# Owner grants (requires the owner's credential with the policy action):
python -m agenthub.raw_access grant --profile /absolute/private/profile --tenant acme \
  --token-file /absolute/private/owner.token --scope-kind connection --scope-id team-documents \
  --grantee-kind group --grantee maple-team
python -m agenthub.raw_access revoke ...   # same arguments; the row is kept for audit
python -m agenthub.raw_access list --profile ... --tenant acme --token-file ...
```

A grant names one source, one capture session or one connection, and either a
`principal` or a `group` (`project` grantees are refused with
`project_grantee_retired_use_group`; migration 048 rewrites existing project
grants into grants to that project's `<project>-members` local group). These
operator-style commands grant originals only. Owners grant passages, and
optionally originals, themselves with `vaelius-client share` or the MCP
`share_memories` tool and the `share` action; no grant reaches a derived
document, and no grant widens an `origin_private` source
([owner controls](PRIVACY_MODEL.md#owner-controls)).

### Upgrade and migration

Migration `040_raw_source_grants.sql` is additive. Run migrations, then refresh
application-role grants with `cloud_profile._grant` as for earlier migrations.
Existing credentials keep their actions, including `source_read`; nothing is
revoked automatically. Behavior changes at upgrade:

- Team/organization readers lose original-document **download** until the owner
  issues a grant (metadata and passages are unaffected).
- Credentials with `read` but no `source_read` can now search their owner's own
  redacted passages and fetch their evidence; they still cannot fetch originals.

To bring existing credentials to least privilege:

```sh
python -m agenthub.raw_access report --profile /absolute/private/profile --tenant acme
python -m agenthub.raw_access strip-source-read --profile ... --tenant acme --principal alice          # dry run
python -m agenthub.raw_access strip-source-read --profile ... --tenant acme --principal alice --apply
```

`strip-source-read` removes only that action (a credential left with no actions is
deactivated) and is audited. Regrant by enrolling a new credential that lists
`source_read`. For provider OAuth, remove `source_read` from the provider's
`actions` (or omit the list) and from the identity server's client scopes; the
next introspection applies it. Rolling back to an older binary ignores grants and
re-opens team document downloads; apply the same hold as other source-pipeline
rollbacks.

## Shared conversation passages and originals

The complete model, including who controls each setting, is in the
[privacy model](PRIVACY_MODEL.md) and the design is in
[the access model](ACCESS_MODEL.md). In short: every captured source belongs to
one **workspace** and is either **private** (its owner only) or shared with the
workspace audience (every principal the access list reaches as a reader).
Originals are a separate tier, read with ownership or an **originals share**.

By default, a repository is captured when a checked-in scope file names the
workspace ([client side](CLIENT.md#repository-scope-files)):

```toml
# .vaelius at the repository root
workspace = "example-repo"   # the Vaelius workspace this capture belongs to
capture = "on"               # on | read-only | off
# sharing = "private"        # optional owner opt-out; omit to inherit the audience
```

The file is a **request, never a grant**: anyone who can edit the repository
can edit it, and `workspace =` only works when the person already holds a role
in that workspace. Legacy keys (`project`, `sharing`, `originals`) are read as
an upper bound and are mapped as described in
[the client guide](CLIENT.md#repository-scope-files) and
[the access model](ACCESS_MODEL.md#5-the-scope-file).

Every capture also carries a normalized Git **remote** (`host/path`, lower
case, no scheme/user/port, no trailing `.git` or `/`). The server rejects a
capture whose remote is not linked to the named workspace with HTTP 409
`repo_not_linked`; the client keeps the event queued and the status line names
the admin fix. An admin's first capture from an unlinked remote links it
automatically (role `admin`, credential `manage`, non-delegated) and the
parts receipt carries a one-line `notice`. A capture with no remote (a scoped
folder that is not a Git repository) is accepted on the person's writer role
alone.

| Step | Rule |
| --- | --- |
| Request | The client sends `workspace`, the normalized `remote` and an optional `sharing`. `sharing` is omitted (owner-private) or `organization` (meaning "inherit the workspace audience"); legacy `team` is still accepted. `originals` is no longer sent. |
| Ingest | The service resolves the workspace by name then id, checks that the credential ∩ the person's role in that workspace holds `ingest` (so the writer role or higher), and checks that `remote` is linked (section above). It stores the item with `private = 1` when (a) `sharing` is omitted, (b) `team` is requested against a workspace with an organization entry, (c) the capture connection is private, (d) the person's `contributions_default` is `private`, or (e) the workspace's `item_default` is `private`. Otherwise the item inherits the workspace audience. |
| Query | A reader other than the owner reads a passage, its cited evidence and its conversation context only when the item is not private AND the reader is in the audience (`role(ws) ≥ reader`), OR they are the subject of an explicit share. The reader also needs to be in the audience of every **carried workspace** on the item (`origin_projects`), and (for items captured before migration 048 with origin ceiling `team`) a direct — non-organization — role in the workspace and in every carried workspace. |

Consequences:

- **Removing a principal entry or a group member** narrows items on the next
  request. Lowering `widest_entry` suspends wider entries at query time, so it
  hides immediately and raising restores.
- **Adding an entry** widens the audience of every item in the workspace, past
  and future, as on GitHub, Google Drive and Notion. Carried workspaces stop a
  later widening of one workspace from exposing content that came from another.
- **Shares** stand in for the audience check for one reader at a time and
  obey `owner_sharing`.
- **Originals** need `source_read` and either ownership or an originals share.
- Every existing protection still applies on top: tenant isolation, withdrawal
  and deletion, legal holds (a held deletion hides like a withdrawal), operator
  quarantine (held sources stay owner-only), delegation limits, connection
  reader lists and freshness.

Who sees what for a passage Alice captured in workspace `maple` (access list:
`principal:alice → admin`, `group:maple-team → writer`, `organization →
reader`), with her own `contributions_default = shared`:

| Reader | Alice marks private | Default (audience) | Alice shares to `principal:carol` |
| --- | --- | --- | --- |
| Alice | passage + original | passage + original | passage + original |
| Bob, `maple-team` member | nothing | passage, evidence, context | nothing (not shared to) |
| Carol, in the org but not in `maple-team` | nothing | passage, evidence, context (through the organization entry) | passage, evidence, context (through the share) |
| A quarantined, deactivated or delegated reader | nothing | nothing | nothing |

Originals in that table additionally require ownership, an originals share and
`source_read`.

Admins manage the access list, repository links and workspace settings from the
client; operators and settings administrators can do the same as a break-glass
override. The commands are in
[the client guide](CLIENT.md#workspaces-and-groups) and
[self-hosting](SELF_HOSTING.md).

Enforcement is one predicate, in three places: the Python oracle
(`agenthub.workspace_access.WorkspaceAccessMixin`, used by
`EnterpriseStore._document_allowed`), the indexed SQL filter
(`agenthub.search_permissions.AUDIENCE`, computed once per request from the
reader's workspaces `R` and delegated set `D`) and the row-level security
functions `search_reader_in_audience` and `search_document_allowed`, applied by
the restricted search-reader role. A parity test keeps all three in agreement,
and a teammate's search runs the same number of SQL statements as the owner's.

Migrations `042_project_sharing.sql` (team sharing), `045_sharing_levels.sql`
(the ladder, person and tenant ceilings, originals level), `046_origin_labels`
and `047_curated_derived_cap` prepared the ground; migration
`048_workspaces.sql` replaces the ladder with workspace audiences and explicit
shares. See [upgrading to migration 048](PRIVACY_MODEL.md#upgrading-to-migration-048).
A client that sends `workspace` or `remote` against an earlier service is
rejected with 400 `invalid_general_fields` for those fields.

## Origin labels and session taint

A scope file decides capture for a whole session, but a single tool call can read
far outside the repository: a document in the person's home folder, a mail or chat
MCP server, a credentials file. Without a further check that content would be
captured with the workspace's audience. The client therefore labels every captured
event with what it touched (`agentclient.origin`), deterministically, locally and
without a model call, and the service enforces the label.

The scoped-folder allowlist rules below (#40) are unchanged. What differs under
the access model is who the labels reach: an item's audience is its workspace
access list, and **carried workspaces** (what `origin_projects` holds) each
need the reader to have at least `reader` in them too. An `origin_private`
label still makes the item owner-only and no share can widen it.

### Fixed session labels and the backstop

Since PR B the primary control for Claude Code sessions is the **fixed session
label** ([access model, section 7](ACCESS_MODEL.md#7-fixed-session-labels)): a
session started in a workspace folder with capture on may read only folders whose
workspace audience contains its own, and the PreToolUse guardrail hook denies the
rest before the read happens. The labels below remain, demoted to a **backstop**
for what the hook cannot see or classify:

| Read | Hook (before) | Origin label (after) |
| --- | --- | --- |
| The session's own workspace, or one it is narrowed to | Allow | None (narrowed workspaces are carried) |
| Another workspace whose audience contains the session's | Allow | Carries that workspace; never narrows the session |
| A narrower, unreadable or unknown workspace, or an unreadable scope file | **Deny** (one plain line, never `ask`) | — (the tool does not run) |
| Personal folders, credential files | Deny (guardrail profile, unchanged) | `private` |
| Unscoped repositories and unknown paths | Allow | `private` (the session becomes private) |
| Programs that open files without naming them, MCP servers other than Vaelius, non-public web addresses, unknown tools | Not visible to the hook | `private`, exactly as before |
| Recalled memory | Filtered by the service to the session's superset list | Carries its workspaces; never narrows |

A read the hook allowed therefore never also narrows the session; it only adds the
workspace to the carried set, so captured items are still limited to people who can
read both. Sessions without a label (read-only, off, outside every workspace, other
hosts, older clients) keep the #32 behavior described below unchanged.

### What an event touched

| Touched | Label |
| --- | --- |
| Credentials (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.netrc`, `.env`, `*.pem`, `id_*` keys, agent sign-in files `~/.claude/.credentials.json`, `~/.claude.json`, `~/.codex/auth.json`, ...), anywhere, including inside the project. Checked-in templates (`.env.example`, `.env.sample`, `.env.template`, ...) are not credentials | always `private`; no allowlist opens them |
| A path inside a [scoped folder](#scoped-folders) of the **same workspace**: the current root, another checkout or worktree, a sibling folder whose scope file names the same `workspace` (legacy `project`). Symlinks, `..` and relative paths are resolved against the event's working directory first | no restriction |
| A path inside a scoped folder of a **different workspace** | tagged as a carried workspace: a non-owner reader must be at least `reader` in that other workspace too, so the item is readable only by people in **every** workspace the session carried |
| A path inside a scoped folder whose scope file is off or invalid | `private` |
| Claude Code's own state, `~/.claude/projects/<folder>/` (saved tool output, task output, auto-memory, transcripts) | follows the folder the name encodes: the current session's folder is unrestricted, a scoped folder follows that folder's project as above, anything else is `private` |
| Claude Code user configuration already loaded into sessions: `~/.claude/CLAUDE.md`, `~/.claude/reference/`, `~/.claude/plans/`, `~/.claude/settings*.json`, `~/.claude/plugins/`, `~/.claude/skills/`, `~/.claude/agents/`, `~/.claude/commands/`, `~/.claude/hooks/`, `~/.claude/output-styles/` | no restriction |
| Personal folders under home (`Documents`, `Desktop`, `Downloads`, `Pictures`, `Movies`, `Music`, iCloud and cloud-drive folders, `Library/Mail`, `Library/Messages`) outside every scoped folder | always `private`; no allowlist opens them |
| Temporary folders (`$TMPDIR`, `/tmp`, `/private/var/folders`, including Claude Code's scratch and task folders), tool caches and package folders (`~/.cache`, `~/.npm`, `~/.cargo`, `site-packages`, `node_modules`, ...), read-only system locations (`/usr`, `/opt/homebrew`, `/etc`, `/Library/Developer`) | no restriction |
| A Git repository with no governing scope file | `private` |
| Anything else that exists outside every scoped folder (the rest of the home folder, other volumes) | `private` |
| A path that does not exist (a file the agent is about to create) | no restriction: nothing was read |
| Public web fetches and searches | no restriction, recorded as origin `web` |
| A fetch from a non-public address (`localhost`, private and link-local ranges, single-label and intranet-style names, `file:`) | `private` (`file:` follows the path rules) |
| An MCP server other than Vaelius itself (mail, chat, document stores, issue trackers, ...) | `private`: those systems have permissions memory cannot reproduce |
| Recalled or injected memory | the label the service attached to each item: an item whose audience contains the current session audience adds nothing (and so does an item from the same workspace); an item from another workspace is added to the tag set as a carried workspace; a private or unlabelled item is `private`. The person's own preferences are standing instructions, like prompts, and do not restrict |
| A tool the client does not recognise | `private` |

Planning, delegation and question tools (`TodoWrite`, `Task`/`Agent`, `AskUserQuestion`,
`Skill`, `update_plan`, ...) touch nothing themselves; a subagent's own tool calls
are labelled as they happen.

**Shell commands** are labelled only by the paths they name, after expanding `~`,
`$HOME`, `$TMPDIR` and `$PWD`, following `cd`/`pushd`, and scanning inside command
and process substitution, `bash -c`/`eval` scripts, heredocs and quoted inline code
(`python -c "open('/x')"`). Inline code that asks for the home folder indirectly
(`expanduser`, `Path.home()`, `os.homedir()`, `getenv("HOME")`) is `private`. A
command that names no outside path does not restrict, whatever it runs: `aws`,
`gh`, `git`, `curl`, `kubectl`, `cdk`, test runners. A bare name that exists in the
working directory is checked too, so `cat link` through a symlink out of the
repository is caught. Hosts are covered: Claude Code `Bash`, Codex `Bash`
(including unified exec), and Codex rollout `shell`, `exec_command`,
`CommandExecution` and `write_stdin`; file tools `Read`, `Write`, `Edit`,
`MultiEdit`, `NotebookEdit`, `Glob`/`Grep` (including the files they returned),
`LS`, `LSP`, Codex `apply_patch` and `FileChange`; `WebFetch`, `WebSearch`; and
`mcp__<server>__<tool>`, plugin servers `mcp__plugin_<plugin>_<server>__<tool>`,
MCP resource tools and Codex `McpToolCall` items.

### Scoped folders

A folder that holds a scope file (`.vaelius`, or a legacy name from the profile's
`scope_files`) is a **scoped folder**, together with its whole subtree, whether or
not it is a Git repository. The nearest scope file above a path governs it, so a
nested scope file overrides its parent, and a workspace folder with one scope file
covers every repository inside it that has none. The rules are checked in the order
of the table above: credentials first, then scoped folders, so a repository under
`~/Documents` behaves like any other repository, while a loose file in `~/Documents`
stays private.

A scope file is **ignored**, neither counting nor overriding, when it sits at:

- the home folder, any folder above it, or the filesystem root;
- directly at a top-level personal folder: `~/Documents`, `~/Desktop`,
  `~/Downloads`, `~/Pictures`, `~/Movies`, `~/Music`, the iCloud Drive root and the
  cloud-drive folders;
- anywhere inside the mail and chat stores (`Library/Mail`, `Library/Messages`,
  their containers).

A scope file deeper inside a personal folder, for example
`~/Documents/some-workspace/.vaelius`, counts normally. A session whose own scope
file sits at an ignored place has no in-scope root for labelling: everything it
reads outside temporary, tool and system folders makes it private.

Capture discovery applies the same rule, through the same predicate
(`scope_file.ignored_scope_root`): a scope file at the home folder, above it, or
directly in a top-level personal folder never activates capture, never enrolls its
folder and never selects a workspace or session label. A session started in
`~/scratch` under a stray `~/.vaelius` is simply not captured.

Reading another scoped folder changes the label only when its audience is
narrower: the same `workspace` changes nothing and never shows a status line; a
different workspace adds that workspace to the carried-workspace tag set as
described above.

**Claude Code state.** Claude Code names each `~/.claude/projects/<folder>` after
the session's starting directory, replacing every character other than `A-Z`, `a-z`
and `0-9` with `-` (so `/work/my_repo.v2` becomes `-work-my-repo-v2`). The client
treats the folder of the current session (from the working directory, the scope
root, or the hook's transcript path) as the workspace itself. Any other folder is
matched back to an existing directory by that encoding and follows that directory's
scope; a folder that matches no existing scoped directory is `private`.

**What the agent is told.** At session start in a captured project, the client adds
one informational line, shown to the person and given to the agent as context:

```text
vaelius: in-scope folders: ~/src/repo, ../docs-site (reads elsewhere make this session private)
```

It lists the current root and scoped sibling folders next to it, stays under 200
characters, and never lists the home folder or a personal folder. Set
`"scope_context_notice": false` in the client profile's `config.json` to turn it off.

### Exceptions in the scope file

`capture` may be a table that names places whose content may be shared with the
repository's own workspace audience:

```toml
workspace = "example-repo"

[capture]
mode = "on"                                  # on | read-only | off (default on)
allowed_paths = ["../shared-fixtures"]       # relative to the scope file, absolute, or ~/
allowed_mcp_servers = ["docs"]               # names as the host registers them
```

Anyone who can edit the repository can edit this file, so an `allowed_paths` entry
that is the filesystem root, the home folder or above it, or a hidden folder or
`Library` under home is ignored, and personal folders and credentials stay private
even below an allowed entry. `allowed_mcp_servers` also matches a plugin-provided
server of that name. An invalid table turns capture off, like any invalid scope file.

### Session taint

Content an agent has read stays in its context, so labels are **sticky per
session**. The client keeps a small state file in the profile
(`origin-sessions.json`, 0600, keyed by a hash of the session ID; ceiling, project
set, reason category, start time and a coarse display hint such as `~/Documents/...`;
entries untouched for 30 days are dropped) so the label survives the separate hook
processes:

- the event that first touches restricted content, and **every later event** of
  the session (tool results, prompts, assistant replies, turn ends), carry at least
  that restriction; the most restrictive label wins and nothing widens it again;
- events captured before it are unaffected;
- a session that works in one workspace and then in another carries the first
  workspace into the second one's tag set;
- only a **new session ID** starts clean: `/clear`, a new conversation. Resuming the
  same session keeps the label. **Compaction does not reset it**, because the
  summary that replaces the context is written from the restricted content;
- reads while capture is paused, or while the working directory is outside every
  workspace, still count: a session that read `~/Documents` and then moves into a
  captured repository is restricted there;
- when a label cannot be computed or recorded, the event is `private`.

The person sees one line when it happens, and again when a restricted session is
resumed, for example:

```text
vaelius: this session read personal files (~/Documents/...) — capture is now private
vaelius: this session spans workspaces maple, birch — capture is now limited to people who are readers in every workspace involved
```

### On the wire and in storage

Each event carries an optional `origin_label`:

```json
{"ceiling": "private", "kinds": ["outside_repo"], "reason": "personal_content",
 "projects": [], "inherited": false}
```

`ceiling` is `private`, `team` or `organization` (the wire ladder is kept for
origin and provenance; the service maps `private` to `origin_private` and
`team` to the legacy "members-only origin" rule); `kinds` and `reason` come
from fixed category lists in `agentclient.general_contract`; `projects` names
up to 16 carried workspaces by id; `inherited` says the restriction comes from
an earlier event of the session. Paths, commands, server names and display
hints never leave the computer. Events that only touched the session's own
workspace carry no label. The client also omits `sharing` (owner-private) when
the ceiling is `private`, so even a service that ignored the label would not
widen the event.

The service treats the label as a restriction it enforces, never a grant:

| Step | Rule |
| --- | --- |
| Validate | `origin_label` must match the contract (fixed categories, at most 16 workspace ids in `projects`); anything else rejects the event. The source's own workspace is removed from `projects` |
| Ingest | A ceiling of `private` makes the item `origin_private` (owner only, no share can widen). Every entry in `projects` becomes a **carried workspace** on the item (`origin_projects`), and a non-owner reader must be at least `reader` in each of them. The label is stored as `origin_ceiling`, `origin_reason` and `origin_projects`, and a restricting label is audited (`origin_label` / its ceiling) |
| Fixed | A trigger refuses any later change to the three origin columns, so no access-list change, maintenance task or manual update can widen an origin |
| Query | The one predicate reads the stored origin: `origin_private` denies everyone but the owner, and each carried workspace is checked for `role(ws) ≥ reader`. Items captured before migration 048 with origin ceiling `team` keep the exact pre-048 meaning: a non-owner needs a **direct** (non-organization) role in the workspace and in every carried workspace. Removing a reader from a carried workspace hides the item at once; raising anything never exposes it beyond the stored origin |
| Derived documents | Every derived document is capped (`level_capped = 1`): a reader needs to be in the audience of every source it depends on, including each source's carried workspaces. The same clause is in the Python oracle (`EnterpriseStore._document_allowed`), the indexed SQL filter (`search_permissions.AUDIENCE`) and the row-level security function (`search_document_allowed`), and a parity test checks all three |
| Delivery | Recalled items report `origin.projects`: the carried workspaces their sources hold, so a client that restates them tags its own session the same way |

Sources without an origin label have no origin keys in their policy definition, so
existing definitions are unchanged (migration `046_origin_labels.sql` needs no
projection rebuild). `explain` lists a source's passage readers under these rules in
`raw_readers`.

### Known limits

The labels are a best-effort, deterministic guard against the common ways content
from elsewhere enters a session. They are not a sandbox.

- **A program can read anything without naming a path.** `./run.sh`, `make`,
  `pytest`, a Python script or a build tool in the repository may read the home
  folder internally; only the command line is inspected. So do `xargs` or
  `while read` loops fed from a file, and variables other than `HOME`, `TMPDIR` and
  `PWD`.
- **Commands that fetch from other systems are not restricted** unless they name
  an outside path: `aws`, `gh`, `kubectl`, `curl`, `ssh` and database clients
  return data whose permissions memory cannot see. This is a deliberate trade so
  ordinary engineering sessions are not all private; MCP servers, by contrast, are
  restricted by default.
- **Intranet addresses with public-looking names** are treated as public web by
  `WebFetch`. Only `localhost`, IP ranges that are not global and intranet-style
  names are recognised.
- **Allowed locations can hold personal content.** A file copied into `/tmp`, a
  cache folder or a package folder is not restricted unless its name or symlink
  target says otherwise.
- **Forks lose the label.** A forked session (`--fork-session`, Codex `fork`) gets a
  new session ID while keeping the parent's context; the client cannot link the two.
  The same applies to any host that resumes under a new ID.
- **Subagents** are labelled by the tool calls their hooks report. A host that runs a
  subagent under a different session ID would not taint the parent.
- **The scope file is editable by anyone who can edit the repository**, including
  its allowlists, within the guards above.
- **Text the person types** (a pasted document in a prompt) is captured with the
  session's current label; the client does not inspect prompt content.
- **Local state**: deleting or corrupting `origin-sessions.json` forgets the labels
  of running sessions; deleting `session-labels.json` removes the session-label read
  rule from running sessions (captures still carry what they read).
- **The read rule trusts the client's descriptor** for the allow decision. It is
  advisory: a stale descriptor can allow a read the service would now refuse, which
  the carried set then limits; after 24 hours without a refresh only the session's
  own workspaces are readable.

## Retention guidance

- Keep only what you need: withdraw or delete sources (`/enterprise/v1/lifecycle`)
  rather than relying on access control alone. Deletion purges source and derived
  content in PostgreSQL; verify retained objects and backups are expired on the
  same schedule, since a backup taken earlier still holds the content.
- Configure object-store lifecycle rules and versioning deliberately: a versioned
  bucket keeps deleted originals as noncurrent versions until they expire.
- Incomplete multi-part staging is visible in the worker status (`pending_parts`)
  and in the scan report; investigate stale entries.
- Encrypt object storage with a customer-managed key (below) and the database at rest.

## Scanning retained content

```sh
python -m agenthub.secret_scan --profile /absolute/private/profile --tenant acme [--objects]
```

Reads payloads, source bodies, passages, knowledge revisions, staged parts, error
fields and (with `--objects`) retained objects, and reports counts by location, rule
and owning principal. It never prints values, source IDs or text. To replace
findings in typed captured sources:

```sh
python -m agenthub.secret_scan --profile ... --tenant acme --rewrite-redacted \
  --token-file /absolute/private/owner.token            # dry run: plan only
python -m agenthub.secret_scan ... --rewrite-redacted --token-file ... --apply
```

`--apply` writes a redacted next revision through the normal source revision path
with the owner's credential (the connection's enrollment). The superseded revision
is retired, not deleted; purging it is an explicit lifecycle `delete`. Original
documents and v1 sources are immutable here and need a lifecycle decision.

## Object-storage hardening

See [object storage hardening](../packages/server/docs/CLOUD_SOURCE_OBJECTS.md#hardening)
for SSE-KMS, TLS, file permissions and example least-privilege bucket and key policies.

## Agent access on the capturing machine

Client redaction only sees what the agent read. Before capture, the Claude Code
guardrail profiles limit what an agent working in a captured project can read on the
person's computer. The default `standard` profile refuses personal folders
(Documents, Desktop, Downloads, Mail, iCloud Drive ...) and credential files, and
leaves the tools that read their own configuration working. The opt-in `strict`
profile confines file tools and the Bash sandbox to the repository. Both are
best-effort settings that the person controls unless an organisation deploys them
as managed settings. `standard` is a denylist, and it does not stop a program that
opens a file without naming it. Both profiles also refuse agent sign-in files
(`~/.claude/.credentials.json`, `~/.claude.json`, `~/.codex/auth.json`) and ask the
person before the agent changes its own configuration (`~/.claude/CLAUDE.md`,
settings, skills, hooks, Codex `config.toml`, ...), which would otherwise carry into
every later session; Bash writes there are covered only by the guardrail hook. Both
profiles also apply the fixed session-label read
rule to labelled sessions: folders of narrower, unreadable or unknown workspaces are
denied. The exact coverage and gaps are in
[least privilege for agents](CLIENT.md#least-privilege-for-agents).

## Trust boundaries

Raw agent originals require `source_read` and ownership or an owner-issued
originals share. Raw source passages require ownership, or that the reader is
in the item's audience (the workspace access list, minus `private` or
`origin_private`) or named by an explicit owner share. A workspace name or a
repository scope file does not itself grant access
([privacy model](PRIVACY_MODEL.md)). Reviewed derived knowledge and native
document representations retain their source policy dependencies. Identity,
delegation, connection freshness, lifecycle state and exact revision integrity are
rechecked before evidence is delivered. The operator controls enrollment and
sharing; installation grants neither.

Search results are untrusted references. Similarity scores are discovery signals,
not proof of truth or complete answers. Agents can fetch cited evidence, expand
surrounding conversation and inspect originals before deciding. LLM output is
validated by the canonical backend; optional enrichment does not replace originals.

Shared memory can carry one person's text into another person's agent. Every
delivered item names its author, project, capture time and whether it is original
wording; instruction-shaped text is labelled, never executed. Writes are rate
limited per principal and tenant, and an operator can quarantine a principal or
bulk-withdraw their content. See [shared-memory governance](MEMORY_GOVERNANCE.md).

Withdrawal blocks access to dependent knowledge while preserving recorded history
under its lifecycle policy. Deletion purges source/derived content, vectors and
retained originals and records its denial/deletion journal. Operator retention
policies, legal holds and subject erasure use the same purge; see
[data lifecycle](DATA_LIFECYCLE.md) for stores, verification and limits. Scope
changes invalidate stale processing and retrieval. User preferences can be private
to one user independently of team knowledge.

This prototype trusts the service operator. Application ACLs do not hide database
contents from an administrator with host/database credentials. A production host
still needs network isolation, TLS, backups, secret management, operational
monitoring and verified identity-provider configuration. Local synthetic tests do
not establish cloud deployment or real-team acceptance.

See [SECURITY.md](../SECURITY.md),
[source-first semantics](../packages/server/docs/SOURCE_FIRST.md), and
[indexed vector access](../packages/server/docs/VECTOR_INDEX.md).
