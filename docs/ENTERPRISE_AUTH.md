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
principal, tenant, delegation and workspace/source/document permissions remain
canonical; the indexed `search_permissions` audience filter and the restricted
search-reader role are unchanged. Directory offboarding must update Vaelius
principals and their workspace entries, local-group memberships and bindings, and
the identity provider. Logging out does not withdraw previously retained sources or
remove an independently authorized backend service's processing grants.

[Access reviews and offboarding](ACCESS_REVIEW.md) covers the periodic access
report, reviewer attestations, idle-access hygiene and the offboarding command.

## Actions and route authorization

Every authenticated route requires an action, checked right after authentication
and again by the operation itself. A route that is not in the service's action
table is refused, so a new route cannot ship without being classified. Missing
actions answer `404`, the same as a missing resource.

| Route | Requires |
|---|---|
| `GET /enterprise/v1/status` (MCP `memory_status`) | `read` |
| Search, document detail, evidence, context, timeline, project history, preferences, original-document describe/list/download | `read` (originals also need `source_read` and ownership or a raw grant) |
| `GET /enterprise/v1/sources/*`, `GET /enterprise/v2/sources/*` | `source_read` |
| `GET /enterprise/v1/explain/*` | `read` or `settings` |
| `POST /enterprise/v1/sources`, `/v2/parts`, original-document upload | `ingest` |
| `POST /enterprise/v1/boundary` (compaction boundary) | `ingest` |
| `POST /enterprise/v1/receipts` (delivery receipts) | `ingest` and `read` |
| `POST /enterprise/v3/feedback` (MCP `report_memory_outcome`) | `feedback` and `read` |
| `POST /enterprise/v1/reviewed-notes` (MCP `submit_memory_candidate`), `POST /enterprise/v3/preferences/curate` | `curate` |
| `POST /enterprise/v1/lifecycle` | `correct`, `withdraw` (also for `delete`) or `policy`, by operation |
| `POST /enterprise/v3/preferences/withdraw` | `withdraw` |
| `POST /enterprise/v3/my-data` (MCP `list_my_memories`): the caller's own sources and sessions | `read` |
| `POST /enterprise/v3/my-data/forget` (MCP `forget_memories`) | `withdraw` (own sources only) |
| `POST /enterprise/v3/my-data/sharing` (MCP `change_memory_sharing`) | `withdraw` or `share`; raising needs `share`, a non-delegated credential and a tenant that allows owner sharing |
| `POST /enterprise/v3/my-data/grants` (MCP `list_memory_shares`) | `read` |
| `POST /enterprise/v3/my-data/grant` (MCP `share_memories`) | `share` or `policy` (own sources only; not delegated with `share`) |
| `POST /enterprise/v3/my-data/revoke` (MCP `unshare_memories`) | `share`, `withdraw` or `policy` (own grants only) |
| `POST /enterprise/v3/moderation/withdraw` | `manage` and the workspace `admin` role (credentials issued before migration 048 that hold `moderate` are still accepted) |
| `POST /enterprise/v3/workspaces`, `/workspaces/show`, `/groups`, `/groups/show` | `read` |
| `POST /enterprise/v3/workspaces/create`, `/groups/create` | `manage` |
| `POST /enterprise/v3/workspaces/settings`, `/entries/set`, `/entries/remove`, `/repos/link`, `/repos/unlink`, `/groups/members/add`, `/groups/members/remove`, `/groups/delete` | `manage` and the workspace `admin` role, or `settings` as a settings administrator (audited as `workspace_admin_override`) |
| `POST /enterprise/v3/tenant-policy` | `settings` and the tenant settings administrator flag |
| `POST /enterprise/v2/release` | `policy` and `read` |
| Directory events and SCIM | `settings` (and the tenant administrator flag) |
| `GET /enterprise/v1/processing-status` | `audit` |
| `GET /enterprise/v3/service-status` | `audit` or `settings` |
| Access review | `audit` or `policy` (and the tenant administrator flag) |

Four routes are open to any authenticated credential, because each acts only on
the presented credential or returns text meant for every user:

- `GET /enterprise/v3/auth/credential` reports the presented credential (tenant,
  principal, enrollment, actions, expiry). Every credential needs it to schedule
  its own renewal.
