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

For production, mount secrets or use workload identity for S3; configure HTTPS,
[provider OAuth](ENTERPRISE_AUTH.md), tenant routing and lifecycle policies. The backend MCP endpoint
uses those same identity and permission controls. [Provider configuration and
hosting seams](../packages/server/docs/MODEL_PROVIDERS.md) document API models,
local Codex-login execution and explicit source-object adapters.

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
