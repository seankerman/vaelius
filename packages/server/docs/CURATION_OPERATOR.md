# Finite curation and saved-extraction review

> **October 1 authorization update:** The user removed artificial execution and
> authorization caps. Necessary local backfill, curation and retrieval tests may
> continue without another budget/round-limit approval. Numeric attempt, retry,
> receiving, embedding-time and founder/installation ceilings below are historical.
> Keep accounting and failed receipts; use finite resumable batches and meaningful
> measurements. Consumed cases remain development data, not fresh confirmation.
> Privacy, identity, source validity and accurate reporting still apply.


The packaged `agenthub.curation_operator` uses the canonical backend worker and
AgentClient processing library. It does not ingest sources, activate a building
generation, or change the founder profile. Run it with an explicitly selected
private backend profile, using the matching installed client and hub packages.

Actions:

| Action | Behavior |
| --- | --- |
| `status` | Inspect backend jobs, observers, dispatches and capture gaps. |
| `review --job-id ID --output FILE` | Validate saved stage outputs and reconstruct a first-pass envelope in a rolled-back PostgreSQL transaction. No model dispatch or installation. |
| `migrate-saved --job-id ID --output FILE` | Persist the compatible extraction envelope, preserving the previous completed status and original provider returns. Unsupported or incomplete saved outputs remain errors. |
| `extract --episode-id ID` | Advance an explicitly selected pending episode to `extraction_ready`. Save source-backed records and advance the observer without requiring consolidation. |
| `consolidate --episode-id ID` | Reuse an extraction envelope and the existing fenced resolver/install path. Missing or invalidated extraction cannot silently dispatch extraction again. |
| `resume --phase extract\|consolidate\|full --episode-id ID` | Continue a finite selection. This does not resume an old campaign or enable tenant admission. |
| `pause` | Disable admission for new processing calls; an already dispatched call may finish. Existing usage, checkpoints and receipts remain. |

All actions require `--profile`; tenant defaults to `acme`. `--episode-id` is
repeatable and identifies canonical curation episodes, whereas `--job-id`
identifies a backend worker job. Phase runs default to provider-free execution;
`--live` requires already authorized admission, a shared `--ledger`, valid source
permissions, and the existing backend execution configuration. Provider-free
phase execution cannot fill missing saved model outputs.

Bound each phase with `--max-jobs`, `--max-calls`, `--max-retries` and
`--max-seconds`. Preserve the explicit selection and bounds in the run definition
and receipt record. These per-run limits supplement shared campaign and
installation accounting; they do not grant a new cumulative allowance. Receipt
files are private and exclusive; an existing output path prevents the action.
Preserve earlier failed receipts when choosing a fresh output path.

For a staged legacy checkpoint, preview before migration. A valid preview causes
zero extraction calls. Migration adds a hash-checked envelope inside the existing
episode progress; it retains raw stage outputs, reference manifests and resolution
history. Back up the exact prior progress bytes before a deliberate rollback.
The envelope's stage hashes refer to retained stage outputs; dispatch and usage
remain in the enclosing job's canonical provider-return/accounting records.
Legacy outputs without complete request metadata remain visibly legacy.

The observer stores a bounded index of original evidence in its existing
checkpoint. It validates source revisions, current policies and lifecycle before
reuse and loads the subsequent completed/extraction-ready turns. Omitted originals
remain explicit gaps. This index is an acceleration artifact, not searchable
knowledge. Provider conversation handles and paired cumulative-usage checkpoints
remain separate from authoritative application state.

Draft evaluation is an owner-operated Python capability in `draft_review`; it is
not exposed by HTTP/MCP parameters. It requires an explicit document/revision and
source manifest, applies current authorization, and rolls registration/index
changes back. Do not hold its review transaction over provider inference, run
concurrent measurements under its delivery lock, or activate a generation merely
to inspect it.
