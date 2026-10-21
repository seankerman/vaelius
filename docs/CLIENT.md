# Client installation and enrollment

Install `packages/client` from the matching Vaelius release. It has no backend or
model dependencies. The client stores private credentials, capture cursors and a
durable transport outbox; acknowledged payloads are removed and the outbox is never
searched. Original documents go through the backend document API.

The operator must configure the identity broker, enrollment and source connections
first. Login creates a private profile and enrolls no project automatically:

```sh
python -m agentclient.cloud_enroll \
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

`uninstall --claude-code` removes only this profile's Claude Code entries (add
`--scope user` or `--scope project` to remove one of them) and leaves capture for
other hosts running. `uninstall` without it removes every host installation and
pauses capture. Neither deletes server records.

Capture of unenrolled folders is opt-in with `capture_all_sessions: true` in the
profile's `config.json`, which names projects `Repo:<name>:<id>` and `Chat:<id>`.
Profiles that still use the earlier `capture_all_codex_sessions` key keep working
and keep their existing `CodexRepo:`/`CodexChat:` project names.

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
sharing = "private"        # private | team
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

`sharing = "team"` is a **request**. It is sent with each captured event, and the
service caps it by the operator's project policy and the connection's visibility,
so editing the file can never widen access beyond what the operator allows; see
[team-shared conversation passages](DATA_BOUNDARIES.md#team-shared-conversation-passages).
`private` is never sent, so older services keep working for private capture.

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
| `sharing` | as above |

A legacy file without `project` only adjusts an explicitly enrolled project.

## Capture notice and status

Login and `enroll-project` print the organization's capture notice: what is
captured (prompts, assistant replies, tool calls and results), who can read it under
the service's policy, and how to pause or withdraw. The hook shows the full notice
the first time a project is captured and one capture ON/OFF status line at the start
of every session. Displays are recorded in the profile's `notice-acknowledgements.json`.

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
