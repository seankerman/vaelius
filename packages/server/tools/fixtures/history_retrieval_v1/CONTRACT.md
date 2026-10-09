# H1 synthetic history fixture contract

`development.json` is the frozen, authored DEV bank. `manifest.json` pins its
bytes and the SHA-256 of a separate private 48-question authored confirmation.
The private body is outside Git and must not be loaded by development runs,
diagnostics, tuning agents or public validator tests. The same fixture author
prepared both banks; confirmation is a separate set of story instances, not
independently authored or natural-use evidence. The S 72-case confirmation keeps
its original manifest and one-use admission gate.

Each source has a synthetic tenant, project, session, speaker, recorded time,
version, exact text hash and current policy audience. `oracle_events` identifies
meaningful occurrences and attributed reports, with actor, status, truth label,
supported rationale, warranted effective time or explicit unknown, ingestion
time, evidence lineage and exact source offsets. `source_order` is recorded-source
order; it is not a fabricated event date. A `transport_replay` is an audit receipt
for the same event key and does not count as a second meaningful failure.
`fixed_curated_records` is an authored input solely for the retrieval-layer test.
It cannot establish extraction success. `runtime_operations` specify ordinary
ingest/update, vector refresh, concurrency, lifecycle and summary transitions.
They are operation expectations, not extra user-visible source claims.

The three layers have distinct denominators:

1. **Source to curated:** ingest synthetic sources and compare produced events to
   `oracle_events` by identity, actor, status, truth, rationale, evidence span,
   policy dependency and time precision. Count missing meaningful occurrences as
   extraction misses. Count an invented adoption, success, reason or date as a
   false positive. The replay receipt is checked for deduplication but excluded
   from the meaningful-occurrence denominator. Routine Poplar cleaning has no
   oracle event and must not become a reusable claim.
2. **Fixed curated retrieval:** load only `fixed_curated_records` into an isolated
   fixture corpus, then answer `questions`. This measures selection, temporal
   semantics, policy, evidence and serialized delivery without crediting the
   extractor. Do not use expected answers or event IDs as retrieval input.
3. **End to end:** ingest the sources, run canonical curation and answer the same
   DEV questions. Report extraction and delivery separately as well as complete
   supported answers. An omitted extracted event is not a retrieval miss.

For a positive question, complete support requires every expected event/facet in
the prose answer, the exact cited source spans or an equally direct source span,
correct actor/status/time qualification, and no forbidden event as a settled
claim. The `expected.event_ids` and `evidence_refs` are the source-grounded oracle;
`expected.answer` is the concise semantic answer, not a string-match key. Count
evidence precision over all delivered claim-bearing citations, including
unsupported extras. A negative passes only with the declared abstention, denial,
or premise correction and without protected or invented content. Historical
queries always apply the current source and reader policy, even at old timestamps.

Report event coverage and order, rationale attribution, current/effective/known
time accuracy, false merges, missed same-scope conflicts, citation precision,
permission and lifecycle violations, and pagination completeness separately.
`known_at` filters by recorded time; `effective_at` uses supported validity, with
unknown effective dates ineligible for dated claims. Source replacement retains
permitted earlier original versions; deletion and revocation suppress all affected
history, summaries, caches and reconstructed observer output. The Oak history
requires all twelve milestone IDs exactly once across stable, serialized pages
of at most 320 bytes, with truthful continuation until complete. The existing S
broad-quality thresholds remain independent. The authored H confirmation has
the H10 thresholds in the execution handoff and can be admitted once only after
DEV safety, history, broad quality and serial gates pass.

Rebuild and verify without provider calls:

```sh
python3 AgentHub/tools/build_history_retrieval_dev_fixtures.py
python3 AgentHub/tools/validate_history_retrieval_fixtures.py
python3 -m unittest AgentHub/tests/test_history_retrieval_fixtures.py
```

Do not rebuild DEV after its frozen hash has been used for behavior tuning. Guard
tests use throwaway toy fixtures; they never read either confirmation body.
