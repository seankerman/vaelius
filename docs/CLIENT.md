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

## Credentials

Provider OAuth is the default for new enterprise logins. Its configured authorization
server owns token lifetimes, renewal and revocation. Reauthenticate in the same
profile with `--reauthenticate`; projects, sessions and outbox are preserved.
See [enterprise authentication](ENTERPRISE_AUTH.md) for setup and migration.

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

Desktop capture is forward-only at enrollment. Capture is an explicit finite poll;
installation does not start a recurring collector or import past conversations.
Pausing or uninstalling does not delete server records; use backend lifecycle tools.

`vaelius-client` defaults to `~/.local/share/vaelius/client`. The old `agentclient`
alias retains its old default for compatibility. Supplying `--home` always selects
that explicit profile. An upgrade never copies credentials or redirects an existing
profile to a new service. Back up profile/host configuration before an explicit
cutover. [Capture contract](../packages/server/docs/CONSOLIDATION.md).
