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

### What is captured

The hooks handle `SessionStart`, `UserPromptSubmit`, `PostToolUse`,
`PostToolUseFailure`, `Stop`, `StopFailure`, `PostCompact` and `SessionEnd`. Each
prompt, tool call with its result, and final reply of a turn is redacted locally and
queued in the outbox; Claude Code's `prompt_id` identifies the turn. A `StopFailure`
records only the error category, marking a turn that has no reply. Calls to the
Vaelius MCP tools themselves, under either tool-name form, are never captured, so
retrieved memory is not fed back. `SessionEnd` only delivers what is queued.
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

**How `standard` is built, and why.** Credential files get `Read` and `Edit` deny
rules. Claude Code applies those to its file tools and to the file commands it
recognizes in Bash (`cat`, `head`, `tail`, `sed`, `tee` and redirections), but not
to programs that open files themselves, which is what lets `aws` keep reading
`~/.aws/credentials` while `cat ~/.aws/credentials` is refused. Personal folders
cannot use deny rules: a deny rule always wins, so `Read(~/Documents/**)` would also
lock out a project inside `~/Documents`, and a `!` exception cannot reopen part of a
`~/` rule. A `PreToolUse` hook (`vaelius-client guardrail-check`, matched on those
tool names) decides them instead, with the project as the exception. `standard`
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
Code applies deny rules and `PreToolUse` hook denials in all modes. To let Claude
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

Sources (Claude Code documentation, checked 2026-10-23):
[permissions](https://code.claude.com/docs/en/permissions),
[settings reference](https://code.claude.com/docs/en/settings-reference),
[settings precedence](https://code.claude.com/docs/en/settings),
[sandboxing](https://code.claude.com/docs/en/sandboxing),
[permission modes](https://code.claude.com/docs/en/permission-modes),
[hooks](https://code.claude.com/docs/en/hooks),
[managed settings](https://code.claude.com/docs/en/managed-settings),
[plugins reference](https://code.claude.com/docs/en/plugins-reference).

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
project = "example-repo"   # the service project name
capture = "on"             # on | read-only | off
sharing = "private"        # private | team | organization  (redacted passages)
originals = "private"      # private | team | organization  (unredacted originals)
```

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

`sharing` and `originals` are **requests**. They are sent with each captured event,
and the service caps them by your own sharing setting (below), the operator's
project and tenant ceilings and the connection's visibility, so editing the file
can never widen access beyond what you and the operator allow. Originals are never
shared above passages. See the [privacy model](PRIVACY_MODEL.md) and
[shared conversation passages](DATA_BOUNDARIES.md#shared-conversation-passages-and-originals).
`private` is never sent, so older services keep working for private capture; a
service before migration 045 rejects `organization` and `originals`.

### Your own sharing setting

Your setting is the most any of your captured content is shared, in every project
and whatever a repository asks for. It starts at `private`:

```sh
vaelius-client --home /absolute/private/client-profile sharing                       # show it
vaelius-client --home /absolute/private/client-profile sharing --set team            # project members
vaelius-client --home /absolute/private/client-profile sharing --set organization    # everyone in your organization
vaelius-client --home /absolute/private/client-profile sharing --set private         # only you
```

Lowering it hides your already shared content on the next search. Raising it
applies to what is captured from then on; content captured while it was lower
stays at that level. Only you can change it (a delegated credential cannot), and
each change is audited. The session status line shows it.

### Which projects recall reaches

Automatic context and `search_memory` return memory from the current project
first. Content other projects shared with the whole organization follows as a
separate tier; each such item says where it came from
(`"from": "<project> · <author> · <date>"`). Other projects you belong to are
searched only when you ask: the MCP tool's `scope = "all"`, or `"scope": "all"` in
an HTTP search request.

```sh
vaelius-client --home /absolute/private/client-profile scope-status --cwd /absolute/project/sub/dir
```

### Legacy scope file names

`scope_files` in the profile's `config.json` lists the names to look for, in
priority order (default `[".vaelius"]`). Operators migrating from an earlier
product can add its name, for example `"scope_files": [".vaelius", ".mindshare"]`.
`.vaelius` must be TOML. Another name may also be plain `key: value` or
`key = value` lines (simple YAML); only these top-level fields are mapped and
everything else (tags, team, search filters, nested keys) is ignored:

| Field | Mapping |
| --- | --- |
| `project` | project name |
| `capture` | as above |
| `mode` | `default` → `on`, `read-only` → `read-only`, `disabled` → `off` (used when `capture` is absent) |
| `sharing`, `originals` | as above |

A legacy file without `project` only adjusts an explicitly enrolled project.

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
Pausing or uninstalling does not delete server records; use backend lifecycle tools.

`vaelius-client` defaults to `~/.local/share/vaelius/client`. The old `agentclient`
alias retains its old default for compatibility. Supplying `--home` always selects
that explicit profile. An upgrade never copies credentials or redirects an existing
profile to a new service. Back up profile/host configuration before an explicit
cutover. [Capture contract](../packages/server/docs/CONSOLIDATION.md).
