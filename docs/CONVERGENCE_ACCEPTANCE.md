# Source and local runtime convergence

PR #1 was verified at its exact final revision `6e2364f` and merged into `main`
as `89e89f0`. [Final source/provider checks](https://github.com/seankerman/vaelius/actions/runs/38099468705)
and [runtime image](https://github.com/seankerman/vaelius/actions/runs/38099470215)
passed. The maintained checkout has one main branch and no review worktree.

The installed API, owned hooks, MCP credential helper and existing processing
timer now use the same Vaelius release. The old API/processing LaunchAgents and
completed one-time catchup launcher are retired. Old installed snapshots and
private Git repositories remain archives and rollback evidence, not live code.
Python module names, MCP registration and existing private data paths remain
compatibility interfaces; renaming them is not needed to converge the runtime.

| Check | Evidence | Result |
|---|---|---|
| Source | Exact-final-revision CI: 69 client, 1,160 server tests; 47 explicit skips; seven real-provider checks | Pass |
| Installed identity | Client `4870ba949a7258cc259077c0b3cf0745483ec2d55dff2f05bbd538fa2caef0e0`, server `d6441f3cc116f7ca55be7dbfd76f949a76075f502a24278e5eb0ec8c70c6966b` | Matches main |
| Readiness | Both existing tenants now meet schema requirements; live `/ready` returns 200 | Pass |
| Migration | Existing checksums match; only unapplied additive migrations through 034 applied; application-role grants refreshed | Pass |
| Data | Private PostgreSQL backups saved; source/document/lifecycle fingerprints unchanged through migration | Preserved |
| Client profile | Existing projects, sessions, cursors, pause and publication settings preserved | Pass |
| Real delivery | This chat's memory tools authenticate successfully; private HTTP MCP search returns four discovery records with no protocol error | Pass, no usefulness claim |
| Local service renewal | Real credential rotation retains the existing principal/enrollment/actions; subsequent backend authentication succeeds; client-side bearer extension disabled | Pass |
| Renewal guards | Revoked credential and changed scope denied; interrupted rotation recovered; expired active grant renewed in isolated controller tests | Pass |
| Processing | Existing source-first processing timer uses new packages and exits successfully; enrichment remains disabled | Pass |

The readiness failure was an empty secondary tenant missing migrations after
025, not corruption of the populated corpus. No applied checksum was rewritten.
Backups, configuration checkpoints, failed receipts and controller evidence are
private and remain outside Git. The private local operator controller uses the
canonical enrollment and identity checks; admin credentials are never passed to
the client. Enterprise identity-provider profiles continue to use standard OAuth.

The private search took about 3.1 seconds on its first measured request, including
the already-enabled local vector path. This is not the small lexical benchmark
in [auth acceptance](ENTERPRISE_AUTH_ACCEPTANCE.md), and is not a new relevance
experiment. Retrieved previews were partial historical evidence, not verified
complete answers.

Remaining operational work: the Mac's Podman VM is nearly full and needs planned
capacity maintenance. Real-company SSO/MFA, cloud TLS/IAM, identity-provider HA,
directory interoperability and real-team acceptance are still separate deployment
gates. No cloud service, paid provider, broader visibility or new capture scope
was enabled by this convergence.

## Verify

```sh
curl --fail http://127.0.0.1:55486/health
curl --fail http://127.0.0.1:55486/ready
vaelius-client --home /absolute/existing/private/client-profile backend-check
launchctl print gui/$(id -u)/com.vaelius.local.api
launchctl print gui/$(id -u)/com.vaelius.local.processing
```

Use the installed release's executable when it is not on PATH. Rollback restores
the privately saved configuration and previous installed packages while retaining
the same PostgreSQL authority and current denial/deletion state. Do not restore a
database backup over newer user activity merely to change the running binary.
