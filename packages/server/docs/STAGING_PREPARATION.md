# Portable staging preparation

This bundle prepares a deployment review. It creates no resources and does not
authorize deployment, external collection, paid model calls or a customer pilot.
The selected release and exact local evidence are recorded in the superproject's
S acceptance ledger. The current artifact is an identified engineering candidate;
its failed retrieval quality/100k latency gates prevent local product acceptance.
Preparing or testing it does not select it for production. This guide also works
in an independent AgentHub clone with the matching Client wheel and canonical
source pin.

## Required inputs and account preflights

Copy `containers/staging-inputs.example.json` into an owner-only directory outside
Git. Resolve the null fields with the operator. Never put passwords, tokens,
originals or Codex login state into the image, repository, command arguments or
receipts. An unresolved field means its actual-account preflight is pending.

| Input | Local preflight after explicit authorization | Completion evidence |
| --- | --- | --- |
| Company model account/model and retention controls | Explicit private credential file, bounded structured-output attempt through `ApiExecution`; no ambient key discovery | Actual usage/cache fields, one attempt per ledger row, blocked/cancelled return remains uncertain; remote billing reconciled separately |
| IdP test issuer and client | Exact issuer/subject binding, PKCE and callback using installed `agentclient.cloud_enroll`; provisioning through the documented Users/Groups subset | Real login, group/delegation expiry and offboarding; no email/display-name authorization |
| Slack test workspace and one selected channel | Operator-verified reader bindings, explicit token/signing-secret files, finite connector sync | Actual pagination, 429 hold, edits, signed deletion, incomplete-page hold, reader revocation and replay |
| Cloud/region/CPU/operator accounts | Resolve image architecture, database and object endpoints, TLS, network and workload-role design | Reviewed target-specific configuration/IaC with validation only; no apply |
| Personal project | Explicit isolated profile and connection enrollment | Next five natural reviews; no historical import or global/founder change |

Real provider/vendor-specific instructions must be rechecked against their official
documentation when approved accounts are selected. The local login and mock
fixtures are not production credentials or real account acceptance.

## Process and secret separation

Build from the actual pinned `containers/Dockerfile.cloud` and
`containers/requirements.cloud.lock` with exactly two independent wheels. The
build context contains these artifacts and lock file; no runtime or operator
configuration. Verify `pip check`, installed module hashes and canonical pin.
API and worker processes use the same release and PostgreSQL authority.

```sh
python -I -m agenthub.cloud_runtime --profile /private/runtime --bind 127.0.0.1 --port 8080
python -I -m agenthub.cloud_local readiness --profile /private/runtime --tenant TENANT_ID
python -I -m agenthub.cloud_local worker-run --profile /private/runtime --tenant TENANT_ID \
  --max-jobs 1 --max-seconds 10
python -I -m agenthub.cloud_local status --profile /private/runtime --tenant TENANT_ID
python -I -m agenthub.cloud_local usage --profile /private/runtime --tenant TENANT_ID
```

The default worker indexes deterministically redacted original passages and
configured local embeddings; it does not run curation. Explicit enrichment requires
`processing.curation_enabled: true`; `--provider-free` then selects a synthetic
observer fixture. After model credentials
and usage accounting are selected, the operator explicitly runs a finite live
worker; startup never launches it. This milestone's Codex/Luna login lives on the
host outside the image and remains restricted to its original shared ledgers.

Mount only runtime application credentials into the API. Tenant application roles
cannot create databases/roles or migrate. Provisioning/migration/recovery use a
separate operator role and explicit commands. Router credentials cannot administer
tenant registration. Tenant routing must name a current authorized database;
there is no SQLite failure fallback or client knowledge corpus.

The local S3 adapter uses explicit local SDK credentials and endpoint allowlisting.
It does **not** prove managed workload IAM, encryption, object lock or tenant
object-role separation. A selected-target workload credential/permission adapter
and actual unrelated-object denial checks remain required before deployment.
Do not put Moto's shared synthetic credentials into a cloud configuration.