- `POST /enterprise/v3/auth/rotate` replaces the presented access token with one
  that has the same actions and no later expiry.
- `POST /enterprise/v3/auth/adopt` binds the presented provider token to an
  enrollment of its own principal; one held by another principal is a conflict.
- `POST /enterprise/v3/capture-notice` returns the capture notice; reader detail is
  returned only for a connection the caller owns.

A malformed request body is reported (`400`) only after authentication; an
unauthenticated caller gets the same `401`/`404` for any route.

### `share`, `manage` and `moderate`

`share` lets a person clear the `private` flag on their own sources and grant
them to a named principal or group
([owner controls](PRIVACY_MODEL.md#owner-controls)). It is part of default
interactive enrollment and of the default provider and AWS sign-in scopes
(`vaelius:share`), together with `withdraw`, which the owner needs to delete
sources and to make them private. Credentials and refresh sessions issued
before migration 047 that hold `withdraw` also hold `share` (`legacy_actions`
bit 2); their stored action lists are not rewritten. Delegations cannot
include `share`. An operator restricts it tenant-wide with
`enterprise_organizations.owner_sharing = 'lower_only'`, which keeps lowering,
deleting and revoking.

`manage` creates and administers workspaces. It gates every mutating
`/enterprise/v3/workspaces/*`, `/groups/*` and `/repos/*` route; the business
method also checks the workspace `admin` role (or the settings-admin
break-glass override). It is part of default interactive enrollment and of the
default provider and AWS sign-in scopes (`vaelius:manage`). The AWS STS
defaults are `feedback, ingest, manage, read, share, source_read, withdraw`.
Credentials and refresh sessions issued before migration 048 that hold `share`
(explicitly or through the pre-047 `withdraw`-implies-`share` legacy bit) also
hold `manage` (`legacy_actions` bit 4), following the pattern of bits 1 and 2;
stored action lists are not rewritten. **Delegations never carry `manage`**:
a delegated credential cannot create or administer a workspace.

`moderate` lets a workspace admin withdraw (hide) anyone's sources in their
workspace; it never deletes and never reads originals. **Moderation now
follows the admin role.** A caller needs the workspace `admin` role and a
credential holding `manage`. `moderate` is no longer part of default
enrollment or default provider scopes; credentials and refresh sessions issued
before migration 048 that still hold `moderate` keep working (the route
accepts either `manage` or `moderate` at the action gate, and the business
method's role check runs after). It cannot be delegated and is reported as
privileged in [access reviews](ACCESS_REVIEW.md).

### Delegations are workspace-scoped

`enterprise_delegations.projects` lists the **delegated workspaces** (the ids
are unchanged by migration 048; see
[access-model section 8](ACCESS_MODEL.md#8-roles-and-credential-actions)). A
delegated credential acts only in those workspaces, with its stored actions ∩
the role the **represented person** holds in each workspace, and never through
the organization entry. The `manage` and `share` actions are never carried,
and raising sharing is refused for delegated credentials.

### SCIM groups

Directory groups pushed over SCIM become `scim` groups in Vaelius
(`groups.source = 'scim'`), with group id `scim-` + the first 24 hex of a
SHA-256 of the external id. A SCIM group awaiting reconcile has
`sync_state = 'stale'` and **contributes nobody** to `group_closure` until the
next good sync, so a sync outage never leaves a removed person in. Nesting a
SCIM group inside a `local` group is how a workspace carries a directory
mapping: migration 048 produces one `<project>-members` local group per
workspace, with each SCIM group that was mapped to that project nested inside
it.

### No GitHub sync

Vaelius takes nothing from GitHub: there is no team sync and no identity link
(both were removed in migration 054). Repository permissions are not memory
audiences. Groups are `scim` (above) or `local`.

### Settings-admin break-glass override

A tenant settings administrator (the `settings_admin` flag plus the `settings`
action) can manage any workspace's access list, repository links, settings,
and groups through the `settings` alternative on the `workspace/*` and
`groups/*` routes. Each such change is audited as `workspace_admin_override`.
The override never grants content access; adding themselves as a reader is an
ordinary, audited entry change. Tenant policy itself (`workspace_creation`,
`widest_entry`, `owner_sharing`) is set here on
`POST /enterprise/v3/tenant-policy`, and is also audited.

### `feedback` and `curate`

`feedback` (outcome reports) and `curate` (reviewed notes and preference curation)
are enforced as their own actions. Previously `read` was enough for feedback and
`correct` for curation. Upgrading keeps existing access working: migration
`043_action_grants.sql` marks every credential, refresh session and delegation that
exists when it runs, and those grants keep the implied abilities (`read` implies
`feedback`, `correct` implies `curate`). Their stored action lists are not
rewritten, so renewal still reports the same actions and clients' scope checks keep
passing. Grants issued after the upgrade state their actions explicitly:

- Interactive enrollment without operator `enrollment_actions` now grants
  `ingest, read, feedback, correct, withdraw, share, manage`. Add `curate` there
  (or to a delegation) for users who should submit reviewed notes; `curate` is
  reported as privileged in [access reviews](ACCESS_REVIEW.md).
- Provider OAuth tokens need the `vaelius:feedback` scope to report outcomes (the
  plugin already requests it) and `vaelius:curate` to curate.
- Delegations may now include `curate`. Updating a delegation makes its action list
  explicit.
- `/enterprise/v1/status` needs `read`, and compaction boundaries and delivery
  receipts need `ingest`. A read-only credential's hooks record these as capture
  gaps and continue.

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

The introspection secret file is re-checked at most every 30 seconds and re-read
only when its resolved inode, size or modification time changes. Rotation therefore
needs no restart. A missing, empty or group/world-readable replacement is ignored,
and the last valid secret stays in use until a valid file appears. The value is
never logged. Rotate with an overlap: enable the new client secret at the issuer,
update the mounted file, wait at least 30 seconds, then retire the old secret.
Two common ways to mount it:

- Kubernetes: a `Secret` volume with `defaultMode: 0400`, and `runAsUser` or
  `fsGroup` matching the service UID. Kubernetes swaps the files atomically.
  Mount the volume directory, not a `subPath`, because `subPath` mounts never
  receive updates.
- AWS Secrets Manager on Kubernetes: the Secrets Store CSI Driver with the AWS
  provider. Set `filePermission: "0400"` on the object, because the default `0644`
  is rejected. Install the driver with `enableSecretRotation=true` and a
  `rotationPollInterval`. The same `subPath` limitation applies.
- AWS Secrets Manager on ECS, which injects secrets only as environment variables:
  have a sidecar write the file with mode 0400 to a shared volume, replacing it
  atomically by writing a temporary file and renaming it.

The issuer must support authorization-code/PKCE, discovery, refresh, revocation
and authenticated introspection. Introspection must report `active`, `sub`, `aud`,
`client_id`, `exp` and `scope`. Rejecting missing claims is intentional. JWT issuers
can serve multiple configured tenants; an opaque token without a known digest
route requires one unambiguous configured issuer. Bind each issuer to an explicit
tenant; do not infer the tenant from an email or caller header.

For Keycloak, configure a public `vaelius-plugin` client with PKCE required, exact
loopback callback, disabled password/direct grant and scopes `vaelius:read`,
`vaelius:source_read`, `vaelius:ingest`, `vaelius:feedback`, `vaelius:withdraw`,
`vaelius:share` (the last two let people forget and share their own content)
and `vaelius:manage` (so they can create and administer their own workspaces);
the plugin requests the optional ones when offered. Include scopes in the
token. Use two audience mappers: the Vaelius resource URI and the confidential
`vaelius-api` introspection client. Include the subject in introspection. Configure
refresh rotation (`revokeRefreshToken`, max reuse zero), short access lifetime,
session idle/absolute limits and company federation/MFA in the identity server.
SAML and upstream OIDC federation are broker responsibilities. Production uses
TLS, a service UID, persistent identity-server storage/backups and managed secrets;
the disposable development fixture below is not a production deployment.

`actions` bounds what token scopes can grant. Omitting it maps every Vaelius scope
except `source_read`; list `source_read` (as above) only when users of that provider
should download retained originals, which still requires source ownership or an
explicit owner grant. Interactive enrollment without operator `enrollment_actions`
likewise omits `source_read`. See the
[raw-access policy](DATA_BOUNDARIES.md#raw-access-policy) for upgrade steps.

## Plugin enrollment and migration

```sh
python -P -m agentclient.cloud_enroll --home /absolute/private/profile \
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

MCP advertises `/.well-known/oauth-protected-resource/mcp`. Invalid tokens receive
a 401 discovery challenge. The installed plugin's credential helper also works with
this same provider path; it contains no alternative corpus or service.

Unauthenticated discovery does not reveal which tenants exist. Every well-formed
`/.well-known/oauth-protected-resource/mcp/TENANT` path returns the same
resource-wide document as `/mcp`, with status `200`, whether or not `TENANT` is
configured. It lists every configured issuer, and `vaelius_authorization_servers`
gives each issuer's own scopes. Clients learn their tenant, and their issuer when
the service has more than one, from the enrollment details their administrator
gives them:

```sh
python -P -m agentclient.cloud_enroll ... --tenant company \
  --oauth-issuer https://identity.example.org/realms/company
```

With a single configured issuer `--oauth-issuer` is optional. `--reauthenticate`
reuses the issuer the profile already signed in with. The tenant is confirmed after
sign-in: the service reports the token's tenant and the client refuses a mismatch.
Clients released before this change need exactly one advertised issuer.

Legacy interactive login (`/enterprise/v3/auth/begin` and `/complete`, used only
with `legacy_interactive` or without provider OAuth) is no tenant oracle either.
`begin` validates the broker name, then records the request in the control plane for
any well-formed tenant name and answers the same way. The tenant is looked up only
when a matching state returns, and every completion failure (unknown state, a state
begun for another tenant or broker, a rejected code, an unknown tenant, an unbound
identity) is the same `404`. At most 1,000 requests per tenant name and 20,000 in
total are pending; beyond that the oldest is dropped, so a flood delays logins but
never changes the response. Rate-limit these routes at the proxy as well. Run the
control-plane migrations (they add `cloud_login_requests`); the control
application role needs `INSERT` and `DELETE` on it, which the local profile setup
grants.

`/enterprise/v3/auth/revoke` answers `200 {"revoked": true}` for every token up to
16,384 characters, whether valid, unknown, expired or already revoked (RFC 7009),
so it does not reveal validity. Provider-issued access tokens are revocable there
too: the service stops accepting them, while the issuer still owns their lifetime.

Before rolling back to a binary without provider authentication, run the explicit
operator fence `agenthub.oauth_provider.prepare_legacy_rollback(store)` against
each selected tenant using its operator store. This deactivates provider-token
digests and records an audit event; it preserves sources, permissions, directory
state and service credentials. Never re-enable those digests from a backup. Then
disable the provider configuration and restore the previous installed binary.
New provider login is required after a later upgrade. Test this on disposable
schemas before changing a live deployment.

## AWS IAM Identity Center sign-in

Engineers who already sign in to AWS IAM Identity Center (AWS SSO) can use that
session as their Vaelius login, with no browser step. This follows the Vault AWS
auth method and aws-iam-authenticator pattern. It is optional, disabled unless
`authorization.aws_sts` is configured, and coexists with provider OAuth, legacy
login and explicit service credentials.

1. The client signs one `sts:GetCallerIdentity` request (SigV4) with the current
   credentials of a named AWS profile. The signature covers an
   `x-vaelius-server-id` header naming this service and a random nonce.
2. It posts the signed method, headers and body, never the AWS secret key, to
   `POST /enterprise/v3/auth/aws-sts`. A supplied URL is ignored.
3. The service builds the regional STS URL itself from the operator's region
   allowlist and forwards only the signed headers, without redirects or proxies.
   STS verifies the signature and returns the caller's ARN.
4. Only an `assumed-role` session in an allowlisted account and role is accepted.
   The role session name, which Identity Center sets to the user's username, is
   the external subject. An operator binding maps it to a principal.
5. The service issues a short-lived Vaelius access credential, stored only as a
   digest. There is no refresh token: the client signs a new request when the
   credential nears expiry. Logout revokes that credential.

```json
{"authorization": {
  "resource": "https://memory.example.org/mcp",
  "aws_sts": {
    "tenant": "company",
    "allowed_account_ids": ["111122223333"],
    "allowed_role_patterns": ["AWSReservedSSO_Developer_*"],
    "regions": ["us-east-1"],
    "token_ttl_seconds": 3600
  }
}}
```

| Setting | Default | Meaning |
|---|---|---|
| `tenant` | required | Tenant whose bindings and principals are used |
| `allowed_account_ids` | required | 12-digit AWS accounts whose sessions are considered (1–100) |
| `allowed_role_patterns` | `["AWSReservedSSO_*"]` | Case-sensitive glob patterns for the role name |
| `require_identity_center` | `true` | Also require an `AWSReservedSSO_` role. IAM users, root and federated users are always refused |
| `regions` | `["us-east-1"]` | Regional STS endpoints the service may call; China regions are not supported |
| `server_id` | origin of `resource` | Value the client must sign into `x-vaelius-server-id` |
| `issuer` | `aws-sts:identity-center` | Issuer recorded in bindings and credentials |
| `token_ttl_seconds` | `3600` | Vaelius credential lifetime, 60–43200 |
| `max_clock_skew_seconds` | `300` | Accepted difference from the signed `X-Amz-Date`, 30–900 |
| `actions` | `feedback, ingest, manage, read, share, source_read, withdraw` | Actions granted to issued credentials |

`providers` may be omitted when `aws_sts` is present. Unknown keys and invalid
values stop startup. Run control-plane migrations: they add the
`cloud_aws_sts_replay` table. The control application role needs `INSERT` and
`DELETE` on it, and the local profile setup grants both.

Bind each person explicitly, with the AWS provider allowed:

```python
store.identity_bindings.bind('aws-sts:identity-center', 'alice', 'company', 'alice',
                             allowed_providers=['aws-sts'])
```

A session name never grants membership on its own, and an unbound subject is
denied. Principal, tenant, binding and tenant-policy checks run again whenever the
credential is used, so deactivation applies immediately.
If the tenant has a policy (`set_tenant_policy`), its `allowed_providers` must
include `aws-sts`.

Security properties:

- **Replay.** A digest of each accepted signature is kept until STS would reject
  it as expired (15 minutes plus the permitted skew), and reuse is refused.
- **Cross-service replay.** A request signed for another server ID is refused
  before STS is called. The client signs its own `--url` origin by default, so a
  service that claims another's ID only receives requests naming itself.
- **SSRF.** The STS host comes only from the region allowlist, and the request's
  signed `Host` must equal it. Only expected headers are accepted, and the exact
  `GetCallerIdentity` body and action are required.
- **Allowlists.** Account, role pattern, assumed-role ARN shape and the
  consistency of `Account`/`UserId` with the ARN are all checked.
- **Fail closed.** If STS is unreachable or returns a malformed response, the
  service answers 503 and issues nothing. STS signature errors are denials.
- **Audit.** Issuance and denials for allowlisted accounts are recorded with the
  ARN, without signatures, tokens or AWS credentials. Requests from other
  accounts and requests rejected before STS write no rows, so unauthenticated
  traffic cannot grow the audit table.

Limits: STS returns no group claims, so workspace access still comes from
Vaelius access-list entries (direct, through a local or SCIM group, or through
the organization entry). Only people with AWS access can use this path.
Each sign-in and renewal needs STS to be reachable. A custom
`allowed_role_patterns` with `require_identity_center: false` accepts session
names chosen by whoever assumes that role. Use it only where the role's trust
policy pins the session name, such as `sts:RoleSessionName` set to
`${aws:username}`. Identity Center usernames are unique within one Identity
Center instance only. Do not allowlist accounts from different instances under
one `issuer`.

Client sign-in needs the optional extra `pip install 'vaelius-client[aws]'`:

```sh
aws sso login --profile developer
python -P -m agentclient.cloud_enroll --home /absolute/private/profile \
  --url https://memory.example.org --tenant company --enrollment laptop \
  --auth aws-sts --aws-profile developer --aws-region us-east-1
```

The profile stores the AWS profile name, region and server ID, plus the Vaelius
credential. It never stores AWS keys. When AWS credentials are missing or
expired, hooks keep queuing capture in the outbox. `credential-renew` and
`status` report `{"status": "signed_out", "reason": "aws_signed_out"}`.
After `aws sso login`, the next use signs in again automatically.

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
