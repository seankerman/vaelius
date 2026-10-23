# Data lifecycle

This page states what Vaelius deletes, when, from which stores, what a legal hold
changes, how a person's content is erased, how deletion is verified, and what is
outside the service's reach. PostgreSQL is the authority for every lifecycle
decision; the object store holds original bytes only.

## Operations at a glance

| Operation | Who | Effect |
|---|---|---|
| Withdraw | Source owner (`withdraw` action) | Hides the source and blocks dependent documents. Content stays in place under its lifecycle policy. |
| Correct | Source owner (`correct`) | Replaces the source with a corrected revision; the old revision is hidden but kept. |
| Delete | Source owner (`withdraw`) | Purges the source and its derived content from every store listed below, then journals the deletion. |
| Owner retention | Source owner (`withdraw`) | `enterprise.retention(ctx, project, before)`: deletes the caller's own sources in one project captured before a cutoff. Dry-run by default. |
| Forget (owner self-service) | Source owner (`withdraw`) | `vaelius-client forget`, MCP `forget_memories`, `POST /enterprise/v3/my-data/forget`: deletes chosen sources, a session ("forget this session"), everything in a project or a date range. Preview first; applies only with the preview's token. Same purge. |
| Moderation | Project member (`moderate`) | `POST /enterprise/v3/moderation/withdraw`: withdraws (hides) anyone's sources in that project. Never deletes. |
| Scheduled retention | Operator | `cloud_maintenance retention-run`: applies tenant retention policies to every principal's sources. Dry-run unless `--apply`. |
| Subject erasure | Operator | `cloud_maintenance subject-erase`: deletes everything one principal owns and returns a verification report. Dry-run unless `--apply`. |
| Legal hold | Holds administrator (`legal_hold`) | Preserves covered content from deletion, retention and erasure. |
| Tenant retirement | Operator | Drops the tenant database and its current objects. Refused while any legal hold is active. |

Deletion, retention, erasure and journal replay all use the same purge, so they
reach the same stores.

## What a deletion removes

Each row lists a store, what it holds, and what a deletion does to it. "Cleared"
means the row stays as a tombstone with its identifiers, but its content columns
are emptied.

| Store | Holds | On deletion |
|---|---|---|
| `enterprise_sources` | Source identity, owner, project, visibility, timestamps | Kept as a tombstone, marked inactive. |
| `memories`, `memory_fts` | Redacted source text and its lexical index | Text cleared; index row deleted. |
| `events`, `source_event_metadata` | Legacy hook payloads and metadata | Payload cleared; metadata deleted. |
| `backend_source_revisions`, `backend_parts` | Typed source payloads and transport fragments | Payload cleared and marked excluded; fragments deleted. |
| `cloud_conversation_segments` | Locators for typed conversation originals | Object deleted; locator marked removed. |
| `backend_document_versions` | Locators for document originals, with filename, title and page locations | Row deleted. Object key queued for deletion. *(New in this change)* |
| `cloud_migrated_source_objects` | Locators for migrated originals | Row deleted. Object key queued. *(New)* |
| Object store | Original bytes | Deleted by the adapter. A deletion queue (`enterprise_object_purges`) retries after a failure. *(New)* |
| `knowledge_documents`, `knowledge_revisions` | Source passages and curated claims that depend on the source | Lifecycle set to `deleted`; claim and identity JSON cleared. |
| `knowledge_fts`, `knowledge_embeddings` | Lexical index and legacy vectors | Deleted. |
| `cloud_document_vectors` | pgvector embeddings of dependent documents, in every generation | Deleted. *(New: the purge previously left these in place and relied on the search filter)* |
| `knowledge_temporal_*`, `knowledge_support`, `knowledge_relations` | Dated assertions and evidence links | Deleted. |
| `knowledge_candidate_keys` | Reconciliation keys derived from claims | Deleted. *(New)* |
| `memory_metadata` | Subjects, tags and applicability text of curated observations | Deleted. *(New)* |
| `episode_revisions` | Curated episode summaries citing the source | Payload cleared. *(New)* |
| `episode_candidates`, `curation_episode_jobs`, `backend_jobs`, `backend_provider_returns`, `backend_observers` | Curation progress, model returns and observer checkpoints | Cleared, held or invalidated. |
| `cloud_preferences`, `cloud_preference_candidates` | Preferences derived from the source | Ended, cleared and held. |
| `backend_native_artifacts`, `backend_native_spans` | Passage source URL and heading | Cleared. |
| `outbox`, `enterprise_receipts`, `cloud_metrics`, `enterprise_lifecycle`, `enterprise_audit`, `enterprise_deletion_journal` | Identifiers, counts and hashes only | Kept as the audit trail. |

