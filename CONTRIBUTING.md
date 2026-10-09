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

Use focused pull requests describing the behavior and verification. Contributions
are submitted under Apache 2.0; preserve third-party attribution. Report security
issues privately using [SECURITY.md](SECURITY.md).
