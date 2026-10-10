# Local identity and private preferences

The PostgreSQL profile uses authenticated issuer/subject bindings in its control
database, current principals/membership/delegation in the assigned tenant database,
and expiring plugin/service credentials. Email, source author text, and a login
hint never grant membership. The machine/service operator remains trusted in this
local rehearsal; customer/operator separation and real customer SSO remain pending.

## Login and enrollment

`cloud_identity.OIDCBroker` uses Authlib's OAuth2Session for authorization code
with S256 PKCE and joserfc for RS256 signature and issuer/audience/expiry validation.
Endpoints and allowed algorithm are operator configuration. Token fields do not
select keys or URLs. Callbacks must match the configured URI exactly; state and
nonce are persisted and consumed once. Pending login states are capped at 1,000
per tenant and expire after five minutes. Key refreshes are bounded even during
outages. A small issuing-clock skew is accepted, while explicit expiry stays strict.

The supported routes are:

- `POST /enterprise/v3/auth/begin`: configured tenant and broker identifier.
- `POST /enterprise/v3/auth/complete`: state, authorization code, exact callback
  URI and enrollment identifier. Scopes are server selected; no customer model
  or coding-agent credential is accepted.
- `POST /enterprise/v3/auth/renew`: refresh-token rotation. Body `refresh_token`,
  `replacement_access_token`, `replacement_refresh_token`; no Bearer token is used.
- `POST /enterprise/v3/auth/revoke`: RFC 7009 revocation of a token family.
- `POST /enterprise/v3/auth/rotate`: authenticated access-token rotation that never
  extends the token's expiry.

### Access and refresh tokens

Login (`auth/complete`) issues a short-lived access token (`credential`, for
compatibility) and a refresh token that starts a new token family. Only the access
token is accepted on API and MCP routes; only the refresh token is accepted by
`auth/renew`. Both are random 64-character values stored as SHA-256 digests; raw
tokens never reach the database, audit rows or logs. Operator-issued service
credentials (`enroll()`) get a family but no refresh token.

Lifetimes are operator configuration in the private `runtime.json`; omitted keys
keep their defaults:

~~~json
{"credentials": {"access_ttl_seconds": 3600, "refresh_idle_seconds": 2592000,
                 "refresh_absolute_seconds": 7776000}}
~~~

Access tokens last 1 hour by default (60 seconds to 24 hours). A refresh token
expires after 30 idle days and never later than 90 days after the original OIDC
login (up to 366 days; idle must exceed the access lifetime). Rotation cannot
extend either token past that absolute limit; afterwards the user logs in again.

Each renewal follows OAuth 2.0 Security BCP (RFC 9700, section 4.14.2) refresh-token
rotation: it spends the presented refresh token, deactivates the family's previous
access token and issues a new pair. The client generates both replacements and
journals them before dispatch, so a lost reply is retried with identical values;
that exact retry is answered again while its successor is unused. Any other
presentation of a spent refresh token is treated as theft: the whole family (every
access and refresh token) is revoked in the same transaction and a
`credential_refresh_reuse` audit row is written. Renewal also rechecks the tenant,
principal, represented user, delegation and external identity binding, so a
deprovisioned user cannot refresh; such a denial does not spend the token.

`auth/revoke` follows RFC 7009: the family is identified by the Bearer access token
or by a body `token` (refresh or access). Expired, spent or already revoked tokens
of the family still identify it, so the call is idempotent, and unknown tokens are
acknowledged with the same `{"revoked": true}`. Revocation is audited as
`credential_revocation` (`applied` or `already_revoked`).

Migration `033_refresh_tokens.sql` is additive. Access tokens issued before it
have no family: they keep working until their own expiry, are never exchanged for
a refresh token, and revoke only themselves. The first renewal after an upgrade
therefore requires one interactive login; bridging an existing access token into
a refresh family was rejected because it would let any copy of a one-hour token
mint a 90-day session. Re-run the application role grants after migrating, as for
other new tables. Sender-constrained tokens (DPoP, mTLS) are not implemented.

