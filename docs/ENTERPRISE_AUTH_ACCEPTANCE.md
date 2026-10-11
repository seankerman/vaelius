# Enterprise authentication acceptance

Production source is committed in `b3022983d8eeb7e5ae42f8dfe87a47a485275b62`.
The follow-up changes only strengthen the hook acceptance test and documentation.
No live profile, global hooks, customer data or model provider was changed.

| Area | Evidence | Result |
|---|---|---|
| Three PR regressions | Frozen tests failed against submitted PR; delegation reduction, plaintext-keyring rejection and short-token caching pass in installed wheels | Pass |
| Client | 69 installed tests, zero failures/errors/skips | Pass |
| Real provider | Seven installed Keycloak/PostgreSQL checks: code/PKCE, hook intake, indexing, MCP, refresh, revocation, offboarding, profile migration, rollback and SQL bounds | Pass |
| Profile continuity | Expired legacy profile retains its existing capture connection; reauthentication retains projects, sessions and outbox; account switching denied | Pass |
| Refresh recovery | Saved replies recover; ambiguous dispatch never repeats; interrupted login preserves profile metadata | Pass |
| Previous installed binary | One actual previous-writer test preserves withdrawal and writes on the same disposable authority | Pass |
| Canonical query timing | 40 sources, three lexical queries, 30 warm searches: median 40.242 → 38.665 ms; p95 42.621 → 40.838 ms; SQL calls remain 12 | Pass, small synthetic regression only |
| Provider authentication | Five warm local samples: median 94.847 ms, 10 SQL calls each; separate from retrieval and model time | Pass |
| Actual default pipeline | Installed hook → HTTP → source worker → MCP/evidence → withdrawal; secret redaction and empty acknowledged outbox verified | Pass |
| Packaging/privacy | Matching wheels/sdists, package boundaries and public-file checks; Gitleaks 8.30.1 reports zero findings | Pass |
| Non-root runtime image | Clean Linux CI build and installed provider import/pipeline integrity | Pass |
| Broad regressions | Clean Linux: 69 client tests and 1,160 server tests, zero failures/errors; 47 server skips require separate fixtures; seven additional real-provider checks pass without skips | Pass |

Measured installed identities:

- Client: `4870ba949a7258cc259077c0b3cf0745483ec2d55dff2f05bbd538fa2caef0e0`
- Server: `d6441f3cc116f7ca55be7dbfd76f949a76075f502a24278e5eb0ec8c70c6966b`

Successful [source/provider CI](https://github.com/seankerman/vaelius/actions/runs/38096668159)
and [runtime image CI](https://github.com/seankerman/vaelius/actions/runs/38096668065)
ran on `b3022983d8eeb7e5ae42f8dfe87a47a485275b62`. Final production modules
and build IDs are identical; the subsequent commit strengthens the hook test and
records acceptance documentation. That exact hook path also passes in installed
local wheels. No skipped test is counted as passing.

The full Mac run was invalidated by Podman's full disk: 361 database/capacity
errors, zero assertion failures and 41 skips. The second local broad run was
stopped before further exhaustion. Failed receipts are retained privately; neither
run is counted as passing. Only resources created for this task were removed.
The original historical PostgreSQL container remains available. The Mac VM still
needs capacity maintenance before further broad/container runs.

These are authored conformance fixtures and known regression reproductions, not
held-out retrieval evidence or real-team acceptance. Customer federation/SAML,
MFA/conditional-access policy, production TLS, identity-server HA/backup, secret
rotation, directory interoperability, device binding and deployment/load review
remain operational acceptance work. Introspection adds provider network latency;
the local timing is not a cloud SLA. No AI calls were made by these checks.

## Reproduce

```sh
python scripts/build.py
python -m pip install --no-deps --force-reinstall dist/*.whl
python scripts/dev_services.py start --state /absolute/private/test-services
python scripts/identity_fixture.py start --state /absolute/private/test-identity \
  --resource http://127.0.0.1:API_PORT/mcp
VAELIUS_IDENTITY_FIXTURE=/absolute/private/test-identity/identity.json \
  python scripts/test.py --installed --services /absolute/private/test-services/services.json
python scripts/measure_query.py --services /absolute/private/test-services/services.json \
  --output /absolute/private/query-timing.json
python scripts/demo.py --state /absolute/private/test-services
python scripts/check_public_files.py --artifacts dist
python scripts/scan_secrets.py
python scripts/identity_fixture.py destroy --state /absolute/private/test-identity
python scripts/dev_services.py destroy --state /absolute/private/test-services
```

Use only disposable services. Substituting an existing user/customer database is
not supported. [Configuration, migration and rollback](ENTERPRISE_AUTH.md).
