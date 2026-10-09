# Supported service boundary

The 0.3 package has one PostgreSQL API/worker path. AgentHub owns processing,
model defaults (GPT-6 Luna, low reasoning), continuous contexts, embeddings,
identity, policy, source objects, curation, retrieval and lifecycle.

`agenthub doctor --profile PROFILE` checks local configuration;
`python -m agenthub.cloud_runtime --profile PROFILE` starts the API;
`agenthub worker-run --profile PROFILE --tenant TENANT` runs a finite source-index
and local-embedding batch. Curation/consolidation are disabled by default;
explicit enrichment also requires `processing.curation_enabled: true` and `--live`.
See [source-first defaults and rollback](SOURCE_FIRST.md).
`python -m agenthub.cloud_recovery --help` exposes real offline restore/reconcile
operations. Rehearsals and research runners are in `research/` and are not wheels.

Use `PYTHONPATH=.:research python -m unittest discover -s tests` for source tests.
Set `AGENTNETWORK_PG_SERVICES` to a private local services manifest for disposable
PostgreSQL schema tests. Never use a production database for destructive tests.

The small `vaelius-client==0.3.0` dependency provides capture/transport contracts.
`CLIENT_PIPELINE_PIN.json` now names fully qualified modules: processing belongs to
this wheel, client pins cover only wire contracts and cleaning. Independent clone
builds require no sibling source checkout; only the pinned client wheel.

Old installed artifacts and stored data remain intact. Source cleanup does not
switch a running profile. Research fixture adapters use disposable PostgreSQL schemas and the canonical
processing implementation; the copied SQLite fixture applications have been deleted. Unchanged historical migration files preserve checksums;
new outcome reports use additive migration 027. Older binaries can still read the
unchanged tables; rollback preserves the new self-report table rather than deleting
reports. Test that assertion on a disposable schema before any profile cutover.
