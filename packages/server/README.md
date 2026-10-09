# Vaelius server

The canonical PostgreSQL knowledge service: identity, ingestion, source indexing,
hybrid retrieval, cited evidence, original documents and lifecycle. Continuous
curation and LLM reranking are optional backend processing stages.

Install the client contract and server from the same release:

```sh
python -m pip install ./packages/client './packages/server[cloud,semantic]'
```

Use `vaelius --help`. The `agenthub` command and Python namespace remain available
for compatibility. Keep profiles, credentials and originals outside source control.