The dependent documents of a deleted source are blocked and cleared. This follows
withdrawal semantics: Vaelius does not automatically re-derive a curated document
from its remaining sources, and it never calls a model during deletion. If an
operator runs curation again, documents are rebuilt only from sources that are
still active.

## Owner self-service deletion

A person deletes what they created without an operator
([privacy model](PRIVACY_MODEL.md#owner-controls), [client](CLIENT.md)):

- **Selection**: `source_ids` (up to 200), `session` (`current` for the session the
  caller captured most recently in the given project, a session handle from
  `mine`, or a host conversation ID), `project`, `since`, `until`. At least one is
  required. Only the caller's own sources are ever selected, including withdrawn
  ones; already purged sources are not selected again. One call handles at most 200
  sources (`more: true` means run it again).
- **Preview first**: without `confirm` the call changes nothing and returns counts
  (sources, active, held, sessions, projects, capture range) and a `confirm_token`
  bound to the exact selection. With `confirm` set to that token the selection is
  deleted; if it changed meanwhile, the call returns a fresh preview with
  `status: selection_changed` instead.
- **Same purge**: each source goes through `_expire_source_tx`, the path retention
  and erasure use, under the policy lock: the stores in the table above, the
  deletion journal (`delete`, or `withdraw` + `delete_held` under a hold), queued
  original objects, and an `owner_delete` audit row. Journal replay on a restored
  copy therefore repeats owner deletions too.
- **Legal holds win** and are reported as `held` (see above).

Moderation (`moderate`) is a withdrawal, not a deletion: the source and its derived
documents are hidden, a `withdraw` journal entry and a `moderate_withdraw` audit
row are written, and `enterprise_sources.moderated_by/at/reason` record who, when
and why. The author can still delete it.

## Scheduled retention

Retention policies are set per tenant. Each policy gives a maximum age for one
project and source type, and either can be the wildcard `*`. Source types are
`agent`, `conversation`, `document` and `record`; legacy hook captures count as
`agent`. When several policies match a source, the most specific one wins:

1. project and type
2. project only
3. type only
4. tenant default (`*`, `*`)

A source with no matching policy is never expired. Age is measured from the
time the source was captured (`enterprise_sources.created`), not from the time
the content describes.

Write the policy set declaratively, with the input file mode set to 0600. Each
call replaces the tenant's whole policy set:

```json
{"policies": [
  {"project": "*", "source_type": "*", "max_age_days": 730},
  {"project": "support", "source_type": "conversation", "max_age_days": 90}
]}
```

```sh
python -I -m agenthub.cloud_maintenance retention-policy --profile /private/profile \
  --tenant TENANT --input /private/retention-policy.json
python -I -m agenthub.cloud_maintenance retention-run --profile /private/profile --tenant TENANT
python -I -m agenthub.cloud_maintenance retention-run --profile /private/profile --tenant TENANT --apply
```

To run retention on a schedule, start `retention-run --apply` from cron or a
systemd timer (daily is typical). Each run is bounded: it scans at most
`max_scan` sources (default 1,000) and deletes at most `max_sources` (default
100). The next run resumes from where the last one stopped, and changing the
policy restarts the scan from the oldest source. Re-running a completed run
changes nothing.

Each run also completes deletions that a legal hold had deferred, once that hold
is released. Every run stores a content-free report in `enterprise_lifecycle_runs`
and writes an audit row. To read a report, run `lifecycle-report` with
`{"run_id": ...}`. A dry run records the counts it would act on, but never
changes data or moves the resume position.

## Legal holds

A hold covers one of four scopes: a whole `tenant`, a `project`, a `principal`
(as owner), or a single `source`. Each hold records a reason, the actor who set
it, and when; releasing it records the same. Creating or releasing a hold
requires:

- an enrollment credential with the new `legal_hold` action;
- a principal with `settings_admin`;
- the principal acting for itself.

Delegations cannot grant `legal_hold`, and OAuth provider tokens do not carry
it. The command authenticates with a token file:

```sh
python -I -m agenthub.cloud_maintenance legal-hold --profile /private/profile --tenant TENANT \
  --token-file /private/holds-admin.token --input /private/hold.json
# hold.json: {"action": "create", "scope": "principal", "target": "PRINCIPAL", "reason": "Matter 123"}
#            {"action": "release", "hold_id": "hold_...", "reason": "Matter closed"}
#            {"action": "list"}
```

While a hold covers a source:

- Retention skips the source, and it stays searchable.
- An owner's deletion hides the source like a withdrawal. The deletion journal
  records both `withdraw` and `delete_held`. Content and originals are preserved.
  The single-source lifecycle response (`POST /enterprise/v1/lifecycle`) keeps its
  strict wire shape and does not mention the hold. Owner self-service (`forget`,
  `mine`) does report it: the preview and the result count held sources as
  `held`, and `mine --all` shows them as `deletion_held`. It names no hold, matter
  or reason, but it does tell the owner that some of their content is held, so
  legal teams should expect custodians to learn that a hold exists.
- Subject erasure also hides the source and defers the purge. The report counts
  it under `held`.
- Withdrawal and correction work as usual.
- `tenant-retire` refuses to run.

Releasing a hold deletes nothing by itself. The next `retention-run --apply`
completes any deferred deletions, and the next `subject-erase --apply` completes
an erasure. Hold create and release events appear in `enterprise_audit`.

Limitation: a hold preserves the covered sources and their originals. A curated
document that also depended on an uncovered source is cleared when that other
source is deleted, because derived documents can be rebuilt from their sources.

## Subject erasure

Use subject erasure for offboarding or for a deletion request. It erases every
source the principal owns. For delegated capture, the owner is the person the
content was captured for. The report counts sources the principal captured on
behalf of other owners (`captured_for_other_owners`), but does not delete them.

1. Deactivate the person in the directory and revoke their credentials
   ([enterprise auth](ENTERPRISE_AUTH.md)), so no new capture arrives.
2. Preview the erasure:
   `subject-erase --input erase.json`, where `erase.json` is `{"principal": "PRINCIPAL"}`.
3. Apply it with `subject-erase --input erase.json --apply`. Repeat until the
   report shows `more: false`. Each pass handles at most `max_sources` sources
   and is idempotent.
4. Keep the final report. `verified: true` means all of the following hold:
   - every owned source has a deletion tombstone;
   - every store listed in `after` reports zero remaining content;
   - no deletion of an original object is still pending;
   - an object `HEAD` on every recorded original key found nothing.

   If a legal hold blocks the erasure, the status is `held`.

The report contains identifiers and counts per store, before and after. It never
contains content.

## Verification

`test_data_lifecycle.py` adds content with a unique marker through every intake
path:

- legacy hook capture with a reviewed note;
- a typed conversation with source passages;
- an original document;
- vectors;
- curation rows.

After deletion or erasure, the tests scan every text, JSON and binary column of
every table, as well as the object store. They also check that search and the
MCP `search_memory` tool return nothing, and that running source indexing,
document registration and vector reconciliation again does not bring the content
back. A companion test turns off the new derivative purge and confirms that the
scan then finds the leftover originals, metadata, episode summaries, keys and
vectors, so the scan does not pass by default.

## Backups and restore

Logical snapshots (`cloud_local backup`, `cloud_recovery.backup`) and database or
object backups contain content as it was when the backup was taken. Deleting
from the live authority does not change an existing backup. Operators must:

- Keep backups no longer than the shortest retention period that covers their
  content (plus the erasure deadline you commit to), and delete expired backups.
  The service does not manage backup storage.
- Expire noncurrent object versions with an S3 lifecycle rule
  (`NoncurrentVersionExpiration`) when bucket versioning is on. A delete only
  adds a delete marker.
- Export the content-free deletion journal on a schedule, and store it apart from
  the backups:
  `cloud_maintenance deletion-journal --output /private/journal/PAGE.json`
  (`{"after_id": N}` pages forward).
- Replay the journal on any restored copy before reconciling it or putting it
  into service:

```sh
python -m agenthub.cloud_recovery replay-deletions --current-profile /private/profile \
  --target-config /private/offline-target.json --journal /private/journal/PAGE.json --apply
```

Replay purges every journaled deletion that is still present in the restored
copy, including its originals in the target's object store. It also hides every
journaled withdrawal or correction. Running it again changes nothing, and it
leaves the target held as not ready. `reconcile_restore` then installs the
current authority state as before. Without a journal replay, a restored snapshot
can contain deleted content. The readiness hold prevents serving that copy, but
the bytes stay on disk until replay or reconciliation.

## Limits

- **Tombstones remain.** Source IDs, owner and project names, external IDs,
  timestamps, journal and audit rows, hold reasons and run reports are kept,
  because they are needed for accountability and replay. They contain no
  captured content.
- **Database internals.** PostgreSQL keeps deleted row versions until `VACUUM`,
  and WAL archives and point-in-time-recovery windows keep them until they
  expire. Configure autovacuum and WAL/PITR retention within your retention
  commitment.
- **Outside the database.** The service does not manage:
  - model provider accounts (provider-side retention);
  - execution working directories under the profile;
  - the optional local embedding campaign cache (`semantic.accounting_root`);
  - request logs that the operator ships elsewhere;
  - client devices, whose outbox drops each payload once it is acknowledged.
- **Trusted operator.** Erasure is an operator command. The trusted operator and
  the database administrator can read everything until it is purged.
