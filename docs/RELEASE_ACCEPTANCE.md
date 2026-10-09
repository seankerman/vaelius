# Release acceptance

Release: 0.3.0, initial public alpha. Date: 2026-10-09.

| Area | Evidence | Result |
| --- | --- | --- |
| Public export | Reviewed current client/server sources, shared synthetic fixtures and maintained support; fresh single-commit history | Pass |
| Private material | Development campaign archive, runtime profiles, original conversations, credentials and installed snapshots excluded | Pass |
| Packaging | Both distributions built and installed from a separate clean local clone and new Python 3.12 environment; `pip check` | Pass |
| Client | 49 installed tests, including model-free imports, capture/redaction, transport and explicit profile compatibility | Pass |
| Backend | Final Linux CI: 1,139 backend tests, zero failures/errors, 41 declared skips; installed client: 49 tests | Pass; skips remain evidence gaps |
| Recovery | 28 additional original-object, logical restore/reconciliation, source-fault and segment tests; one unrelated explicit-DSN skip | Pass, 27 executed |
| Installed rollback | Actual previous installed package writes against the disposable current schema; withdrawal remains denied after rollback/forward | Pass |
| End-to-end delivery | Actual client hook → loopback HTTP API → PostgreSQL source worker → backend MCP → exact evidence → withdrawal | Pass |
| Container | Clean source build, installed contract verification, non-root image, no developer home/operator credentials in image; actual container API/MCP demo | Pass |
| Public artifacts | Wheels contain runtime packages and licenses, no tests/fixtures/research/profiles; source distributions checked | Pass |
| Secret scan | Gitleaks 8.30.1, official archive checksum verified; final tracked tree, narrow checksum/synthetic-test allowances | Zero unresolved findings |

The initial backend run exposed obsolete fixture assumptions: optional enrichment
was not explicitly enabled, fixture readers were not provisioned, fixture resources
were still expected in runtime wheels, and a preflight treated qualified discovery
as a complete answer. Corrections preserve the source-first production default and
record discovery separately from actual receiving-model grounding. Stateless/fake
observer tests do not establish native provider checkpoint or cache reuse.

## Installed query timing

Fixed workload: 40 invented source records, three natural lexical queries, 30 warm
searches, separate disposable PostgreSQL schemas. This is a small regression check,
not evidence of production scale, semantic quality or ordinary usefulness.

| Metric | Before export cleanup | Final installed packages |
| --- | ---: | ---: |
| Median query time | 41.704 ms | 35.109 ms |
| p95 query time | 48.521 ms | 44.799 ms |
| SQL calls per search | 12 | 12 |
| Nonowner raw-source results | 0 | 0 |
| Model/provider calls | 0 | 0 |

Final package build identities:

- Client: `0f02bca804fec07c440dd80f9dc1807ce5653d7aed34b5d049746f6ad3190200`
- Server: `62ffd5dc94dc4994b1db7d9107bf954e2fb761874595bdb417fdf09c5e7b946f`

Timing differences of this size may be environmental noise. Query latency is
separate from receiving-model response time; no model was invoked in this workload.

## Reproduction

```sh
python scripts/build.py
python -m pip install --no-deps --force-reinstall dist/*.whl
python scripts/dev_services.py start --state /absolute/private/disposable-state
python scripts/test.py --installed --services /absolute/private/disposable-state/services.json
python scripts/demo.py --state /absolute/private/disposable-state
python scripts/measure_query.py --services /absolute/private/disposable-state/services.json --output /absolute/private/query.json
python scripts/check_public_files.py --artifacts dist
python scripts/scan_secrets.py
```

CI runs the installed suite, disposable PostgreSQL, actual HTTP/MCP demo, artifact
checks and secret scan from the public source. A separate runtime-image workflow
builds and verifies the installed image so registry failures do not rerun the full
regression suite. The initial combined CI run passed all source checks, then Docker
Hub returned HTTP 429 on the base-image pull. The image now uses the verified public
ECR mirror of the exact same pinned Python image digest. Numeric batch
bounds are operational controls; they do not establish authorization quotas.

## Remaining acceptance gaps

Some optional tests require a separately declared direct DSN, large-volume fixture,
previous package, or live synthetic Keycloak configuration. Skips are not passes.
Local recovery and the actual previous-writer check above cover important upgrade
and deletion behavior independently, but are not managed-cloud backup evidence.
The extra fault tests emitted resource warnings from development subprocess helpers;
their assertions passed. Those warnings are not a clean-resource audit.

This release does not establish real identity-provider/customer-account acceptance,
ordinary team usefulness, live receiving-agent model grounding, hosted-model quality/
cost/cache behavior, or production cloud readiness. Prior authored/private questions
are not presented as untouched confirmation. No private corpus or model weights are
published. The project website link is configured; website content/hosting is a
separate operator action.
