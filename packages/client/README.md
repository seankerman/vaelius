# Vaelius client

Model-free capture, deterministic secret redaction, durable transport, enrollment
and agent delivery for the Vaelius PostgreSQL service. This package has no backend
or model dependencies. It stores credentials and transport state, not a searchable
knowledge corpus.

Install with `python -m pip install ./packages/client` from the Vaelius repository.
Use `vaelius-client --help`. The `agentclient` command and Python namespace remain
available for compatibility. See the repository README and client guide.
