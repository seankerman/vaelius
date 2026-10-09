# Serving reranking

`agenthub.serving_reranker.ServingReranker` adds a Luna evidence-selection stage to
authorized fused search candidates. It reuses the tool-disabled structured
execution adapter; Codex local tests retain their installation ledger. Tenant usage goes through the
existing `cloud_ops.Meter`; attempts also enter the existing shared evaluation
ledger. It does not select a paid API, run a client model, or perform startup work.
The local default is GPT-6 Luna with low reasoning. Hosted models are explicit
operator configuration; see [model providers](MODEL_PROVIDERS.md).

Enable only in the backend's private `runtime.json` with an internal `reranking`
object: `enabled: true`, an absolute `worker_config` path to a mode-0600 existing
Codex-login worker configuration, an absolute `ledger` path to the shared
evaluation ledger for Codex local execution (optional for API execution), and optional `timeout_seconds` (20 by default, 10–60).
Omission preserves the existing profile. There is no customer-facing model
selector. Configuration and startup perform no inference. At most two reranker
calls run per API process. Busy, unavailable or invalid model execution falls back
to canonical deterministic selection with an explicit diagnostic gap. Deployment
process counts multiply this concurrency limit.

The selector sees fused candidates before heuristic relevance selection, the card
limit and delivery packing. Hard applicability conflicts, temporal cutoffs,
permissions and session suppression still apply. It returns separate `support`
(`none`, `partial`, `complete`) and `answerable` values: useful partial evidence can
be delivered without claiming completeness. It cannot recover candidates missed by
retrieval. Typed historical routing retains its independent validity gate.

## Integration boundary

1. Generate canonical authorized candidates, preserving project and historical cutoff.
2. Batch-check current revisions and permissions before model dispatch.
3. Outside database locks, call `rerank(query, cards, context=...)` with trusted
   request actor/project/time fields. Candidate content remains untrusted data.
4. Recheck canonical current authorization after model selection. Pack original
   cards (at most 1,300 serialized characters each) into the 1,500-character
   automatic or 4,000-character explicit response. Omission marks partial support;
   oversized cards are skipped, never rewritten into unsupported summaries.
5. Keep execution/usage diagnostics out of the client response. Selected IDs and
   revisions map to exact original evidence. Explicit MCP tools can expand it.

The module has no corpus SQL or permission implementation. It must not be called
with raw unfiltered candidates. Both checks use `search_permissions`; permission
implementation must not migrate into this module or client adapters.

## Bounds and failure behavior

The default request considers at most 20 cards with 200-character titles and
2,000-character bodies. The serialized candidate packet, including Unicode
escapes, is at most 65,536 bytes. Candidate or text truncation prevents a complete
answerability result. Accepted model output is at most 8,192 bytes with a strict
`order`/`support` schema. Duplicate and unoffered keys, extra fields and inconsistent
support labels are rejected. Prompt text is untrusted, never instructions.

The default model execution timeout is 20 seconds; the existing harness also has
a separately bounded 15-second login-status check. This is an execution bound,
not evidence that responses are fast. The harness kills its process group on
execution timeout. There is one attempt and no implicit retry. Admission failure,
provider failure or invalid output produces an explicit stage failure; the API
then runs canonical deterministic selection and current authorization again. Confirmed invalid responses still count as returned
provider work. Accounting failures preserve existing receipts for reconciliation.

No response cache is introduced: current authorization is always checked around
execution. Stable instructions can benefit from provider prompt caching; actual
`cached_input_tokens` must be read from receipts, not assumed. The module stores
no prompt or source text. The canonical meter stores a digest and usage; the
harness deletes private temporary execution files.

## Evidence and measurement

`tests/fixtures/serving_reranker_v1/cases.json` was frozen before implementation:
SHA256 `dae19d0f721aa42d57c1812ead9e15b5da9abbb8f43b8c339d58873bcb9974d1`.
The six cases cover actor/correction, historical questions, complementary evidence,
absence, partial support and hostile instructions. These are authored synthetic
**development** cases, not held-out model-quality evidence. Fake expected returns
verify contracts and cannot demonstrate that Luna chooses those returns.

The provider-free suite covers exact card preservation, answerability demotion,
invalid identifiers, output/schema bounds, Unicode input bounds, accounting,
admission failure and no startup/empty-query dispatch. Meter operation counts are
constant for 1, 20 and 100 supplied cards; installed PostgreSQL integration still
must verify the complete authorization/serving SQL count.

Before rollout, run the same frozen authorized queries against identified installed
builds with reranking off/on. Record candidate generation/database time, reranking
execution time, total search-tool time and whole receiving-agent time separately.
Report input, cached-input, output and reasoning tokens from accounting receipts.
Test a concurrent source withdrawal/identity change during the fake model call.
A small Luna smoke proves execution and accounting, not broad relevance improvement.
Temporal semantics, explicit applicability and delivery ceilings stay in force.

October 7 follow-up: the shared development replay now invokes this same selector.
Its real-corpus comparison did not establish a reliable historical-answer win, and
several automated grades failed exact-quote validation. Reranking remains opt-in.
See the superproject final-service evidence for installed builds and full results.
