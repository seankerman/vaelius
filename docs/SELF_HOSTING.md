# Self-hosting

The service uses PostgreSQL 17 with pgvector. The local development setup uses
`pgvector/pgvector:0.8.6-pg17` and the Moto S3 emulator. These are disposable test
services; a cloud provider is not selected automatically.

`scripts/dev_services.py start --state DIRECTORY` creates an isolated container
namespace, restrictive application/router roles and a private profile. It seeds
synthetic identities, not real source material. It prints profile and manifest
paths without credentials. `stop` pauses only containers carrying its exact
ownership label; a later `start` resumes them without resetting database state,
permissions or credentials. `destroy` explicitly removes those containers and their
ephemeral databases; use a new state directory afterward. Private state files
remain available for inspection or explicit deletion. Do not commit them.

The API uses application credentials; migration/provisioning credentials remain
in a separate operator file. `runtime.json` must be mode 0600 or 0400. Runtime and
worker configuration are explicit. Startup does not enable model calls, source
collection or curation. Finite workers consume PostgreSQL jobs; repeat them as needed.

## Container

Build from the public source tree:

```sh
docker build -t vaelius:local .
# Substitute your runtime UID/GID and an already prepared private profile:
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user UID:GID --tmpfs /tmp:rw,nosuid,size=256m \
  -p 127.0.0.1:8080:8080 -v /absolute/private/profile:/profile:rw vaelius:local
```

The image contains installed packages, no operator credentials, original sources,
development tests or model caches. Configure network-reachable PostgreSQL/S3
endpoints in a separate container profile. Host loopback addresses do not refer to
host services from inside a container. A profile using an in-container endpoint
override must name exactly the intended local service network. Do not silently
rewrite identity-broker issuer URLs.

The image declares a Docker `HEALTHCHECK` that calls the unauthenticated `/health`
liveness endpoint on port 8080 with the image's own Python, so it needs no shell or
curl. It checks only that the API answers; `/ready` checks the database. Kubernetes
ignores `HEALTHCHECK` (use `httpGet` probes on the same paths), Podman/Buildah drop it
unless built with `--format docker`, and a different `--port` needs a matching
`--health-cmd` or `--no-healthcheck`. The check uses plain HTTP with `Host:
127.0.0.1`, so a production profile with `tls: "direct"`, or an `allowed_hosts` list
without `127.0.0.1`, needs the same override (see HTTPS below). Released images are on
`ghcr.io` with signed provenance and SBOMs; pull them by digest and verify them as in
[RELEASING.md](../RELEASING.md#verifying-a-release).

For production, mount secrets or use workload identity for S3; configure HTTPS
(below), [provider OAuth](ENTERPRISE_AUTH.md), tenant routing and lifecycle policies. The backend MCP endpoint
uses those same identity and permission controls. [Provider configuration and
hosting seams](../packages/server/docs/MODEL_PROVIDERS.md) document API models,
local Codex-login execution and explicit source-object adapters.

## HTTPS and production transport

The container listens with plain HTTP on port 8080. That is only acceptable on
loopback or behind a TLS-terminating proxy or load balancer. Bearer tokens, refresh
tokens and source evidence must never cross a network in clear text. A profile
declares production in `runtime.json`; the API then refuses to start unless it also
declares how TLS is provided and an exact `allowed_hosts` list (no wildcards):

```json
{"allowed_hosts": ["memory.example.org"],
 "allowed_origins": [],
 "deployment": {"mode": "production", "tls": "proxy",
                "trusted_proxies": ["10.0.0.0/16"]}}
```

- `tls: "proxy"` lists the proxy addresses or networks (`0.0.0.0/0` and `*` are
  rejected). Only those peers may set `X-Forwarded-Proto`. The proxy must terminate
  TLS 1.2 or later, overwrite rather than append `X-Forwarded-Proto` and
  `X-Forwarded-For`, and keep the public `Host`. Do not publish the container port
  anywhere except to the proxy.
- `tls: "direct"` names absolute `certfile` and `keyfile` paths. The key file must
  be mode 0600 or 0400. The service then serves HTTPS itself.

In production, every request except `/health` and `/ready` must arrive over HTTPS,
or it is refused with `403 https_required`. Responses carry
`Strict-Transport-Security`. All responses carry `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer` and a deny-all `Content-Security-Policy`. Without `deployment`, the
profile is development: plain HTTP with a warning when bound beyond loopback.

`/health` is unauthenticated liveness only. Build identity is available at
`GET /enterprise/v3/service-status` to credentials holding the `audit` or
`settings` action. The MCP endpoint applies the MCP SDK's DNS-rebinding protection
to the same `allowed_hosts`/`allowed_origins`, in addition to the API host check.

## Recovery and upgrades

Keep database/profile/object backups together with their revision provenance.
Use additive migrations and the matching client/server release. Stop old workers
before changing provider identities or processing configuration; reconstruct
authorized observer context when checkpoints cannot be resumed.

`python -m agenthub.cloud_recovery --help` exposes restore/reconciliation operations.
Recovery must retain denial/deletion journals and recheck permissions; a restored
snapshot must not resurrect withdrawn content. Never rehearse destructive recovery
against a user's live database. [Source-first rollback](../packages/server/docs/SOURCE_FIRST.md)
documents the raw-passage hold required before returning to an older binary.
