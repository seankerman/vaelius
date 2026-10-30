# Client installation and enrollment

Install `packages/client` from the matching Vaelius release. It has no backend or
model dependencies. The client stores private credentials, capture cursors and a
durable transport outbox; acknowledged payloads are removed and the outbox is never
searched. Original documents go through the backend document API.

The operator must configure the identity broker, enrollment and source connections
first. Login creates a private profile and enrolls no project automatically:

```sh
python -P -m agentclient.cloud_enroll \
  --home /absolute/private/client-profile --url https://memory.example.org \
  --tenant TENANT --oauth-client-id vaelius-plugin --enrollment ENROLLMENT \
  --callback-uri http://127.0.0.1:CALLBACK_PORT/callback
vaelius-client --home /absolute/private/client-profile enroll-project \
  --root /absolute/project --name PROJECT
vaelius-client --home /absolute/private/client-profile backend-check
vaelius-client --home /absolute/private/client-profile install --codex-home /absolute/codex-home
```

Use the exact callback registered with the operator. Installation manages only
its owned hook handlers and MCP registration. Existing unrelated agent settings
are retained. Trust hooks through the host's normal mechanism.

The direct Streamable HTTP MCP endpoint is `/mcp` on the configured backend. A
local credential helper supplies current authentication headers through the host's
credential pipe; do not log or paste its output. The backend owns the tool catalogue
and authorization. The `agentnetwork_memory` registration name remains a compatibility
identifier for existing configurations; it does not select another server.

```sh
vaelius-client --home /absolute/private/client-profile capture
vaelius-client --home /absolute/private/client-profile capture-status
vaelius-client --home /absolute/private/client-profile outbox-drain --max-events 32 --max-seconds 5
vaelius-client --home /absolute/private/client-profile credential-renew
vaelius-client --home /absolute/private/client-profile pause
vaelius-client --home /absolute/private/client-profile resume
vaelius-client --home /absolute/private/client-profile uninstall
```

## Hook commands and the project folder

Codex and Claude Code run the installed hook and MCP header-helper commands with
the project folder as the working directory. Installers write them as
`python -P -m agentclient...`: `-P` stops Python from looking for modules in that
folder, so only the installed client runs.

Installations made by `vaelius-client` 0.3.0, and by development builds before
this change, wrote these commands without `-P`. In those installations a project
containing its own `agentclient` package could run its code in place of the
client, with this profile's credentials. Affected: the Codex installation
(`install`) and the Claude Code settings installation (`install --claude-code`).
The Claude Code plugin is not affected: it runs the `vaelius-client` console
script, which does not import from the working directory.

Re-run the install command you used; it rewrites only the entries this profile
installed, as recorded in its receipt, and keeps everything else:

```sh
vaelius-client --home /absolute/private/client-profile install --codex-home /absolute/codex-home
vaelius-client --home /absolute/private/client-profile install --claude-code --scope user
vaelius-client --home /absolute/private/client-profile install --claude-code --scope project \
  --project-root /absolute/project
```

(For Claude Code this also adds the `standard` guardrail profile if none is
installed; add `--guardrails none` to leave guardrails out.) Until then, a session
started by an older hook command shows a one-line status naming the command to
run. Only the real client can show that line: in a folder whose own package
replaces the client, that package runs instead and nothing is shown. Re-run the
install rather than waiting for the message.

## Claude Code

Claude Code uses the same client profile, outbox and credentials as Codex. Sign in
and enroll projects first (above); nothing is captured outside an enrolled project.
Use one of the two installation methods below for a folder, not both: where a
settings installation covers a folder, the plugin's hooks stay idle.

### Plugin

The repository is a Claude Code plugin marketplace; the plugin is in
[`integrations/claude-code`](../integrations/claude-code). It runs the installed
`vaelius-client` (found on `PATH`, or named by `VAELIUS_CLIENT`) and installs or
downloads nothing itself. Set `VAELIUS_CLIENT_HOME` in the environment that starts
Claude Code when the profile is not at the default location.

```sh
claude plugin marketplace add seankerman/vaelius
claude plugin install vaelius@vaelius --config server_url=https://memory.example.org
```

Install the matching `vaelius-client` release first so that `vaelius-client` is on
`PATH`, for example with `pipx install` of the release wheel.

`server_url` is the plugin's **Vaelius service URL**; without `--config`, Claude Code
asks for it (`/plugin configure vaelius@vaelius`). Enter the profile's service URL
exactly: the header helper releases credentials only to that profile's own `/mcp`
endpoint and refuses any other URL. To try a checkout without a marketplace, start
Claude Code with `claude --plugin-dir /absolute/checkout/integrations/claude-code`.

