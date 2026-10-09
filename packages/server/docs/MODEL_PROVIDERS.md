# Internal model providers and portable hosting

AgentHub owns model execution. AgentClient captures, redacts and transports; it
never invokes a model. Operator configuration selects models independently of the
coding agent used by an employee. There is no customer model-selection UI.

## One execution interface

`cloud_execution.Execution` and `execution_from_config` serve the observer,
resolver and serving reranker. The canonical pipeline owns prompts, schema/evidence
validation, provenance, PostgreSQL jobs/checkpoints, permissions and result commits.
Adapters perform exactly one structured request and normalize usage. They do not
retry or fall back to another provider. Unknown network outcomes remain uncertain.

| Adapter | Intended use | Conversation state | Accounting |
|---|---|---|---|
| `codex` | Local tests through the existing Codex login | Native resumed conversation plus recoverable authorized PostgreSQL context | PostgreSQL tenant meter, shared local evaluation ledger, existing installation receipts |
| `operator_api` | Explicit OpenAI Responses API configuration | Stateless `store:false` requests reconstructed from authorized bounded context; response IDs are not resume handles | PostgreSQL tenant meter and pipeline receipts; optional local evaluation ledger |
| deterministic fixture | Provider-free tests only | Synthetic | Test receipts; never selected as a live fallback |

An internal model change within a provider is configuration. A different vendor
requires one adapter and a factory entry, tested against the same contract. This
is not a claim that arbitrary vendors implement the OpenAI wire protocol.

The protocol returns `(value, usage, handle_or_none)` for `durable_memory_curate`
and `(value, usage)` for other stages. `resumable` describes actual handle support.
Raw content remains untrusted; no tools are enabled. Timeouts, input/output bounds,
refusal and malformed results are explicit. In-flight cancellation does not claim
to cancel remote billing. Provider/model/reasoning identity fences observer state
and saved-return reuse; credential rotation alone does not change that identity.

Migration 029 adds this execution fingerprint. Legacy observers reconstruct once
on their next pending job; their durable evidence, completed outputs and receipts
remain. A provider switch also reconstructs and fences an older worker. Drain old
workers before rolling versions; old code cannot enforce a new fingerprint fence.
Use a new curation generation when changing its frozen model/prompt definition.

## Local worker configuration

Keep the existing private `worker-config.json` and ledgers. Relevant fields:

```json
{
  "backend_execution": {"kind": "codex"},
  "observer": {"enabled": true, "model": "gpt-6-luna", "reasoning": "low", "timeout_seconds": 60}
}
```

The remaining existing episode, accounting and authorized-connection settings are
still required; this snippet is not a replacement for the whole worker profile.
Local native context/cache support is unchanged. Cache hits are measured in usage
receipts, not promised by the interface.

## Hosted worker configuration

Mount a private file containing an operator-owned API key. Do not put it in the
image, repository, command line or user configuration. Example configuration:

```json
{
  "backend_execution": {
    "kind": "operator_api",
    "model": "REPLACE_WITH_OPERATOR_SELECTED_API_MODEL",
    "credential_file": "/run/secrets/model_api_key",
    "timeout": 60,
    "max_input_chars": 256000,
    "max_output_tokens": 4096
  },
  "observer": {"enabled": true, "reasoning": "low", "timeout_seconds": 60}
}
```

Choose an API model available to the operator that supports strict structured
outputs. Do not assume a Codex subscription alias is an API model ID. Reasoning is
sent only when configured; use `null` to omit it for a model without that option.
The effective API model is recorded in pipeline model metadata. The Responses
request uses `text.format` JSON schema, no tools and `store:false`; provider-side
retention/access policy still requires deployment review. API credentials are not
loaded from an unrelated environment variable. No paid request occurs on startup.

Reranking uses the same worker-config format, through `runtime.json`:

```json
{"reranking": {"enabled": true, "worker_config": "/profile/ranker.json", "timeout_seconds": 20}}
```

For local Codex ranking, additionally supply the existing absolute `ledger` path.
Cloud API ranking needs no SQLite evaluation ledger. Curation and ranking may use
different internally selected models/configurations. Enabling ranking remains an
operator choice governed by usefulness and latency evidence.

## Other hosting seams

- **Database:** ordinary PostgreSQL DSNs and tenant routing. `runtime.json` accepts
  `control_dsn_file` instead of an inline `control_dsn`; specifying both fails.
  Mount mode-0600/0400 secrets readable by the runtime UID. Configure database TLS,
  roles and networking for the chosen managed service.
- **Original objects:** `objects.kind=file` for local disk; `s3` for explicit local
  emulators; `s3_aws` with `bucket` and `region` for AWS SDK workload credentials.
  AWS mode supplies no static key or custom endpoint. Grant the service role only
  its intended bucket/prefix access. ECS/EKS workload identity works through the SDK
  chain; EC2 metadata credentials require explicitly changing the image's default
  `AWS_EC2_METADATA_DISABLED=true` policy.
