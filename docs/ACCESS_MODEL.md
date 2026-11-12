# Access model: team workspaces

One workspace per team. A workspace spans many repositories, and every chat is in
exactly one workspace: the one whose folder it started in. This page is the
specification; the [privacy model](PRIVACY_MODEL.md) and
[data boundaries](DATA_BOUNDARIES.md) describe the surrounding guarantees.

## Where memory lives

| Memory | Who reads it | Who writes it |
| --- | --- | --- |
| **Org workspace** (visibility `org`, the default) | Everyone signed in to the tenant (implicitly a reader), plus its listed readers, writers and admins | Its writers and admins, from chats in the workspace's folders |
| **Private workspace** (visibility `private`) | Only its listed readers, writers and admins | Its writers and admins, from chats in the workspace's folders |
| **Personal memory** | Only its owner | Its owner: `remember`, a private chat or folder, and every chat in a workspace folder where they are not a writer |
| **Read-only chat** | (writes nothing) | Nobody: the service refuses every write from it |

There is no internet-public tier and no per-item sharing. "Org reads, team writes"
is simply an org workspace whose team is listed as writers. A personal private
space already exists: `vaelius-client workspace create <name> --private` with no
other entries is a workspace of one.

## Rules

1. **Workspaces and folders.** A repository, or a subfolder of a monorepo, belongs
   to the workspace named by its nearest `.vaelius` scope file:

   ```toml
   workspace = "maple"          # the workspace name
   capture = "on"               # on | private | read-only | off
   read = ["maple", "birch"]    # optional recall filter (rule 6)
   ```

   *Repository boundary.* For capture and for file reads alike, a path inside a
   Git repository is governed only by a scope file at or below that repository's
   root; a folder in no repository takes the nearest scope file upwards.

   *Settings, lowest to highest.* The person's `default_capture` (`off` or
   `read-only`, never more; only where no scope file applies) < the committed
   `.vaelius` < their uncommitted `.vaelius.local` beside it (`capture` and `read`
   only) < the per-chat `session private` / `session read-only`. Each step can only
   narrow capture along `on` > `private` > `read-only` > `off`.

   A repository can also be linked to its workspace by normalized remote
   (`host/path`, lower case, no scheme, user, port or `.git`); one remote links to
   one workspace per tenant, and a capture whose remote is linked to a different
   workspace is refused. An admin's first capture from an unlinked remote links it.
2. **Roles.** An access list holds entries of a person, a group (`scim` from the
   identity provider, or `local`) and a role:
   - **reader**: can recall the workspace's memory; never writes to it;
   - **writer**: a reader whose chats in the workspace's folders are captured into it;
   - **admin**: a writer who also manages access and settings.

   A person's role is the highest any entry gives them (directly or through nested
   groups). Admins are the creator plus whoever admins add; the last admin cannot
   be removed. A tenant settings administrator can manage any access list as an
   audited break-glass operation.
3. **Visibility.** Chosen when the workspace is created, on the server:
   `vaelius-client workspace create <name>` makes it `org` (everyone in the tenant
   is implicitly a reader, and the listed writers and admins are the team);
   `--private` limits it to the listed entries, where readers read without
   contributing. Private needs no reason.
   An admin changes it with `vaelius-client workspace set-visibility <name>
   private|org`: private to org (admins only) reports how many existing items became
   visible to the org (`items_now_visible_to_org`); org to private needs no
   confirmation. The tenant setting
   `widest_entry` can forbid org-wide or group entries; `workspace_creation` can
   limit who creates workspaces. A `.vaelius` file never sets visibility.
4. **A chat is in one workspace.** Its label is taken when it starts, from its
   starting folder, and does not change when it moves elsewhere; `/clear` or a new
   chat starts a new one, compaction keeps it. There is no way to switch or widen
   it.
5. **Capture.** With `on`, a writer's or admin's chat is captured into the
   workspace; a reader's, or a non-member's, chat in the same folder is captured
   into **their personal memory only**: the service decides it on the person's
   current role at every capture and never trusts the client for it. A **private**
   chat (`vaelius-client session private`, the MCP tool `private_session`, or
   `capture = "private"` for a whole folder) is captured into the person's personal
   memory only. A **read-only** chat (`session read-only`, `read_only_session`, or
   `capture = "read-only"`) captures nothing. Both per-chat switches are one-way
   until `/clear` or a new chat, earlier captures stay where they were, and the
   service enforces both from the registered chat, whatever the client sends.