The plugin registers a `vaelius` HTTP MCP server bound to the project of the folder
Claude Code started in, as the hooks resolve it: an enrolled project or a
[repository scope file](#repository-scope-files), including a `read-only` one. Its tools are named
`mcp__plugin_vaelius_vaelius__<tool>`. Claude Code runs a plugin's header helper
without environment variables whose names contain `KEY`, `TOKEN`, `SECRET`,
`PASSWORD` or `AUTH`, so AWS sign-in must come from a named AWS profile (as set up
by `aws sso login --profile NAME`), not from `AWS_*` key variables.

### Settings installation

Without the plugin, the client adds its own hook handlers and MCP entries to
Claude Code's configuration. It changes only entries it installed, keeps every
unrelated setting, hook and MCP server, backs up each file before its first change,
and records what it wrote in the profile's `claude-code-installation.json`.

```sh
vaelius-client --home /absolute/private/client-profile install --claude-code --scope user
vaelius-client --home /absolute/private/client-profile install --claude-code --scope project \
  --project-root /absolute/project
```

`--scope user` writes the hooks to `~/.claude/settings.json` (or to the folder named
by `CLAUDE_CONFIG_DIR` or `--claude-config-dir`). `--scope project` writes them to the
project's `.claude/settings.local.json`, which is personal and not committed; that
root must be enrolled or hold a repository scope file. Every MCP connection is bound
to one project, so both scopes register a `vaelius` HTTP MCP server for each enrolled
project root (and each root a scope file has enrolled) in Claude Code's per-project
("local") MCP configuration in `~/.claude.json`. Run the command again after
enrolling another project or upgrading Python; it is idempotent. An existing
`vaelius` server that this profile did not install is never replaced. Tools are named
`mcp__vaelius__<tool>`. Run it while Claude Code is closed, since Claude Code
rewrites `~/.claude.json` while it runs, then start Claude Code again.

The same command also writes the `standard` guardrail profile into that settings
file unless one is already installed there; see
[least privilege for agents](#least-privilege-for-agents). Pass `--guardrails strict`
for the repository-only profile or `--guardrails none` to leave guardrails out.

It also switches Claude Code's built-in auto memory off in each of those project
roots; see [auto memory](#claude-code-auto-memory).

### What is captured

The hooks handle `SessionStart`, `UserPromptSubmit`, `PostToolUse`,
`PostToolUseFailure`, `Stop`, `StopFailure`, `PostCompact` and `SessionEnd`. Each
prompt, tool call with its result, and final reply of a turn is redacted locally and
queued in the outbox; Claude Code's `prompt_id` identifies the turn. A `StopFailure`
records only the error category, marking a turn that has no reply. Calls to the
Vaelius MCP tools themselves, under either tool-name form, are never captured, so
retrieved memory is not fed back, but the sharing labels of what they return still
restrict the session ([origin labels](DATA_BOUNDARIES.md#origin-labels-and-session-taint)).
`SessionEnd` only delivers what is queued.
Automatic context is added on `SessionStart` and `UserPromptSubmit` as framed
reference data.

Status is shown to the person and never given to the model. Each session starts
with the capture ON/OFF line, plus the full notice the first time a project is
captured. When the profile is signed out the line says so and how to sign in again
(for AWS sign-in, `aws sso login --profile NAME`). Capture keeps queuing meanwhile
and is delivered by the first hook or `outbox-drain` after sign-in. While signed
out, the MCP header helper fails closed and Claude Code lists the server as failed.

### Removing it

```sh
claude plugin uninstall vaelius@vaelius
vaelius-client --home /absolute/private/client-profile uninstall --claude-code
```

`uninstall --claude-code` removes only this profile's Claude Code entries, guardrails
included (add `--scope user` or `--scope project` to remove one of them; `uninstall
--guardrails-only` removes just the guardrails) and leaves capture for other hosts
running. `uninstall` without it removes every host installation and
pauses capture. Neither deletes server records.

Capture of unenrolled folders is opt-in with `capture_all_sessions: true` in the
profile's `config.json`, which names projects `Repo:<name>:<id>` and `Chat:<id>`.
Profiles that still use the earlier `capture_all_codex_sessions` key keep working
and keep their existing `CodexRepo:`/`CodexChat:` project names.

### Remembered items

Ask Vaelius to keep something, in your own words: a preference, project context, a
reference or feedback, up to 8 KB. It is private to you until you share it.

```sh
vaelius-client remember add "Prefer pytest; run the focused test before the suite." --always-load
vaelius-client remember add - --title "Release checklist" < notes.md
vaelius-client remember list
vaelius-client remember add "Release on Thursdays." --replace <id>    # a new version
vaelius-client remember remove <id>
```

- **Where it is stored.** In this folder's workspace, or the one named by
  `--workspace` (you need the writer role there). `--personal` delivers it in every
  workspace, not only that one.
- **Always load.** `--always-load` items are delivered with your preferences at
  session start and with recall, in sessions whose audience admits them. For a
  private item that is your private sessions (`vaelius-client session use private`,
  a private workspace or a private sharing setting). To have one load in a shared
  session, share it: `vaelius-client sharing --set shared --source <source id>`
  (the source id is in `remember list`).
- **In Claude Code.** Ask the agent to remember something: it calls the MCP tool
  `remember`, shows you a preview and applies it only with the preview's token after
  you agree. `list_remembered` and `forget_remembered` list and remove. These are
  Vaelius tools, so they are never captured.
- **Your command is the review.** It needs only the `ingest` action, not `curate`.
  Each add, new version and removal is audited.

### Claude Code auto memory

Claude Code's auto memory (`~/.claude/projects/<project>/memory/`, whose `MEMORY.md`
loads at session start) is outside the Vaelius access model: a narrowed or private
session can write notes there that a later shared session in the same repository
then loads, unlabelled, and captures for its audience. So:

- `install --claude-code` writes `autoMemoryEnabled: false` into each enrolled or
  scoped project's `.claude/settings.local.json` (personal, not committed). The
  receipt records the earlier value and `uninstall --claude-code` restores it while
  the key still holds `false`; if you turn it back on yourself, a later install
  leaves it on and says so (`auto_memory_left_on`). CLAUDE.md files still load.
- Both managed-settings templates set `autoMemoryEnabled: false`. Claude Code's
  documentation says the key "has no managed-only lock" and that the environment
  variable `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (`1` off, `0` on) wins over the key for
  a session; it does not say whether that variable can override a managed `false`.
- A captured session starts with one line for you when auto memory is still on in
  that repository (`"auto_memory_notice": false` in the profile's `config.json`
  silences it). A worktree has its own `.claude/settings.local.json`, so a new agent
  worktree starts with auto memory on unless the managed setting covers it.
- Reading an auto-memory folder makes the rest of the session private for capture.
  Other Claude Code state for a project (tool results, task output, session files)
  follows the folder it belongs to, as before. An `autoMemoryDirectory` setting
  that moves the folder elsewhere is not recognised by this rule.

Import what is already there as remembered items:

```sh
vaelius-client import-memory                       # this repository's folder: counts only
vaelius-client import-memory --from ~/.claude/projects/<project>/memory --apply
```

Every entry becomes one item. `type: user` and `type: feedback` entries are
"always load"; `project` and `reference` entries are not. A folder that maps to an
enrolled or scoped repository is stored in that workspace; any other folder's items
are personal and need `--workspace`. The dry run (the default) prints counts, never
contents; `--apply` writes them, and retries do not duplicate. Source files are never
changed or deleted.

### Least privilege for agents

Capture can only redact what an agent sees. The guardrails stop the agent in a
captured project from reading the person's own files in the first place, so
nothing from them reaches a transcript or shared memory. They are Claude Code
settings: plugins cannot ship permission or sandbox settings (a plugin's
`settings.json` applies only `agent` and `subagentStatusLine`), so the client
writes them into a settings file. Capture-side origin labelling is a separate,
later layer; it does not depend on these settings.

```sh
# standard (the default): plugin users add --guardrails-only to skip the hooks
vaelius-client --home /absolute/private/client-profile install --claude-code --guardrails-only
# strict, for one project
vaelius-client --home /absolute/private/client-profile install --claude-code --guardrails-only \
  --guardrails strict --scope project --project-root /absolute/project
# remove
vaelius-client --home /absolute/private/client-profile uninstall --guardrails-only
```

The installer follows the rules of the settings installation above: it adds or
changes only the entries it owns, merges with existing rules instead of replacing
them, backs up the file before its first change, records each entry and each value
it changed in `claude-code-installation.json`, and restores the earlier value on
removal unless someone has changed it since. Running it twice gives identical files.
Switching profiles takes out the entries the old profile owned. The profiles as
written are in [`integrations/claude-code/guardrails`](../integrations/claude-code/guardrails).

| | `standard` (default) | `strict` (opt-in) |
| --- | --- | --- |
| Intent | Normal engineering work is not hindered | The agent works inside the repository only |
| Claude's file tools (Read, Grep, Glob, LSP, Edit, Write, NotebookEdit) | Refused in personal folders and on credential files | Refused everywhere outside the working directories (`permissions.blockReadsOutsideWorkingDirectories`), plus the `standard` refusals |
| Bash | Refused when the command names a personal folder or searches the home folder; Claude Code refuses its recognized file commands on credential files | Runs in the OS sandbox: no reads of the home folder or other user-file roots except the working directories, package caches and git configuration; writes only in the project, the session temp folder and the caches; no unsandboxed fallback or retry |
| Edits outside the project | Normal Claude Code prompts | Refused |
| Changes to agent configuration (`~/.claude/CLAUDE.md`, settings, skills, agents, commands, hooks, output styles, reference, `~/.claude.json`, Codex `config.toml`, `hooks.json`, `AGENTS.md`) | Asks you first, in every interactive mode, Bash included | Asks for Bash; file-tool edits are refused (outside the project) |
| Agent sign-in files (`~/.claude/.credentials.json`, `~/.claude.json`, `~/.codex/auth.json`) | Refused, Bash included | Refused, Bash included |
| `aws`, `gh`, `git` over SSH, `cdk`, package managers | Work as before; they read their own configuration | Commands that read configuration under the home folder fail (for example `aws configure get region`, git over SSH on macOS) |
| MCP servers | Not restricted | Not restricted |

Personal folders are `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Pictures`,
`~/Movies`, `~/Music`, `~/Videos`, `~/Library/Mobile Documents` (iCloud Drive),
`~/Library/CloudStorage`, `~/Library/Mail` and `~/Library/Messages`. A project that
lives inside one of them, such as `~/Documents/work/repo`, stays fully usable; its
sibling folders do not. Credential files are `~/.aws/credentials`, the AWS SSO and
CLI caches, `~/.ssh/id_*`, `~/.netrc`, `~/.git-credentials`,
`~/.config/gh/hosts.yml`, `~/.docker/config.json`, `~/.npmrc`, `~/.pypirc` and
`~/.kube/config`. Configuration such as `~/.aws/config`, `~/.ssh/config` and
`~/.gitconfig` stays readable.

**Agent configuration and sign-in files.** Some files outlive the session that
writes them: `~/.claude/CLAUDE.md`, `~/.claude/settings.json` and
`settings.local.json`, the `skills`, `agents`, `commands`, `hooks`, `output-styles`
and `reference` folders under `~/.claude`, `~/.claude.json`, and Codex's
`~/.codex/config.toml`, `hooks.json`, `AGENTS.md` and `AGENTS.override.md`. A change
there loads into every later session, captured or not, so both profiles add an
`ask` rule for each (`Edit(~/.claude/CLAUDE.md)`; an `Edit` rule also covers Write,
and `Write(...)` rules never match). Claude Code prompts for an explicit ask rule in
every mode, `bypassPermissions` and `auto` included. The guardrail hook answers `ask`
for a Bash command that writes one of them: an output redirection, `tee`, `sed -i` or
`perl -i`, `cp`, `mv`, `install` or `ln` onto them (or into their folder), and `rm`,
`truncate` or `touch`, including inside `bash -c` and heredocs. Code that names one of
these paths inside a quoted script asks too; plain reads (`cat`, `grep`) do not. This
is the one place the guardrails ask instead of deciding: such writes are rare (about
3 sessions in 130 over two weeks). `~/.claude/plans/` has no rule, because plan mode
writes there, and there is no rule for the whole of `~/.claude`. With
`CLAUDE_CONFIG_DIR` or `CODEX_HOME` set, the hook checks the moved folders too; the
rules name the default locations.

Agent sign-in files are never read: `~/.claude/.credentials.json` (Claude Code's
token file on Linux and Windows, and the macOS fallback), `~/.claude.json` (account
details and personal MCP server configuration, often with tokens) and
`~/.codex/auth.json`. Each gets a `Read` deny rule, and the hook refuses them for
every tool, Bash included (`jq . ~/.claude.json` is not one of the commands Claude
Code recognizes) and refuses searches of a folder that holds one, such as
`grep -r token ~/.claude`. `~/.claude.json` also carries an ask rule, but a deny rule
wins and Claude Code's `Read` deny also refuses edits of that path, so in practice
the agent can neither read nor change it. Reading the other configuration is allowed
and does not make a session private
([origin labels](DATA_BOUNDARIES.md#what-an-event-touched)).

Known gaps, from Claude Code's documentation (checked 2026-10-30):

- Claude Code documents that writes under `.claude` are "routed to the classifier" in
  `auto` mode, but not how the classifier decides; the ask rules still force a prompt.
- Its documentation lists `Edit` allow and deny rules for Bash redirection and `tee`
  targets, not ask rules, so Bash writes are covered only by the guardrail hook. A
  program that writes a file without naming it on the command line is not seen.
- A hook's `ask` is not documented as prompting in `bypassPermissions` mode (ask
  rules are). In `dontAsk` mode and in non-interactive runs (`-p`), calls that would
  prompt are refused instead.
- Codex's own hooks cannot ask, so these rules protect Codex's files from Claude
  Code sessions only, not from a Codex session.

**How `standard` is built, and why.** Credential files get `Read` and `Edit` deny
rules. Claude Code applies those to its file tools and to the file commands it
recognizes in Bash (`cat`, `head`, `tail`, `sed`, `tee` and redirections), but not
to programs that open files themselves, which is what lets `aws` keep reading
`~/.aws/credentials` while `cat ~/.aws/credentials` is refused. Personal folders
cannot use deny rules: a deny rule always wins, so `Read(~/Documents/**)` would also
lock out a project inside `~/Documents`, and a `!` exception cannot reopen part of a
`~/` rule. A `PreToolUse` hook (`vaelius-client guardrail-check`, matched on those
tool names) decides them instead, with the project as the exception. The same hook
applies the [session label](#session-labels) read rule; it reads the client profile
(`--home`) but never creates it and never calls the service. `standard`
does not turn on the Bash sandbox. Its network isolation breaks git over SSH on
macOS, database clients and connections to local servers, and its read limits apply
to every child process of a command. So the sandbox cannot tell `cat
~/.aws/credentials` from `aws s3 ls` reading the same file, and Claude Code also
copies `Read` deny rules into the sandbox's read denylist.

**What `standard` does not cover.** It is a denylist, so anything outside the lists
stays readable, for example `~/notes` or `/Volumes`. Its Bash check reads the
command text: it finds paths given as arguments, inside quoted code, after `cd`,
with `~`, `$HOME` or a glob, and searches that start at the home folder. A program
that opens a personal file without naming it on the command line still can,
for example a script in the repository (checked live: `python3 read_probe.py`
printed a `~/Documents` canary), and so can a path assembled at run time. For
credential files, Bash coverage is only Claude Code's recognized commands. `python3
-c "open('…/.aws/credentials')"` or `cp ~/.aws/credentials .` can still read them,
by design, because the tools that need those files must keep working. A command
that only mentions a personal path in text, such as a commit message, is refused.
If the check itself fails, `standard` reports the failure and lets the call run.

**`strict` notes.** It needs Claude Code 2.1.257 or later. It turns the sandbox on
with `failIfUnavailable`, so Claude Code does not start where the sandbox can't run:
native Windows, or Linux without `bubblewrap` and `socat`. Sandboxed commands can
still read system locations outside the user-file roots, such as `/tmp`, `/etc` and
`/opt`. Network access is Claude Code's sandbox default: new hosts prompt in Manual
mode and are allowed in `bypassPermissions` mode. Hooks, MCP servers and commands
you type at the `!` prompt run outside the sandbox. The deny rules also cover the
settings file that holds the profile and the project's `.claude/settings*.json`, so
the agent cannot switch it off mid-session. A repository's `.claude/settings.json`
outranks your user settings, so it could set `sandbox.enabled: false` against a
user-scope `strict`. Install `strict` at project scope (the local file outranks the
repository's) or deploy it as managed settings.

Both profiles hold in every permission mode, `bypassPermissions` included: Claude
Code applies deny rules and `PreToolUse` hook denials in all modes, and prompts for
ask rules in every interactive mode. To let Claude
read a personal folder for one session, start Claude Code with
`VAELIUS_GUARDRAIL_ALLOW=/absolute/folder` (several folders separated by `:`). This
never opens credential files. The agent cannot set it, because hooks get Claude
Code's environment, not the environment of the commands it runs.

At session start, the status line (shown to the person, never to the model) adds
one line when capture is on in a project and no guardrails are found. The check
looks for the guardrail hook, or for both the read fence and the sandbox, in the
managed settings files, your user settings and the project's settings files. Policy
delivered only by an MDM profile or server-managed settings is invisible to that
check; set `"claude_code_guardrail_notice": false` in the client profile's
`config.json` to silence it.

At session start the client also names the in-scope folders, the current root and
scoped folders next to it (`vaelius: in-scope folders: ...`), for the person and as
context for the agent; set `"scope_context_notice": false` to turn that line off
([scoped folders](DATA_BOUNDARIES.md#scoped-folders)).

#### Enforce for an organisation

An administrator can deploy the profile as managed settings, which users and
repositories cannot override. Deny rules and hooks from managed settings always
apply. Two templates are provided: [`managed-settings.json`](../integrations/claude-code/guardrails/managed-settings.json)
(`standard`) and [`managed-settings-strict.json`](../integrations/claude-code/guardrails/managed-settings-strict.json),
which also sets `disableBypassPermissionsMode`. The installer never writes them,
because that needs administrator rights. Deploy one as a drop-in next to any
existing policy, for example
`/Library/Application Support/ClaudeCode/managed-settings.d/50-vaelius-guardrails.json`
on macOS or `/etc/claude-code/managed-settings.d/50-vaelius-guardrails.json` on Linux
and WSL. On Windows, use `C:\Program Files\ClaudeCode\managed-settings.d\`. An MDM
configuration profile for the `com.anthropic.claudecode` domain, or server-managed
settings, works the same way. The hook command expects `vaelius-client` on the
`PATH` of every user. Without it, the hook fails and Claude Code continues, so the
deny rules (and under `strict`, the read fence and sandbox) still apply, but the
personal-folder check does not. Do not add `sandbox.filesystem.allowManagedReadPathsOnly`
to the strict template: with it, the read fence no longer reaches sandboxed commands.
Users can still add `allowRead` paths for sandboxed commands in their own settings,
but Claude's file tools keep refusing those paths.

Sources (Claude Code documentation, checked 2026-10-23; agent configuration rules
2026-10-30):
[permissions](https://code.claude.com/docs/en/permissions),
[settings reference](https://code.claude.com/docs/en/settings-reference),
[settings precedence](https://code.claude.com/docs/en/settings),
[sandboxing](https://code.claude.com/docs/en/sandboxing),
[permission modes](https://code.claude.com/docs/en/permission-modes),
[hooks](https://code.claude.com/docs/en/hooks),
[managed settings](https://code.claude.com/docs/en/managed-settings),
[plugins reference](https://code.claude.com/docs/en/plugins-reference),
[hooks guide](https://code.claude.com/docs/en/hooks-guide),
[authentication](https://code.claude.com/docs/en/authentication),
[the `.claude` directory](https://code.claude.com/docs/en/claude-directory); Codex
[hooks](https://learn.chatgpt.com/docs/hooks),
[AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) and
[authentication](https://learn.chatgpt.com/docs/auth).

## Credentials

Provider OAuth is the default for new enterprise logins. Its configured authorization
server owns token lifetimes, renewal and revocation. Reauthenticate in the same
profile with `--reauthenticate`; projects, sessions and outbox are preserved.
See [enterprise authentication](ENTERPRISE_AUTH.md) for setup and migration.

AWS IAM Identity Center sign-in (`--auth aws-sts --aws-profile NAME`, with the
`vaelius-client[aws]` extra) reuses the current AWS SSO session instead of a
browser login. It stores only a short-lived Vaelius access credential and renews it
by signing a new STS request. `vaelius-client credential-status` reports
`signed_in` or `signed_out` without contacting AWS or the service. While signed
out of AWS, capture keeps queuing in the outbox; run `aws sso login` to resume. See
[enterprise authentication](ENTERPRISE_AUTH.md#aws-iam-identity-center-sign-in).

Explicit legacy broker login stores a one-hour access token and a refresh token. Hooks, transports and
the MCP header helper only ever read the access token; the refresh token is used
solely by `credential-renew` (also run automatically before requests when less
than five minutes remain) and by `logout`. Each renewal rotates both tokens. The
service revokes the whole session if a used refresh token is ever presented again,
so do not copy a profile between machines. A refresh token expires after 30 idle
days and at most 90 days after login (operator configurable). Migrating to provider
OAuth supports subsequent login in the existing profile.

Tokens are kept in the OS keychain (for example macOS Keychain or the Linux
Secret Service) when the optional `keyring` package is installed with a usable
backend (`pip install keyring`). Otherwise they are owner-only (0600) files beside
the profile's `credential` path: `credential` and `credential.refresh`. The choice
is made at login (`--credential-store auto|keyring|file`) and recorded in the
profile; a keychain profile fails closed rather than falling back to files.
Profiles created before refresh tokens existed keep their file and need one new
login when their access token expires.

```sh
vaelius-client --home /absolute/private/client-profile logout
```

`logout` (alias `credential-revoke`) revokes the profile's token family at the
service, then deletes the local tokens. If revocation cannot be confirmed the
tokens are kept for a retry; `--local-only` deletes them without contacting the
service, leaving the session valid until its idle timeout.

## Repository scope files

A repository can declare how Vaelius treats it with a checked-in TOML file named
`.vaelius` at its root:

```toml
workspace = "example-repo"   # the Vaelius workspace name (access-model)
capture = "on"               # on | read-only | off
# sharing = "private"        # optional: owner-only; omit to inherit the workspace audience
```

The access model (`docs/ACCESS_MODEL.md`) decides who can read a capture from
the workspace's **access list** (people, groups and the optional organization
entry), not from a per-item sharing ladder. Writing `workspace =` is the
explicit opt-in: items inherit the workspace audience unless the person opts
them out as private. The service requires migration 048 (PR A on the server);
clients that send `workspace` and `remote` against an earlier service are
rejected with 400 `invalid_general_fields` for the extra fields.

Legacy keys are still read so existing repositories keep working:

| Key | Mapping |
| --- | --- |
| `project` | Treated as the workspace with that id. If `workspace` is also present and names a different id, the file is invalid and fails closed (capture and recall off) exactly like any other invalid file. |
| `sharing` | A request and therefore an upper bound. In a file **without** `workspace` keep today's meaning on the wire (`private` -> omitted, `team` -> `team`, `organization` -> `organization`). In a file **with** `workspace`, only `sharing = "private"` is honored (sent as omission); any other value is ignored with a one-time warning. When not private the client sends `sharing = "organization"`, which the service reads as "inherit the workspace audience". |
| `originals` | Ignored with a one-time warning and never sent on the wire. Sharing originals is now an explicit `vaelius-client share --originals` grant to a specific principal or group. |
| `mode` | Legacy names map as before: `default` -> `on`, `read-only` -> `read-only`, `disabled` -> `off`. |

| `capture` | Capture | Automatic recall |
| --- | --- | --- |
| `on` (default) | yes | yes |
| `read-only` | no | yes |
| `off` | no | no |

The hook finds the file by walking up from the session's working directory and
stops at the Git root (a directory holding `.git`) or the filesystem root. A linked
worktree whose checkout lacks the file uses the same place in the main worktree, so
every worktree of a repository resolves to the same request. Without a scope file
nothing changes: an explicitly enrolled project captures privately, and any other
folder is neither captured nor recalled.

A scope file at the home folder, at any folder above it (including `/`), directly in
a top-level personal folder (`~/Documents`, `~/Desktop`, `~/Downloads`,
`~/Pictures`, `~/Movies`, `~/Music`, iCloud Drive and the cloud-drive roots) or
anywhere inside the mail and chat stores never activates capture. Discovery skips
it and keeps walking up to the Git root or the filesystem root, so a stray
`~/.vaelius` does not turn capture on for every folder under home. The person sees
a one-time status line naming the ignored file; the agent is never told. A scope
file deeper down, such as `~/Documents/proj/.vaelius`, works normally.

A scope file naming a project enrolls its root automatically. Like `enroll-project`
this is forward-only: nothing earlier is imported. The enrollment is recorded in
the profile's `scope-enrollments.json`, never in `config.json`, so the file stays
authoritative: delete it or set `capture = "off"` and capture stops.

Precedence, from strongest:

1. `pause` and `capture_disabled_projects` in the profile.
2. Explicit enrollment (`enroll-project`, mapped sessions, their worktrees) names
   the project. A scope file naming the same project, or no project, sets the
   capture mode and sharing request there; a file naming a different project is
   ignored. Explicitly mapped desktop sessions use the file found from the cwd
   recorded in their transcript; read-only or off records a capture gap instead.
3. A scope file alone.
4. The explicit capture-all option, for folders with neither.

An unreadable or invalid scope file fails closed: no capture and no recall there.

`sharing` is a **request** and never grants anything: the workspace's access
list decides. In a workspace file only `private` (owner only) is honored and is
sent as omission on the wire. A workspace file without `sharing` captures with
`sharing = "organization"` on the wire, which the service reads as "inherit the
workspace audience"; the server never promotes it past the access list. In a
legacy (`project =`) file `sharing` keeps today's meaning on the wire, so an
unmigrated repository captures exactly as it did before. See the
[privacy model](PRIVACY_MODEL.md) and
[shared conversation passages](DATA_BOUNDARIES.md#shared-conversation-passages-and-originals).

### Repository remote on every capture

Every capture now carries a normalized Git `remote` (`host/path`, lower case,
no scheme/user/port, no trailing `.git` or `/`) alongside `workspace`. The
client reads it from the scope root's Git remotes: the one named `origin`, else
the only remote, else none (a scoped folder that is not a Git repository, or a
repository with several remotes and no `origin`, has none, which is still
accepted on the person's writer role alone). The server rejects a capture
whose remote is not linked to the named workspace with HTTP 409
`repo_not_linked`; the client keeps that event queued and the status line
names the admin fix, for example:

```
vaelius: example.com/example-org/maple is not linked to workspace "maple" --
a workspace admin can run `vaelius-client workspace link maple
example.com/example-org/maple` (the event stays queued).
```

The admin fix retries about every fifteen minutes, so a lasting mismatch does
not keep retrying forever and the queued events ship on the next successful
drain. A parts receipt may also carry an `notice` (for example when an admin's
first capture links a remote automatically); it is shown on the status line
once per receipt. Minimum service version: migration **048** (PR A). Earlier
services reject the extra `workspace` and `remote` fields with 400
`invalid_general_fields`.

### Your own sharing setting

Your setting is `contributions_default`, the state new items of yours are
captured in. It has two values under the access model (`shared` means items
inherit the workspace audience; `private` means owner only). New principals
start at `shared`; a delegated credential cannot change it:

```sh
vaelius-client --home /absolute/private/client-profile sharing                       # show it
vaelius-client --home /absolute/private/client-profile sharing --set shared          # inherit the workspace audience
vaelius-client --home /absolute/private/client-profile sharing --set private         # only you
```

`--set team` and `--set organization` are kept as deprecated aliases of
`shared`. With `--set private --apply-to-existing` the client first fetches a
preview with a `confirm_token`, so your agent must show the preview and get
your agreement first, then applies with `--confirm <token>` (or `--yes` to
preview and apply in one step). Only you can change it, and each change is
audited; the session status line shows it.

### Your own content: find, delete, re-share and share

Everything below acts only on content you created (sources you captured, or that a
delegated service captured for you), needs no operator, and works from the command
line or by asking your agent (the MCP tools in brackets).

| Command | What it does | Needs |
| --- | --- | --- |
| `mine` (`list_my_memories`) | Lists your sources, or with `--sessions` your capture sessions, with each source's current sharing level and which setting holds it there | `read` |
| `forget` (`forget_memories`) | Deletes a session, chosen sources, everything of yours in a project, or a date range | `withdraw` |
| `sharing --set LEVEL` with a selection (`change_memory_sharing`) | Changes the stored level of those sources | `withdraw` to lower, `share` to raise |
| `share` / `unshare` (`share_memories`, `unshare_memories`) | Shares a session or sources with one person or one project's members, and revokes it; `share --list` (`list_memory_shares`) lists shares you made and received | `share` |

Select content with `--session current` (the session captured most recently in this
folder's project: "what we just did"), `--session HANDLE` (from `mine --sessions`)
or a host conversation ID, `--source ID` (repeatable), `--project NAME`, and
`--since` / `--until` (UTC dates or ISO times):

```sh
vaelius-client --home /absolute/private/client-profile mine --sessions
vaelius-client --home /absolute/private/client-profile mine --sharing shared
vaelius-client --home /absolute/private/client-profile forget --session current
vaelius-client --home /absolute/private/client-profile forget --session current --confirm TOKEN
vaelius-client --home /absolute/private/client-profile forget --project maple --until 2026-01-01 --yes
vaelius-client --home /absolute/private/client-profile sharing --set private --session current --yes
vaelius-client --home /absolute/private/client-profile share --session current --with-group maple-team
vaelius-client --home /absolute/private/client-profile share --source SOURCE --with-principal alice --originals --yes
vaelius-client --home /absolute/private/client-profile unshare --source SOURCE --with-principal alice
```

Grantees are now a principal (`--with-principal NAME`) or a group
(`--with-group NAME`); `--with-project` has been dropped by the access model.
The selection form of `--sharing` and `--set` takes `shared|private` (and the
old `team`/`organization` names stay as aliases of `shared`).

### Workspaces and groups

Workspace and group management move from the operator CLI to the client:

```sh
vaelius-client --home /absolute/private/client-profile workspace list
vaelius-client --home /absolute/private/client-profile workspace create maple --item-default shared
vaelius-client --home /absolute/private/client-profile workspace show maple
vaelius-client --home /absolute/private/client-profile workspace settings maple --name maple --item-default private
vaelius-client --home /absolute/private/client-profile workspace set-entry maple \
    --subject-kind principal --subject alice --role admin
vaelius-client --home /absolute/private/client-profile workspace set-entry maple \
    --subject-kind organization --role reader
vaelius-client --home /absolute/private/client-profile workspace remove-entry maple \
    --subject-kind principal --subject alice
vaelius-client --home /absolute/private/client-profile workspace link maple \
    https://example.com/Example-Org/Maple.git
vaelius-client --home /absolute/private/client-profile workspace unlink maple \
    example.com/example-org/maple
vaelius-client --home /absolute/private/client-profile group list
vaelius-client --home /absolute/private/client-profile group create maple-team --workspace maple
vaelius-client --home /absolute/private/client-profile group show maple-team
vaelius-client --home /absolute/private/client-profile group add-member maple-team \
    --member-kind principal --member bob --role member
vaelius-client --home /absolute/private/client-profile group remove-member maple-team \
    --member-kind principal --member bob
vaelius-client --home /absolute/private/client-profile group delete maple-team
```

Workspace-admin operations need the `manage` credential action (`vaelius:manage`
on provider OAuth; a credential issued before migration 048 that holds `share`
also holds `manage`). A delegated credential cannot carry it. See
`docs/ACCESS_MODEL.md` sections 2, 3 and 8 for the full access list, group
source and role model.

`forget`, `sharing` with a selection and `share` change nothing on their own: they
print a preview (how many sources, sessions and projects, what is held, what would
be capped) and a `confirm_token`. Run the same command with `--confirm TOKEN`, or
add `--yes` to preview and apply in one step. A selection that changed in between
is previewed again instead of applied. The MCP tools work the same way: they need
`confirm`, which must be `false` (preview) or the preview's token, so your agent
has to show you the preview and get your agreement first. These tools are never
captured back into memory.

- **Deleting** purges the sources, everything derived from them and their
  originals, exactly as the service's own deletion does. Content under a legal hold
  is hidden and preserved instead, and the result counts it as `held`. Content
  captured after you forget a session is captured as usual; pause capture to stop it.
- **Lowering** a level hides the content on the next search. **Raising** applies
  to the selected sources only, is never stored above the content's origin (content
  recalled from a more restricted place stays as restricted), and is still capped
  at every search by your own setting and the project, tenant and connection
  ceilings; the preview names the ceiling that will hold it lower. An operator can
  restrict raising and new shares tenant-wide; lowering, deleting and revoking
  always work.
- **Sharing with people** names one principal or the members of one project. They
  can then read those passages even where your sharing level would not reach them,
  in every project they search, and, with `--originals` and their own `source_read`
  action, the originals. A session share also covers what is captured later in that
  session. Revoking takes effect on the next search. Every change and share is
  audited. See [the privacy model](PRIVACY_MODEL.md#owner-controls).

### GitHub identity link

When your organization syncs GitHub teams into Vaelius, a team's access counts for
you only after you prove you own your GitHub account:

```sh
vaelius-client --home /absolute/private/client-profile github link
# Open https://github.com/login/device and enter the code WXYZ-1234 to link your GitHub account.
vaelius-client --home /absolute/private/client-profile github status
vaelius-client --home /absolute/private/client-profile github unlink
```

`link` asks the service to start a GitHub device-flow authorization, prints the
one-time code and address, and waits while you approve in the browser (up to 15
minutes). The device code and the GitHub token stay with the service; it reads
your numeric GitHub user id and login once, stores those, and discards the token.
Typing a login never links one. A renamed GitHub login keeps its link, because the
link is by user id. One GitHub account links to one person per organization;
running `link` again with another account replaces the link. `unlink` removes it,
and your GitHub team memberships stop counting at once. A settings administrator
can unlink someone else with `github unlink --principal NAME`. Linking, relinking
and unlinking are audited, and offboarding removes the link.

To link while enrolling, add `--link-github` to the enrollment command; if the
link step fails, the enrollment stands and the output says to run
`vaelius-client github link` later.

### Which projects recall reaches

A Claude Code session that starts in a workspace folder sends its **session label**
with every recall (automatic context, and through the MCP connection's
`X-Vaelius-Session-Label` header, `search_memory` and the evidence, context and
history tools). The service then returns only items whose audience contains the
session's: the current workspace first, then every other workspace at least as wide
(an organization workspace, a workspace the same team can read), each marked
`"from": "<workspace> · <author> · <date>"`. Your own private items are recalled only
in a private session. The cross-project `scope = "all"` option is gone from the MCP
tool: the label decides. Clients and sessions without a label keep the earlier
behavior (the current project, then organization-shared items; `"scope": "all"` in an
HTTP search request still widens it).

### Session labels

A session takes its label when it starts and keeps it: `cd` does not change it,
compaction and `--resume` keep it, `/clear` or a new conversation starts over.
Sessions that start read-only, off or outside every workspace have no label.

- **Reads of narrower workspaces are denied.** The guardrail hook refuses folders
  whose workspace fewer people can read than the session's, workspaces you cannot
  read, and folders with a scope file that does not parse, with one line such as
  `vaelius: denied — ~/src/birch (workspace "birch") is visible to fewer people than
  this session (maple). Start a session there, or ask the person to run
  \`vaelius-client session use birch\`; capture is then limited to people in both.`
  It never asks. Unscoped folders stay readable and make the session private.
- **Narrowing is explicit.** `vaelius-client session use birch` (or a folder of it,
  a linked remote, or `private`) narrows the session active in this folder; with
  several sessions, pass `--session <id>`. In Claude Code you can also ask the agent
  to call the MCP tool `use_workspace`. The service checks that you can read the
  target. The session then reads and recalls as `maple ∩ birch`, every later capture
  carries `birch`, and the status line says
  `vaelius: session uses maple ∩ birch — capture limited to people in both`.
  Narrowing lasts until `/clear`. `vaelius-client session status` shows the label.
- **The descriptor.** At session start the client fetches which workspaces the
  session may read (`POST /enterprise/v3/workspaces/descriptor`) and keeps it in the
  profile (`workspaces/<id>.json` and `session-labels.json`, 0600), never in the
  scope file. It is refreshed after 15 minutes at the next prompt, and at once when
  a search reports a newer epoch. After 24 hours without a refresh only the
  session's own workspaces are readable. If you become a reader, capture turns off
  with a status line. The hook itself never calls the service (measured p50 about
  65 ms per call, most of it Python start-up).
- **The MCP connection.** Claude Code runs the MCP header helper once per connection
  (at session start and on reconnect) and gives it no session ID. The hooks therefore
  record the label of the session last active in each scoped folder
  (`mcp-labels/<hash>.json` in the profile, 0600; removed when that session ends),
  and the helper sends it, so a private or narrowed session's `search_memory` reaches
  what it may read. The record can only narrow the folder's own label (same
  workspace and sharing request, more narrowed workspaces, or private), never widen
  it; a record older than 12 hours, for another workspace or unreadable is ignored.
  A narrowing reaches the connection when it next connects (`/mcp`, then reconnect).
  With two sessions in one folder the last one to start or prompt wins: the other
  may recall as the narrower label, and the taint of what it recalls then limits its
  own capture accordingly.

```sh
vaelius-client --home /absolute/private/client-profile scope-status --cwd /absolute/project/sub/dir
```

### Content from outside the repository

In a labelled session this is the backstop for what the guardrail hook cannot see
(programs that open files without naming them, other MCP servers, unscoped
folders); an allowed read of another workspace only adds it to what captures carry.
Each captured tool event is labelled with what it touched, and the label sticks to
the rest of the session: once an agent reads a personal folder, a credentials file,
a repository without a scope file, or uses an MCP server other than Vaelius, that
event and everything after it in the session is captured as private. Reading another
repository that has its own scope file tags the session with that project instead,
so the service shares it only with members of both. Shell commands are judged by the
paths they name; `aws`, `gh`, `git` and similar commands that name no outside path do
not restrict. A new session or `/clear` starts clean; compaction does not. The person
sees one line when it happens, for example
`vaelius: this session read personal files (~/Documents/...) — capture is now private`.

`capture` can be a table to allow specific places:

```toml
[capture]
mode = "on"
allowed_paths = ["../shared-fixtures"]
allowed_mcp_servers = ["docs"]
```

Rules, guards on the allowlists, what is sent and the known limits are in
[origin labels and session taint](DATA_BOUNDARIES.md#origin-labels-and-session-taint).

### Legacy scope file names

`scope_files` in the profile's `config.json` lists the names to look for, in
priority order (default `[".vaelius"]`). Operators migrating from an earlier
product can add its name, for example `"scope_files": [".vaelius", ".mindshare"]`.
`.vaelius` must be TOML. Another name may also be plain `key: value` or
`key = value` lines (simple YAML); only these top-level fields are mapped and
everything else (tags, team, search filters, nested keys) is ignored:

| Field | Mapping |
| --- | --- |
| `workspace` | workspace name (new; takes precedence over `project`) |
| `project` | legacy alias of `workspace`; disagreement is invalid |
| `capture` | as above |
| `mode` | `default` → `on`, `read-only` → `read-only`, `disabled` → `off` (used when `capture` is absent) |
| `sharing` | workspace file: `private` honored, else inherit; legacy file: as before |
| `originals` | ignored, with a one-time warning on the status line |

A legacy file without `workspace` or `project` only adjusts an explicitly
enrolled project.

## Capture notice and status

Login and `enroll-project` print the organization's capture notice: what is
captured (prompts, assistant replies, tool calls and results), who can read it under
the service's policy, your own sharing setting and how widely passages in that
project can be shared at most, and how to pause or withdraw. The hook shows the full
notice the first time a project is captured and one capture ON/OFF status line,
including your sharing setting, at the start of every session. Displays are recorded in the profile's `notice-acknowledgements.json`.

```sh
vaelius-client --home /absolute/private/client-profile notice --project PROJECT --acknowledge
```

Automatic context is framed as reference data, not instructions, and each item
carries its author, project, capture time and original/derived wording. A
rate-limited capture (HTTP 429) stays in the outbox until the service's
`Retry-After`. See [shared-memory governance](MEMORY_GOVERNANCE.md).

Desktop capture is forward-only at enrollment. Capture is an explicit finite poll;
installation does not start a recurring collector or import past conversations.
Pausing or uninstalling does not delete server records; use `vaelius-client forget`
(above) to delete what was captured.

`vaelius-client` defaults to `~/.local/share/vaelius/client`. The old `agentclient`
alias retains its old default for compatibility. Supplying `--home` always selects
that explicit profile. An upgrade never copies credentials or redirects an existing
profile to a new service. Back up profile/host configuration before an explicit
cutover. [Capture contract](../packages/server/docs/CONSOLIDATION.md).