- **Identity:** existing Keycloak configuration, or `kind:oidc` with explicit
  `issuer`, `client_id`, `authorization_endpoint`, `token_endpoint`, `jwks_uri` and
  `redirect_uri`. Existing signature/claim/tenant/delegation checks remain. Endpoints
  currently require the issuer's origin; IdPs with split origins need a reviewed
  adapter extension. The factory performs no discovery/network request at startup.
- **Client:** `knowledge_backend.transport=https` explicitly permits the configured
  HTTPS service origin for capture and HTTP MCP. Default remains loopback HTTP.
  TLS verification, no redirects and no proxy fallback remain in force. Setting a
  URL never creates an enrollment or changes source visibility. Existing enrollment
  credentials/header helper are supported; generic MCP OAuth discovery is separate.
- **Embeddings:** cached Nomic through portable ONNX Runtime, including Linux CPU.
  The query/index embedder interface remains separate from the structured LLM
  interface. Mount the matching model files; a new embedding model/dimension requires
  building and validating a compatible vector generation before activation.
- **Process state:** API and finite workers run from the same installed image.
  PostgreSQL owns queues, leases, source metadata and knowledge; S3 owns original
  bytes. Cloud API workers do not require a desktop login or its native session files.
  Working directories and embedding caches are derivatives, not another corpus.

Build `containers/Dockerfile.cloud` from the two pinned wheels and dependency lock.
API entry point: `python -m agenthub.cloud_runtime --profile /profile --bind 0.0.0.0 --port 8080`.
Default worker entry point: `python -m agenthub.cloud_local worker-run --profile /profile --tenant acme --max-jobs 20 --max-seconds 120`.
This indexes original passages and configured local embeddings without an LLM.
Model enrichment additionally requires `processing.curation_enabled: true` and
`--live`; Luna reranking has its own opt-in setting. See [source-first defaults](SOURCE_FIRST.md).
A process supervisor/scheduler repeats finite worker batches. Do not run a model
loop inside API startup. Front the API with TLS, configure `allowed_hosts`/origins,
and inject explicitly provisioned runtime/worker/tenant configuration. The local
`cloud_container prepare` helper targets the emulator rehearsal, not cloud provisioning.

## Acceptance boundary

Contract mocks and local container/PostgreSQL tests verify adapter wiring, recovery
and isolation. They do not prove a real account's model access, rate limits, cloud
IAM, managed-database operations, internet OAuth onboarding or team usefulness.
No deployment or paid provider selection is implied. Existing retrieval quality
and withdrawal-dependency risks remain independent product gates.

API contract checked against the [official Responses reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create).

## Local readiness follow-up (October 7, 2026)

Both live adapters now validate the entire canonical JSON schema, including nested
bounds and additional properties. Domain validation still checks original evidence,
actor and lifecycle semantics. A valid JSON result is not proof of a useful memory.
Errors carry a content-free outcome and normalized usage: returned-invalid/refused/
incomplete, rejected, cancelled before dispatch, or uncertain after dispatch. The
worker and reranker retain known usage in PostgreSQL and the existing local ledgers.
No adapter retries an invalid response. Unknown usage remains unknown, not zero cost.

Codex-login continuation was measured on four synthetic turns through the installed
worker, canonical curator and resolver: the same observer handle was used throughout;
the three continuing curator calls reported 40,192 cached input tokens. Total for
seven curator/resolver calls was 91,343 input and 743 output tokens. Curator dispatch
elapsed times were 3.7–5.0 seconds, separate from database search and receiver latency.
These are subscription receipts, not API latency, billing or a cache guarantee.
The smoke also exposed a correction failure: a location correction became RELATED,
leaving the earlier location current. Provider wiring passes; usefulness does not.

Migration 030 snapshots the exact compared revisions and source dependencies before
model dispatch. Future registration cannot inherit unrelated later dependencies from
a compared document. Native observer history remains an influence until a genuine
context reset. `backend_worker.max_context_sources` defaults to 256 and bounds future
observer epochs; required originals of saved stages remain available for validation.
This is a context-size control, not an authorization or spending quota. Legacy jobs
and pre-worker partial outputs keep conservative dependencies; migration does not
silently remove their historical privacy links.

For a held job whose own original sources remain authorized, the local operator can
use `python -m agenthub.cloud_local retry --profile <backend-profile> --tenant acme
--job-id <backend-job-id> --rederive`. This schedules recovery; the next explicit
finite worker batch performs any model work. This recovery starts a fresh
observer, discards previous draft/comparison inputs, and creates a new document.
Prior candidates are archived and prior withdrawn documents remain denied. It never
restores a withdrawn original. Prefer this explicit operation to editing dependency
rows. Existing large legacy dependency graphs require reviewed rebuilding; they were
not globally rewritten in the readiness pass.

Managed hosting and self-hosting use these same packages, API and worker. The
operator supplies deployment configuration and model credentials; the end user's
plugin holds only its scoped service credential. Vaelius is distributed under Apache 2.0; provider services and model assets retain
their own terms and licenses.
