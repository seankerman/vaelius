# Local PostgreSQL service

See the maintained [self-hosting guide](../../../docs/SELF_HOSTING.md) for
explicit disposable services, installation, containers and recovery.

The supported path is source-first PostgreSQL ingestion, indexed authorization,
search and cited evidence through the backend MCP service. Clients capture and
transport; optional model enrichment and reranking run only on the backend.

```sh
vaelius doctor --profile /absolute/private/profile
vaelius status --profile /absolute/private/profile
vaelius worker-run --profile /absolute/private/profile --tenant acme --max-jobs 20 --max-seconds 60
python -m agenthub.cloud_runtime --profile /absolute/private/profile --port PORT
```

Profiles and original source objects remain outside the repository. Use application
credentials for the API and separate operator credentials for provisioning and
migrations. See [source-first defaults](SOURCE_FIRST.md), [model providers](MODEL_PROVIDERS.md),
[backend MCP](BACKEND_MCP.md), and [release acceptance](../../../docs/RELEASE_ACCEPTANCE.md).

The numeric worker options bound one batch; repeat batches until authorized work
drains. They are operational controls, not lifetime usage quotas.
