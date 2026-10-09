# Local usage, volume and logical recovery

`Meter` reserves stable backend attempts before dispatch, enforces tenant admission
under a row lock, distinguishes genuine retries, and retains reported input,
output, cache-read, cache-write and reasoning counters. A confirmed duplicate
completion does not charge again. An uncertain return can be reconciled into the
same attempt. Versioned price estimates are separate from a bill; missing reported
usage is unknown. Metrics accept only a content-free field allowlist.

Original-document acceptance records bytes, source revisions, completed logical
parsing attempts/status and native passage counts against stable upload IDs. An
accepted retry verifies the original and repairs any partially written metrics.
These parsing counters describe accepted source revisions, not every failed CPU
parse before acceptance. Server API/worker/embedding accounting is integrated by
the corresponding operators; it is not a plugin usage throttle.

`cloud_load` creates two explicit private test databases/roles and individual
synthetic originals. It expands canonical exact passages with bulk COPY for fast
fixture setup; this setup is not ordinary ingestion throughput evidence. Every
measured request uses `CloudStore.search`, with candidate-stage timing inside that
same request. The deterministic 512-dimensional model key
`synthetic-volume-onehot-512-v1` is isolated to this fixture. It measures volume,
not semantic quality, and never replaces the default Nomic model.

```sh
python -m agenthub.cloud_load --profile /private/volume-rehearsal \
  --parent-profile /private/cloud-readiness-v1 --seed 10000 \
  --queries 200 --max-seconds 60
python -m agenthub.cloud_load --profile /private/volume-rehearsal \
  --parent-profile /private/cloud-readiness-v1 --seed 100000 \
  --queries 200 --max-seconds 60
python -m agenthub.cloud_load --profile /private/volume-rehearsal \
  --parent-profile /private/cloud-readiness-v1 --seed 100000 \
  --workload hybrid --queries 200 --max-seconds 60
```

The subprocess enforces the finite wall-clock bound, including long individual
queries. Timestamped receipts preserve failures and earlier measurements. Corpus
counts, hardware, actual query count, p50/p95 candidate/search stage latency,
memory, concurrency and errors are explicit. Serial measurements do not establish
HTTP transport capacity, queue throughput, cloud capacity or a workload cost.
Exact discovery is labeled separately and can bypass vectors. The frozen non-exact
hybrid workload requires an actual vector path and the specific requested source
path in delivered answers. Both retain the same 200-request/60-second limits.
Supplementary tests cover two independent query processes while a source ingests,
mid-read original permission revocation, and an unreachable tenant transport while
the neighboring tenant continues. The last case does not stop a real database.

`cloud_recovery.backup(store, objects, destination)` is a bounded transactional
logical fixture snapshot, **not** `pg_dump`, managed database backup or S3
durability evidence. It stores checksummed table records and matching individual
source files in a private directory. Its manifest includes canonical schema,
generation, model/index, source/policy and lifecycle tables plus original
document, migrated-source and typed conversation-segment object locators. Defaults are 5,000 rows per table,
64 MiB total and 100 individual objects. Larger inputs fail with explicit bounds.
Private source data and operator credentials never enter Git.

`restore_snapshot(snapshot, target, objects, admin_dsn=...)` requires a fresh
unregistered offline target. It verifies all bytes, applies the schema-compatible
logical state with explicit private operator credentials, and leaves readiness
held. Missing current source/policy/lifecycle deltas and missing or corrupt
originals cannot be acknowledged as a usable restore.

`reconcile_restore(snapshot, target, live_authority, target_objects, live_objects,
admin_dsn=..., destination=...)` refuses to overwrite target writes. It captures
current authoritative state under the delivery lock, restores the offline
fixture, and verifies table fingerprints and original objects before acknowledging
the fixture as reconciled. Post-backup sources, correction, deletion and narrowed
permissions survive. There is no route cutover or production failover claim.

```sh
python -m agenthub.cloud_local --profile /private/installed-profile \
  rehearse --tenant acme --restore --provider-free
```

The operator adapter creates unregistered synthetic source/restore test databases
using the private `operator.json`, exercises missing deltas/originals and current
correction/deletion/new-source/privacy checks, and leaves installed tenant sources
unchanged. Only a content-free receipt is returned. Real managed backup/PITR,
object lifecycle, worker dispatch fencing after recovery, crash-consistent physical
backup and cloud IAM must still receive separate acceptance evidence.

Actual offline restore and current-policy reconciliation are separate from that
synthetic rehearsal. The explicit target configuration must be private (0600):
`kind=offline_restore_target_v1`, `tenant`, nonadmin `dsn`, matching `admin_dsn`,
`home`, `objects={kind:file,root:...}`, `serve=false`, `dispatch=false`.
The command verifies actual PostgreSQL server/database identity against every
registered route in the selected current profile; a registered authority, admin
application role, wrong administrative database or public config is rejected.
An initial restore requires empty knowledge tables and remains held. Reconciliation
preserves target writes by refusing to overwrite them; it never registers a route.

```sh
python -m agenthub.cloud_recovery restore --current-profile /private/installed-profile \
  --target-config /private/offline-target.json --snapshot /private/authority-backup
python -m agenthub.cloud_recovery reconcile --current-profile /private/installed-profile \
  --target-config /private/offline-target.json --snapshot /private/authority-backup \
  --output /private/new-current-authority-snapshot
```

`cloud_local backup --output /private/new-backup` backs up the selected actual
authority with individual object checksums. `cloud_local restore --provider-free`
is still a clearly labeled synthetic rehearsal; the explicit commands above
perform actual offline restoration. None provide managed cloud recovery evidence.