6. **Recall.** A session may recall its own workspace, the org workspaces (from any
   session) and any other workspace whose readers include everyone who can read
   the session's own workspace. Results are ordered by relevance across all of
   them, with no workspace ranked first; those from other workspaces carry `from
   <workspace> · <author> · <date>`. Personal memory is recalled only in a personal
   session: one whose capture stays with the person (a private chat or folder, a
   reader or non-member there, a private workspace of one, or a private personal
   setting). A private team's memory never reaches a session whose capture other
   people can read. A read-only chat recalls everything the person can read, since
   it writes nothing. A `read` list (or `workspaces` on one `search_memory` call)
   restricts which workspaces recall searches: a convenience filter, never a
   permission. The service intersects it with what the session may read, the
   current workspace always stays in, and without it recall is unfiltered.
7. **File reads** in a session whose capture is shared (the Claude Code guardrail
   hook, one line, never `ask`):
   - its own workspace's folders, and folders of a workspace whose readers include
     everyone who can read the session's workspace: allowed (the item carries that
     workspace, so later widening one workspace cannot expose another's content);
   - folders of any other workspace, an unknown one, or a scope file that does not
     parse: denied;
   - folders without a `.vaelius`: denied ("add a .vaelius naming its workspace");
   - Claude Code's auto memory (`~/.claude/projects/<project>/memory/`), which any
     earlier chat may have written: denied (keep entries with `vaelius-client
     import-memory`);
   - credentials and personal folders: denied in every session;
   - tooling, caches, system paths, temporary folders and Claude Code
     configuration: allowed.

   A personal session may read every workspace the person can read, unscoped
   folders and auto memory, because its capture is theirs alone.
8. **Personal memory and owner controls.** `remember`, `list_remembered` and
   `forget_remembered` (and `vaelius-client remember`) keep owner-private items;
   anyone who can open a personal session in a folder can remember there (no
   writer role needed), and a read-only chat cannot.
   `list_my_memories` / `vaelius-client mine` list what a person created and
   `forget_memories` / `vaelius-client forget` delete it. Nobody can share a single
   item with chosen people; an item's audience is its workspace's, or its owner
   alone.
9. **Originals** (complete source payloads and document bytes) are read only by
   their owner, or through an operator-issued originals grant (`agenthub.raw_access`,
   `policy`), and always need the credential action `source_read`.
10. **Credential actions bound roles.** Effective actions in a workspace are the
    credential's actions intersected with the role's: a reader `read`, `feedback`,
    `source_read`; a writer also `ingest`, `correct`, `curate`, `withdraw`; an admin
    also `manage` and moderation. Owners keep finding, withdrawing and deleting
    their own items whatever their role.

## Enforcement

| Layer | What it enforces |
| --- | --- |
| **One read predicate, three implementations** | *reader ∈ item audience* (owner; or not private, a role in the item's workspace and in every carried workspace) plus the session term for recall: the Python oracle (`agenthub.workspace_access`), the indexed SQL filter (`agenthub.search_permissions`) and PostgreSQL row-level security (`search_document_allowed`). Parity tests keep them identical. |
| **Capture** | `WorkspaceAccessMixin._ingest_privacy` stores a capture owner-private when the person is not a writer, the chat is registered private (`POST /enterprise/v3/sessions/private`), or the request, connection, personal setting or workspace default says private; `_check_remote` refuses a remote linked elsewhere. |
| **Recall filter** | `_recall_workspaces` intersects a `read` list with the label's workspaces; the result is one more predicate over the already authorized query, so it only narrows. |
| **Scope files (client)** | `scope_file.governing` is the one boundary rule for capture discovery and the read allowlist; `load` applies `.vaelius.local` narrow-only; `default_capture` rejects any value but `off` and `read-only`. |
| **Session label** | `POST /enterprise/v3/workspaces/descriptor` tells the client which workspaces its session may read; recall routes carry the label and the service recomputes it from current membership on every request (nothing about access is cached on the server). |
| **Read-only chats** | The service refuses every write route and tool with `403 read_only_session` for a registered chat, its MCP connection and `capture = "read-only"` folders. |
| **Guardrail hook** | Applies rule 7 from the cached descriptor, with no network call; an expired descriptor allows only the session's own workspace. The origin taint is the backstop for reads the hook cannot see (programs that open files themselves, other MCP servers, private network addresses): they make the session personal. |
| **Audit** | Every access-list, visibility, link and settings change is audited with the audience size before and after; see [access reviews](ACCESS_REVIEW.md). |

History: workspaces, groups and access lists (migration 048), session labels (049),
GitHub team sync added and removed (050, 054), remembered items (051), read-only
chats and server-side visibility (053), per-item sharing and explicit narrowing
removed (055), private chats (056).
