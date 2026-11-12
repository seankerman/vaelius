# Contributing

Use Python 3.12+ and a virtual environment. Install matching client/server packages,
run `python scripts/build.py`, and reinstall the resulting wheels before checking
installed behavior. Client tests require no server or model runtime. PostgreSQL
integration tests require an explicitly disposable local manifest.

```sh
python scripts/test.py --installed
python scripts/test.py --installed --services /absolute/private/services.json
python scripts/check_public_files.py --artifacts dist
```

Keep tests synthetic and preserve the client/server boundary. Do not commit source
conversations, credentials, profiles, object stores, caches or execution logs.
Follow [AGENTS.md](AGENTS.md). Add behavior-focused regressions for security and
retrieval changes; record installed SQL counts and query latency where applicable.
Report skipped tests and optional-provider gaps accurately.

CI runs the suite in parallel shards (`python scripts/test.py --shard 2/4 ...`), each
against its own disposable PostgreSQL. Test modules are split by the seconds recorded
in `scripts/test_timings.json`; a new test file needs no entry there and lands in a
shard on its own. The `installed-postgres` job fails unless the shard reports add up
to full discovery with every test in exactly one shard. To rebalance, download the
`test-report-*` artifacts of a run on `main` and run
`python scripts/test.py --record-timings <reports>/shard-*.json`. The shard jobs
start their PostgreSQL with `--postgres-host-network` and durability off
(`--postgres-setting fsync=off` and two more): only for a database that is thrown
away, never for one you keep.

CI installs third-party packages only from hash-locked files
(`python -m pip install --require-hashes -r requirements-dev.txt`, then
`python scripts/build.py --no-isolation`). When you change a dependency pin, update
the matching `requirements-*.in` file and regenerate the locks as described in
[RELEASING.md](RELEASING.md#dependency-locks).

Use focused pull requests describing the behavior and verification. Contributions
are submitted under Apache 2.0; preserve third-party attribution. Report security
issues privately using [SECURITY.md](SECURITY.md).