## Resource and operator controls

The current API admission limit is 32; tenant store cache is configurable (local
profile 4). Each worker has finite job/call/retry/time/input limits, durable leases,
fences, checkpoints and saved returns. Per-tenant model admission and backlog/hold
status are separate from the founder's rolling limit. Tested concurrency and
latency belong to the exact release/workload; these defaults are not a production
capacity promise. Backpressure and slower tenants must retain privacy and fairness.

Keep access logs disabled, ignore untrusted forwarded headers and return sanitized
errors. Export content-free request outcomes, latency, backlog/hold counts,
uncertain calls, meter totals and object/embedding work. Do not log prompts,
originals, source URLs, SQL parameters, credentials or answer text.

HTTP model work runs in one supervised transport process. A wall-time expiry or
cancellation kills that local process and denies late commits. The remote attempt
may remain billed; reconciliation is required. No implicit retry is permitted.
Use `cloud_local usage`, connection status and explicit bounded retry/recovery
commands; never reset ledgers or restore expired permissions to clear a hold.

## Migration, originals and recovery

Apply checksum-pinned additive migrations with the operator role, grant only
required application permissions, then start the identified release. Indexes are
rebuildable derivatives. Build a new generation, validate sources/revisions/model,
and atomically activate it. Interrupted/failed generations cannot replace serving
state; correction, withdrawal and deletion invalidate stale derived delivery.
Source originals remain individual immutable objects and are fetched under current
permissions with version/checksum/length verification. They are not summarized
away or constrained to the prompt-card character limit.

```sh
python -I -m agenthub.cloud_local versions --profile /private/runtime --tenant TENANT_ID
python -I -m agenthub.cloud_local reindex --profile /private/runtime --tenant TENANT_ID
python -I -m agenthub.cloud_local backup --profile /private/runtime --tenant TENANT_ID \
  --output /private/new-snapshot
python -I -m agenthub.cloud_recovery --help
python -I -m agenthub.cloud_local migrate --profile /private/runtime --tenant TENANT_ID \
  --provider-free
```

The reindex command has a 10,000-document local bound; larger operator rebuilds
must use an explicitly bounded resumable path. Recovery commands require an
owned empty held target and explicit offline target/operator configuration. Verify
snapshot hashes, restore, then reconcile current authority deltas—including later
ingestion, policy narrowing, correction, withdrawal/deletion and originals—before
releasing its hold. Do not point clients at a stale restored corpus. Old-code
rollback is safe only on the same current authority with a tested additive schema;
otherwise declare roll-forward only. Local logical restore is not managed PITR,
RPO/RTO, cross-region recovery or production failover evidence.

`cloud_local migrate` requires an explicit private `migration-selection.json`
with the immutable snapshot path, checksum and tenant. It imports into a new
unrouted rehearsal database and removes that temporary database after recording
the result; it does not cut over the serving authority. `cloud_migration` is a
library, not a command-line migration entry point. `cloud_local rollback` likewise
requires a private `rollback-selection.json` identifying the actual previous
installed interpreter and build IDs. Its read-only compatibility probe does not
authorize old workers or restore obsolete permissions.

## First staging and team entry gates

First deployment, when separately authorized, admits synthetic tenants only.
Verify actual workload identities, TLS, denied network paths, application and
cross-tenant database/object permissions, current revocation during each return
path, managed recovery/failover and object availability. Then repeat serial,
concurrent, ingest/update and fault workloads on the target architecture with
frozen source-grounded questions and complete/censored request counts.

A real-team pilot follows passing local engineering, five natural personal reviews
and staging controls. It requires explicit team/source enrollment and actual IdP,
connector and model account acceptance. Measure capture completeness, extraction,
indexing, relevant delivery, observed usefulness/repetition, abstention, privacy,
cost and latency separately. Local package completion or authored confirmation
cannot substitute for that acceptance.
