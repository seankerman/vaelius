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
   owner, and to project members only when the source is team-shared under the
   operator's project policy ([below](#team-shared-conversation-passages)). Other
   principals see derived knowledge and its citations through the normal search policy.
2. **Retained originals** — the complete canonical source (`/enterprise/v2/sources/`,
   `/enterprise/v1/sources/`) and original document bytes (`source-documents/download`).
   Need the credential action `source_read` **and** source ownership or an explicit,
   active, owner-issued grant. Document metadata (`describe`, `list`) stays at tier 1.

`source_read` is no longer part of default enrollment (`/enterprise/v3/auth/complete`
without operator `enrollment_actions`) or of the default OAuth scope mapping (a
provider without an explicit `actions` list). Operators who want users to download
originals list it explicitly. Grants never bypass tenant, lifecycle, delegation or
connection-reader checks, and a grantee still needs `source_read`:

```sh
# Owner grants (requires the owner's credential with the policy action):
python -m agenthub.raw_access grant --profile /absolute/private/profile --tenant acme \
  --token-file /absolute/private/owner.token --scope-kind connection --scope-id team-documents \
  --grantee-kind project --grantee maple
python -m agenthub.raw_access revoke ...   # same arguments; the row is kept for audit
python -m agenthub.raw_access list --profile ... --tenant acme --token-file ...
```

A grant names one source or one connection, and either a principal or a project
(members of that project, which is how directory groups map into Vaelius).

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

## Team-shared conversation passages

By default a captured conversation passage is readable only by the person who
captured it, even on a team connection, so teammates see nothing of each other's
work unless curation runs. A repository can ask for more with a checked-in scope
file ([client side](CLIENT.md#repository-scope-files)):

```toml
# .vaelius at the repository root
project = "example-repo"
capture = "on"        # on | read-only | off
sharing = "team"      # private | team
```

The file is a **request, never a grant**: anyone who can edit the repository can
edit it. The service decides with operator policy.

| Step | Rule |
| --- | --- |
| Ceiling | Each project has `max_sharing` (`private` or `team`); a project without one inherits the tenant's `default_max_sharing`, which is `private` until the operator changes it. |
| Ingest | Effective sharing = the minimum of the request, the project ceiling at that moment and the capture connection's visibility (a `private` connection never shares). It is stored per source in `enterprise_sources.sharing`, next to `sharing_requested`. A capped request is audited (`sharing_request` / `capped_private`). |
| Query | A teammate can read a passage, its cited evidence and its conversation context only when every source it depends on is stored as `team`, the project's **current** ceiling is `team`, the reader is an active member of that project, and every ordinary source rule passes. |

Consequences:

- **Lowering** a ceiling (or the tenant default) hides team passages on the next
  request, including results already delivered (delivery is rechecked).
- **Raising** a ceiling never exposes a source that was stored as private, even if
  its scope file asked for team. Sources stored as team become visible again.
- Every existing protection still applies on top: tenant isolation, withdrawal and
  deletion, legal holds (a held deletion hides like a withdrawal), operator
  quarantine (held sources stay owner-only), delegation project limits, connection
  reader lists and freshness, and membership removal.
- **Retained originals do not change.** The complete source and original documents
  still need `source_read` and ownership or an explicit owner grant (above).

Who sees what for a passage Alice captured in project `maple`:

| Reader | Stored `private` | Stored `team`, ceiling `team` | Stored `team`, ceiling lowered to `private` |
| --- | --- | --- | --- |
| Alice | passage + original | passage + original | passage + original |
| Bob, active `maple` member | nothing | passage, evidence, context | nothing |
| Carol, not a member (even on an organization connection) | nothing | nothing | nothing |

Operators set and inspect the ceiling:

```sh
vaelius sharing-policy --profile PROFILE --tenant TENANT --project maple --max-sharing team
vaelius sharing-policy --profile PROFILE --tenant TENANT --project maple --max-sharing inherit
vaelius sharing-policy --profile PROFILE --tenant TENANT --max-sharing team     # tenant default
vaelius sharing-policy --profile PROFILE --tenant TENANT                        # status and counts
```

Status reports, per project, the ceiling and the number of active sources stored as
team or private and how many team requests were capped; never content or IDs.
`explain` lists a source's passage readers under current policy in `raw_readers`.

Enforcement is the compiled source policy used for all retrieval: the
`search_source_policy_definitions` projection adds `"sharing": "team"` for
team-shared sources, and the same clause (current ceiling plus active membership of
the policy's project) is applied by the indexed predicate
(`search_permissions.predicate` and `policy_condition(raw=True)`), the row-level
security function `search_document_allowed`, and the Python oracle
(`_visible_source(raw=True)` and `_document_allowed`). A parity test checks that
the indexed predicate and the oracle agree. A teammate's search runs the same number
of SQL statements as the owner's.

Migration `042_project_sharing.sql` is additive: it adds the columns, replaces the
policy view and `search_document_allowed`, and changes no existing policy definition
(existing sources are `private`), so no projection rebuild is needed. Clients that
do not send a request capture as before. A client that requests `team` needs a
service with this migration; an older service rejects the field.

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
opens a file without naming it. The exact coverage and gaps are in
[least privilege for agents](CLIENT.md#least-privilege-for-agents).

## Trust boundaries

Raw agent originals require owner/source access. Raw source passages require
ownership, or team sharing that the operator's project policy allows and the reader's
project membership satisfies. A project or team label, or a repository scope file,
does not itself grant access. Reviewed derived knowledge and native
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
