# Enterprise identity and authorization

Vaelius is an OAuth resource server. The operator-configured authorization server
owns browser login, MFA, account recovery, access/refresh issuance, refresh reuse
detection and revocation. The client uses authorization code with S256 PKCE and
RFC 9207 issuer-response checking. It never handles a user's password.

Vaelius uses fresh RFC 7662 introspection at request authentication and final
delivery. Tokens must have the configured audience, permitted client, current
expiry, subject and `vaelius:*` scopes. The token's issuer is only a routing hint
into the operator allowlist; it never chooses an arbitrary URL. There is no positive
token-validity cache. Provider outages deny access. Discovery is cached for five
minutes, with fixed-origin endpoints and no redirect/proxy fallback.

An operator binds `(issuer, subject, tenant)` to an existing principal. Email never
grants membership. PostgreSQL stores only access-token digests and application
scope/expiry metadata. The backend never receives a provider refresh token. Current
principal, tenant, delegation and source/document permissions remain canonical;
indexed `search_permissions` and restricted search-reader roles are unchanged.
Directory offboarding must update Vaelius principal/binding/group grants and the
identity provider. Logging out does not withdraw previously retained sources or
remove an independently authorized backend service's processing grants.

## Operator configuration

Run tenant/control migrations and application-role grants before serving. Use a
mounted owner-only secret for the confidential introspection client. Configure:

```json
{"authorization": {
  "resource": "https://memory.example.org/mcp",
  "providers": [{
    "issuer": "https://identity.example.org/realms/company",
    "tenant": "company",
    "client_ids": ["vaelius-plugin"],
    "introspection_client_id": "vaelius-api",
    "client_secret_file": "/absolute/private/introspection.secret",
    "actions": ["read", "source_read", "ingest", "feedback"]
  }]
}}
```

The issuer must support authorization-code/PKCE, discovery, refresh, revocation
and authenticated introspection. Introspection must report `active`, `sub`, `aud`,
`client_id`, `exp` and `scope`. Rejecting missing claims is intentional. JWT issuers
can serve multiple configured tenants; an opaque token without a known digest
route requires one unambiguous configured issuer. Bind each issuer to an explicit
tenant; do not infer the tenant from an email or caller header.

For Keycloak, configure a public `vaelius-plugin` client with PKCE required, exact
loopback callback, disabled password/direct grant and scopes `vaelius:read`,
`vaelius:source_read`, `vaelius:ingest`, `vaelius:feedback`. Include scopes in the
token. Use two audience mappers: the Vaelius resource URI and the confidential
`vaelius-api` introspection client. Include the subject in introspection. Configure
refresh rotation (`revokeRefreshToken`, max reuse zero), short access lifetime,
session idle/absolute limits and company federation/MFA in the identity server.
SAML and upstream OIDC federation are broker responsibilities. Production uses
TLS, a service UID, persistent identity-server storage/backups and managed secrets;
the disposable development fixture below is not a production deployment.

## Plugin enrollment and migration

```sh
python -m agentclient.cloud_enroll --home /absolute/private/profile \
  --url https://memory.example.org --tenant company --enrollment laptop \
  --oauth-client-id vaelius-plugin --credential-store keyring \
  --callback-uri http://127.0.0.1:59001/callback
```

New CLI logins use provider OAuth by default. An explicit `--broker` selects legacy
login for an unmigrated deployment; configuring provider auth disables legacy
interactive issuance unless the operator explicitly enables `legacy_interactive`.
Explicit service credentials still use the canonical service identity path.

Append `--reauthenticate` to log in again or explicitly migrate an existing profile
in place. URL, tenant and enrollment must match. The same principal/actor retains
project/session enrollment, cursors, outbox and installation settings. A different
account cannot take over the existing enrollment. Migration grants no new project
or visibility. No live profile is migrated on install or startup.

Verified OS keychain backends are preferred; `--credential-store keyring` fails
closed if unavailable. `auto` selects a verified built-in OS backend or clearly
reported owner-only files. Plaintext/remote third-party keyring backends are not
accepted as an OS keychain. Managed-device policy should require the keychain.
The short-token renewal window is proportional to remaining lifetime.

Provider-returned token pairs are journaled before local replacement. A saved
reply recovers without repeating refresh. An ambiguous lost reply requires browser
reauthentication; blindly replaying a spent token could revoke the provider session.
The provider may omit refresh credentials; access then expires normally and the
user reauthenticates in the same profile.
`credential-renew` and `logout` use provider token/revocation endpoints. Logout only
deletes local tokens after confirmed provider revocation, unless `--local-only` is
explicitly selected. It does not delete knowledge.

MCP advertises `/.well-known/oauth-protected-resource/mcp`; tenant-specific login
metadata is at `/.well-known/oauth-protected-resource/mcp/TENANT`. Invalid tokens
receive a 401 discovery challenge. The installed plugin's credential helper also
works with this same provider path; it contains no alternative corpus or service.

Before rolling back to a binary without provider authentication, run the explicit
operator fence `agenthub.oauth_provider.prepare_legacy_rollback(store)` against
each selected tenant using its operator store. This deactivates provider-token
digests and records an audit event; it preserves sources, permissions, directory
state and service credentials. Never re-enable those digests from a backup. Then
disable the provider configuration and restore the previous installed binary.
New provider login is required after a later upgrade. Test this on disposable
schemas before changing a live deployment.

## Reproducible local verification

Build/install wheels, start disposable PostgreSQL/S3 with `scripts/dev_services.py`,
then use its API port as the resource URI:

```sh
python scripts/identity_fixture.py start --state /absolute/private/identity-fixture \
  --resource http://127.0.0.1:PORT/mcp
VAELIUS_IDENTITY_FIXTURE=/absolute/private/identity-fixture/identity.json \
  python scripts/test.py --installed --suite server --pattern test_oauth_provider.py \
  --services /absolute/private/services/services.json
python scripts/identity_fixture.py stop --state /absolute/private/identity-fixture
```

The fixture creates synthetic accounts and random private credentials outside Git.
Its loopback-only browser driver uses the password grant solely for disposable
administrator provisioning; the plugin login is an actual authorization-code/PKCE
flow. Never point the fixture or destructive tests at a user/customer deployment.

References: [OAuth security BCP](https://www.rfc-editor.org/rfc/rfc9700.html),
[introspection](https://www.rfc-editor.org/rfc/rfc7662.html),
[native applications](https://www.rfc-editor.org/rfc/rfc8252.html),
[MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization).
