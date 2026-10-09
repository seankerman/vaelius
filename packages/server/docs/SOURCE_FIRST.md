# Source-first service defaults

The runtime defaults to `retrieval.corpus: sources`,
`processing.curation_enabled: false`, and `reranking.enabled: false`.
The client is unchanged: deterministic secret cleaning, capture, transport and
delivery. Permitted person/location facts remain in the private authority; this
is not blanket PII anonymization. Raw chat passages require the source owner and
`source_read`, including on team/organization connections. Existing document
visibility, identity, delegation, connection freshness and source policies still
apply. Returning a passage never grants access to its original.

Ingestion commits the redacted source and an idempotent source-index outbox job.
`agenthub worker-run` installs exact 1,600-character maximum passages (same simple
character baseline used in development), source timestamps, roles, actor labels,
conversation/turn IDs and evidence offsets. Encoded media is retained in originals
but excluded from indexed text. Source gaps remain gaps. Each transaction commits
a bounded passage batch and offset; crashes roll back the uncommitted batch.
No observer or consolidation call is needed. Searchable text and vectors reuse
the existing knowledge tables, indexes, source dependencies and permission oracle.

Configure the internal semantic model directory and `semantic.enabled: true`
for hybrid retrieval. The worker initializes an empty incremental vector generation
when none exists and uses the existing vector job queue. No model is downloaded or
provider selected automatically. Sources may become lexical-searchable before
their embeddings are ready. Failed source jobs stay explicit in the outbox.

```sh
agenthub worker-run --profile /absolute/private/profile --tenant acme --max-jobs 20 --max-seconds 60
# Backfill already retained sources, paging with the returned next_after value:
agenthub source-index-reconcile --profile /absolute/private/profile --tenant acme --max-jobs 1000
agenthub source-index-reconcile --profile /absolute/private/profile --tenant acme --max-jobs 1000 --after PREVIOUS_NEXT_AFTER
```

Reconciliation does not collect new chats. Repeat finite workers until indexing
drains. A denied/expired source is held; do not automatically undo lifecycle or
permission changes to resume it. Duplicate ingestion never creates another source.
Source revisions invalidate their old passages; later independent messages remain
separate, preserving earlier proposals and subsequent decisions. Old source bytes
and curated versions remain retained under their existing lifecycle policy.

Search is discovery: `answerable: false`, with `support: partial` for available
passages. MCP returns cards and lets the agent fetch exact evidence, follow
`next_offset`, and expand conversation context. Existing automatic-hook gates
remain conservative and do not inject discovery-only results as complete answers.
`as_of` filters source event time; `known_at` additionally requires that the source
was ingested by that cutoff. This is chronological evidence, not an inferred
validity period. Unknown timestamps cannot satisfy a dated filter. Native original
documents and private-preference tools remain available.

To explicitly inspect retained derived documents, set `retrieval.corpus: all`.
To resume model enrichment, set `processing.curation_enabled: true` and use the
existing private worker configuration with explicit `--live` or `--provider-free`.
Existing jobs, model outputs, checkpoints and accounting are preserved. Luna
reranking is independently opt-in under `reranking`; see [its configuration](SERVING_RERANKER.md).

Migration 031 is additive and tightens raw-passage RLS for the restricted reader.
Migration 032 supplies a fixed full-text lookup so PostgreSQL can use its GIN
index despite non-leakproof text-search operators and RLS. It returns only IDs,
revisions and scores admitted by the existing `search_document_allowed` oracle;
outer reads and delivery still recheck permissions. No caller-supplied SQL,
permission cache, or bypass role is exposed. After migrating, the operator must
refresh the existing application/reader grants with `cloud_profile._grant`.
API startup uses the application role, never the migration owner's credentials.
Back up the authority and private profile before cutover. Roll back to the previous
wheel/profile only after stopping the API/worker and marking raw enterprise documents
inactive with `blocked_reason='source_pipeline_rollback'`. Older detail handlers do
not understand the new owner restriction, so this hold is required before starting
an old binary. Preserve the migration and source rows rather than deleting new evidence.
When returning to the new binary, clear only that explicit rollback hold, then
recheck current source authorization. Verify rollback
with the exact old installation. Local fixture results are not cloud/team acceptance.