The tenant database is authoritative; each control route is bound separately, with
no cross-database atomicity claim. If route binding fails after a renewal commits,
the client's identical retry rebinds it.
Current credential, represented user, global control disable, tenant principal and
delegation checks run again at delivery/processing checkpoints. A settings
administrator has no implicit right to private documents.

`IdentityBindings.bind()` is an operator action; callers never join an organization
by matching an email. A binding can require a signed federation-provider alias and
reject native login. The local Keycloak mapper obtains the alias from the broker
user session, rather than a user-editable profile attribute.

An operator can set `IdentityBindings.set_tenant_policy(tenant,
federation_required=True, allowed_providers=['company'])`. The organization-wide
policy applies to future bindings and is checked again for existing verified
human credentials and in-flight delivery. Explicit operator service/bootstrap
credentials remain separately provisioned, scoped grants.

The model-free client entry point binds an exact loopback callback and runs the
complete server login exchange. It requires a new, explicit isolated home:

~~~sh
python -m agentclient.cloud_enroll --home /private/new-client-profile \
  --url http://127.0.0.1:5000 --tenant orchard --broker knowledge \
  --enrollment laptop --callback-uri http://127.0.0.1:59001/callback
~~~

The operator must configure that exact callback in the broker and its client.
The listener checks state, exact path/Host, duplicate parameters and replay. The
client verifies the server authorization URL uses code flow and S256 PKCE.
Credentials and configuration are owner-only; no token is printed. With
`--credential-store auto` (the default) tokens go to the OS keychain when the
optional `keyring` package has a usable backend, otherwise to 0600 files; see the
[client guide](../../../docs/CLIENT.md#credentials). Login adds no
projects, installs no global hooks, opens no local corpus and makes no client model
calls. Project/connection enrollment remains an explicit subsequent operation.
`--no-browser` prints the authorization URL for a manually opened browser. Timeout,
callback refusal and a failed browser launch do not activate capture.

## Directory lifecycle and source permissions

`normalize_scim()` accepts a bounded full-resource Users/Groups subset. A verified
directory user ID must first be bound to an existing tenant principal; a directory
group ID must be bound to a project. Normalized replacement/deactivation events
use a sequence and idempotency key. Conflicting keys fail, ambiguous older events
hold access until an explicit full-state reconciliation, and group removal removes
effective grants. Explicit operator membership remains separate from directory
grants. Group access never upgrades source visibility.

Use `bind_directory_user`, `bind_directory_group` and `apply_directory_event` as
operator integration interfaces. The event endpoint is
`POST /enterprise/v3/directory/events`. Directory operations require a current
settings-scoped credential for a settings administrator (including an explicitly
provisioned directory service principal). Passwords, roles, arbitrary extension
schemas, SCIM bulk and filter/PATCH operations are unsupported; this is not a claim
of complete SCIM interoperability. Customer-vendor synchronization remains pending.

The bounded core SCIM HTTP surface also supports GET, POST, PUT and DELETE at
`/enterprise/v3/scim/Users/<bound-id>` and `/enterprise/v3/scim/Groups/<bound-id>`.
Mutations require `X-Directory-Sequence` and `X-Idempotency-Key`. PUT replaces the
supported full resource; DELETE deactivates its grants. PATCH is explicitly refused.

Connection reader policy and per-source ACL freshness are distinct from connector
access. `set_source_acl_freshness` can hold stale/unknown policy and invalidate its
policy version. Current permission applies to retrieval, cached results, original
downloads, observer dispatch and installation. Backend processing validates the
current capture enrollment without requiring that an ingest-only plugin hold
unrelated read or correction privileges.

## Preference curation and delivery

Values, quotes and evidence are ordinary canonical `knowledge_revisions` and
support records. `cloud_preferences` stores only verified tenant/user identity,
key, applicability, revision and validity metadata. There is one knowledge corpus.
The original shared source retains its visibility; a derived personal preference
is private to its verified user, including when a scoped service acts for that user.

`curate_preference` validates an observer candidate against a server-attributed
UserPromptSubmit source. Quotes, another speaker's choice, unsupported provenance,
invented values/scopes and one-time instructions do not become durable defaults.
Generic conversation author strings require a verified speaker binding and are
held rather than treated as identity. Freeform candidates can use a grounded key
and value; user, project and explicit bounded task applicability are supported.

The existing durable observer schema gets a conservative automatic bridge for
test runner, programming language, formatting and response style. A validated,
model-selected canonical claim citing the source is required. Source regex alone
does not create knowledge. An authorization marker is written in the same
canonical installation transaction, withholding the prospective preference from
all users during the postcommit transition. The bridge installs a private canonical
preference and retires the shared derived preference claim; it retains the shared
original. A crash leaves a pending marker that denies delivery until recovery.
Other automatic preference families require extending the typed observer contract;
the bridge does not claim unrestricted natural-language preference extraction.

`POST /enterprise/v3/preferences` selects current user defaults for an authorized
project and optional task. Narrower defaults win, explicit current instructions
override stored defaults, and operator-configured organization requirements win.
The endpoint cannot install a requirement supplied by customer JSON. Requirements
are organization settings written through `set_required_preference` by a settings
administrator. This authority grants no private-content access.

Corrections preserve immutable revisions and close the previous validity interval.
Withdrawal disables the canonical document, derived indexes and offered receipts.
Influenced observers reconstruct their context. Automatic selection suppresses a
preference already stated in the same host conversation or offered in its current
context epoch. A trusted PostCompact hook advances the epoch and restores relevant
preferences. Explicit requests remain available. Project membership and original
source permissions are checked separately; a preference grants no extra source
access.

## Provider-free and broker checks

Use the milestone's private interpreter with the cloud extras installed. These
commands make zero model-provider calls. PostgreSQL integration creates and drops
only its own named synthetic test databases on the explicit local services profile:

~~~sh
PYTHONPATH=AgentHub:AgentClient python -m unittest discover -s AgentHub/tests -p test_cloud_identity.py
PYTHONPATH=AgentHub:AgentClient python -m unittest discover -s AgentHub/tests -p test_cloud_preferences.py
PYTHONPATH=AgentClient python -m unittest discover -s AgentClient/tests -p test_cloud_enroll.py
CLOUD_TEST_SERVICES=/private/cloud-readiness-v1/services.json \
CLOUD_BROKER_SETTINGS=/private/cloud-readiness-v1/identity-tests/broker.json \
PYTHONPATH=AgentHub:AgentClient python -m unittest discover -s AgentHub/tests -p test_cloud_identity_postgres.py
~~~

The actual local broker fixture uses Keycloak 26.7.4 with two synthetic realms:
`knowledge-local` has native accounts and brokers `company-local`. Setup is explicit
through `cloud_broker_fixture.prepare_local_broker(loopback_url, private_bootstrap_env,
private_output_json)`. Its passwords and client secret stay in the private output
file, with mode 0600. The helper drives only the synthetic loopback login forms,
emulating browsers' secure-cookie exception for loopback; it does not change real
OAuth transport. Native and federated flows use authorization code/PKCE, actual
signed token validation, HTTP enrollment/status and replay denial. A requests-driven
local form rehearsal does not prove every customer/browser/IdP flow or production
TLS, cookie, SAML, IAM and directory behavior.

The PostgreSQL suite additionally starts a real loopback ASGI listener and launches
the client enrollment module as a subprocess. Both native and federated fixture
logins deliver a real GET to the client's callback, complete through HTTP, store
private credentials and use the resulting credential for backend status. This
source subprocess receipt is separate from an installed-wheel receipt.

The PostgreSQL/ASGI suite also runs the actual canonical finite Worker with a
deterministic provider fixture, including ingest-only capture, private preference
promotion, current source access, correction, withdrawal, scope, override,
compaction, directory offboarding, rotation and commit-transition denial.
Installed-wheel, hook/MCP subprocess, ordinary-session and cloud acceptance receipts
are recorded separately in the superproject milestone status.
