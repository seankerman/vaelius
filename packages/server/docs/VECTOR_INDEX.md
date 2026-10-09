# PostgreSQL vector retrieval

The canonical service keeps the existing Nomic model and 512-dimensional float
vectors. Migration 028 adds a cosine HNSW index to `cloud_document_vectors`.
This is a rebuildable accelerator over the same records, not another corpus.
The tenant registry routes requests to their tenant database; do not combine
customer vector tables into a shared unpartitioned deployment by assumption.

The nearest-neighbor query orders directly by `<=>` and limits inside SQL.
Its correlated scope check keeps the ordered vector relation eligible for HNSW;
`OFFSET 0` prevents PostgreSQL flattening it into a document-first join/sort. Active
generation, current revision, document lifecycle, project/claim filters and the
restricted reader's current authorization policies all apply before records can
leave PostgreSQL. The outer query resolves ties for returned candidates. Lexical
search and reciprocal-rank fusion stay unchanged. Delivery still rechecks current
permissions. Historical evaluation adapters retain their existing authorized
snapshot scope when using the shared candidate function.

Transactions set `hnsw.iterative_scan=strict_order`, `hnsw.ef_search=100`, and
`hnsw.max_scan_tuples=20000`. Iterative scans continue through rejected neighbors;
this is an approximate scan-work threshold, not a strict tuple/output ceiling or
a permission rule. PostgreSQL can stop before finding every eligible neighbor.
Extremely selective filters can still yield fewer results. The planner may choose exact search when
that is cheaper. No production code disables sequential scans or forces HNSW.
The returned `pgvector_hnsw_eligible` indexing label intentionally does not assert
which plan PostgreSQL chose for a particular request.

These choices follow [pgvector's official indexing and filtering guidance](https://github.com/pgvector/pgvector#filtering).
HNSW trades some recall for speed and needs measured recall under actual filters.
The initial implementation uses float vectors and the standard construction
parameters (`m=16`, `ef_construction=64`). Binary quantization and alternative
embedding models are separate quality decisions, not prerequisites for an index.

## Migration and recovery

1. Check `SELECT extversion FROM pg_extension WHERE extname='vector'`. Iterative
   scans require pgvector 0.8.0 or newer; migration 028 fails before creating an
   index on an older installation. An operator must approve/install an extension
   upgrade separately. This migration never changes the global extension.
2. Apply the normal checksum-verified tenant migration with sufficient maintenance
   time. The local migration builds the index transactionally and locks writes
   while building. A timeout rolls the entire migration and receipt back. It does
   not delete vectors, documents or the active generation.
3. For a populated hosted database, schedule a maintenance window for this
   transaction. This initial migration is **not** an online/concurrent index build
   procedure. Rehearse duration and memory on the destination before deployment.
4. Inspect `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for actual serving SQL and
   compare approximate IDs with an exact diagnostic query ordering by `(embedding <=> query_vector) + 0`.
   This removes the ANN ordering match while preserving B-tree permission lookups.
   Disabling all index scans is misleading because it also slows authorization.
5. Emergency performance rollback: `DROP INDEX cloud_document_vectors_hnsw_cosine`
   in the affected tenant schema. The same SQL falls back to exact distance
   ranking, retaining all vectors and authorization. Preserve the migration
   receipt. Restore with the index DDL from migration 028 under operator control;
   do not rewrite applied migration checksums or delete historical receipts.

This change does not delete retired generations or silently rebuild embeddings.
A large number of retired vectors or very narrow permission scopes can degrade
ANN recall before any confidentiality failure; monitor both returned-result
counts and recall. Per-generation/tenant partitioning is a measured future scaling
choice rather than a second permission system.

## Verification

`tests/test_vector_index.py` freezes deterministic geometry and policy fixtures:
3,000 vectors and a permitted group containing 1% of the records. Coverage includes
exact-versus-ANN top-10 comparison, actual HNSW plan use and bounded SQL calls,
correction/withdrawal denial and index-drop rollback preserving vector rows.
These are mechanics/regression tests, not held-out semantic usefulness evidence.

Run with an installed AgentHub package, `AgentHub/tests` on `PYTHONPATH`, and the
explicit disposable PostgreSQL services manifest in `AGENTNETWORK_PG_SERVICES`:

```sh
python -m unittest test_vector_index -v
```

`AGENTNETWORK_VECTOR_RECEIPT` optionally records source-free exact/indexed query
latencies and installed package identity. These timings exclude embedding model
execution (a deterministic geometry embedder is used), curation and answering.
Installed verification on October 6, 2026 passed all three vector-index tests.
The measured AgentHub build was
`32438d2efaf031a9d194f2948f381c2c8f5988e02abbc6f3f20d5cc790e34908`,
installed at
`~/.local/share/agentnetwork/service-improvements-v1/venv/lib/python3.12/site-packages/agenthub`.

| Candidate-query measurement | Median | Counted application SQL calls |
| --- | ---: | ---: |
| Prior installed exact-search build `820c17b9…` | 141.5 ms | 7 |
| New installed build, diagnostic exact ordering | 155.6 ms | 8 |
| New installed build, HNSW-eligible serving SQL | 61.1 ms | 8 |

Each row uses five queries over the same deterministic 3,000-vector fixture and
1% authorized subset. The additional SQL statement installs transaction-local
HNSW settings; it is constant per vector query. These counts instrument
`PostgresConnection.execute`, not every internal PostgreSQL function or connection
setup statement. The new exact and indexed queries returned the same ten IDs, and
the actual serving EXPLAIN plan used `cloud_document_vectors_hnsw_cosine`.
The paired new-build measurement isolates index ordering from other build changes;
the prior-build measurement records the actual before/after installation boundary.

Source-free receipts remain under
`~/.local/share/agentnetwork/vector-index-v1/before-installed.json` and
`~/.local/share/agentnetwork/service-improvements-v1/vector-after-installed.json`
(with `.plan.json` appended for the plan).

This establishes installed index use, selective-scope recall for this fixture and
preserved lifecycle boundaries. It does **not** establish production-scale recall,
concurrent throughput, real Nomic semantic quality or real-team readiness. The
fixture's 3,000 vectors fit inside the 20,000 approximate scan threshold; it does
not exercise exhaustion on a much larger corpus. Before cloud acceptance, repeat
the exact-versus-ANN comparison on the actual vector distribution and expected
corpus size, including narrow permissions, stale/retired generations, concurrent
updates, tenant isolation and scan-threshold exhaustion. Do not extrapolate the
61.1 ms candidate timing to full hybrid retrieval or end-to-end model response.
