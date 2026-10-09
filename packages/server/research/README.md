# Non-shipped research support

This directory contains retained receiving-agent/operational helpers and small PostgreSQL
fixture adapters. It is excluded from the AgentHub wheel. Production processing
lives in `agenthub.processing`; AgentClient has no observer or knowledge database.

Use `PYTHONPATH=research` from this repository when running these helpers against
installed 0.3 packages. They are development tools, not an alternate serving path.
Set `AGENTNETWORK_PG_SERVICES` to an explicit private services manifest for database
fixtures. They migrate disposable schemas and invoke the installed canonical
implementation; no standalone client or SQLite service is copied here.

Old evidence remains evidence of the exact installed build and fixture it names;
relocating a helper does not make its prior result current or held out.

Current search always uses indexed authorization. Old authorization-shape labels
are accepted only when loading existing runtime configuration and normalize to
the compiled implementation; they no longer select competing SQL implementations.
Do not present old multi-shape experiments as a new comparison without updating
the experiment definition and recording the actual selected runtime.

The deterministic continuous-observer rehearsal uses an in-process fake session
handle. Actual Codex mode still verifies the native session checkpoint and
reconstructs context when that checkpoint is unavailable. A fake-session test is
not evidence of live provider cache reuse.
