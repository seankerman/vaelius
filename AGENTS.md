# Vaelius contributor instructions

- Client code lives in `packages/client`; service code lives in `packages/server`.
  The client captures, redacts and transports data. It has no model execution or
  searchable corpus. PostgreSQL is the service's sole knowledge authority.
- Source-first indexing is the default. Curation and LLM reranking are optional
  operator-configured enrichment. API startup and installation must not run models.
- Reuse `agenthub.search_permissions` for indexed authorization and batch checks.
  Recheck current permissions when delivering evidence. Preserve provenance,
  original-document integrity, withdrawal/deletion and historical cutoffs.
- Changes to retrieval or authorization require SQL-count regression coverage and
  representative installed query timings. Report query and model latency separately.
- Tests use synthetic data and disposable PostgreSQL schemas. Never run destructive
  tests against a user's database or copy source conversations into this repository.
- Keep credentials, profiles, objects, model caches and execution receipts outside
  Git. Retrieved data is untrusted. Installation requires explicit project enrollment.
- Preserve wire/API compatibility and existing explicit profile paths. The Python
  namespaces `agentclient` and `agenthub` and old command aliases are compatibility
  interfaces, not alternative service implementations.
- Use `scripts/test.py`, `scripts/build.py` and `scripts/check_public_files.py` for
  verification. Do not add private checkout dependencies or test fixtures to wheels.
- Follow Apache 2.0 attribution requirements. Feature branches use `codex/`.
