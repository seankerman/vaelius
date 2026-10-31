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
| **Personal memory** | Only its owner | Its owner: `remember`, and every chat in a workspace folder where they are not a writer |
| **Read-only chat** | (writes nothing) | Nobody: the service refuses every write from it |

There is no internet-public tier and no per-item sharing. "Org reads, team writes"
is simply an org workspace whose team is listed as writers.

## Rules

1. **Workspaces and folders.** A repository, or a subfolder of a monorepo, belongs
   to the workspace named by its nearest `.vaelius` scope file:

   ```toml
   workspace = "maple"   # the workspace name
   capture = "on"        # on | read-only | off
   ```

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
5. **Capture.** A writer's or admin's chat is captured into the workspace. A
   reader's, or a non-member's, chat in the same folder is captured into **their
   personal memory only**: the service decides it on the person's current role at
   every capture and never trusts the client for it. A read-only chat (`vaelius-client
   session read-only`, the MCP tool `read_only_session`, or `capture = "read-only"`)
   captures nothing.
6. **Recall.** A session recalls its own workspace first, then the org workspaces
   (from any session), then any other workspace whose readers include everyone who
   can read the session's own workspace. Personal memory is recalled only in a
   personal session: one whose capture stays with the person (a reader or
   non-member there, a private workspace of one, or a private personal setting). A
   private team's memory never reaches a session whose capture other people can
   read. A read-only chat recalls everything the person can read, since it writes
   nothing.
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
   `forget_remembered` (and `vaelius-client remember`) keep owner-private items.
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
| **Capture** | `WorkspaceAccessMixin._ingest_privacy` stores a capture owner-private when the person is not a writer, the request, connection, personal setting or workspace default says private; `_check_remote` refuses a remote linked elsewhere. |
| **Session label** | `POST /enterprise/v3/workspaces/descriptor` tells the client which workspaces its session may read; recall routes carry the label and the service recomputes it from current membership on every request (nothing about access is cached on the server). |
| **Read-only chats** | The service refuses every write route and tool with `403 read_only_session` for a registered chat, its MCP connection and `capture = "read-only"` folders. |
| **Guardrail hook** | Applies rule 7 from the cached descriptor, with no network call; an expired descriptor allows only the session's own workspace. The origin taint is the backstop for reads the hook cannot see (programs that open files themselves, other MCP servers, private network addresses): they make the session personal. |
| **Audit** | Every access-list, visibility, link and settings change is audited with the audience size before and after; see [access reviews](ACCESS_REVIEW.md). |

History: workspaces, groups and access lists (migration 048), session labels (049),
GitHub team sync added and removed (050, 054), remembered items (051), read-only
chats and server-side visibility (053), per-item sharing and explicit narrowing
removed (055).
