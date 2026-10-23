# Security reporting

Report vulnerabilities through GitHub's private vulnerability reporting page:

https://github.com/seankerman/vaelius/security/advisories/new

Do not put credentials, source conversations, customer records or exploit details
in a public issue. A minimal synthetic reproduction is preferable. There is no
guaranteed response-time SLA for this early community release.

The release has local synthetic security and lifecycle coverage. It is not a
claim of a completed independent security audit or production cloud acceptance.
See [data boundaries](docs/DATA_BOUNDARIES.md) for redaction and operator-trust limits.

Releases carry CycloneDX SBOMs and Sigstore-signed build-provenance attestations;
[RELEASING.md](RELEASING.md#verifying-a-release) shows how to verify them and how
vulnerability exceptions are reviewed.

## Known issues fixed

- Hook and MCP header-helper commands written by the Codex installation and the
  Claude Code settings installation of `vaelius-client` 0.3.0 (and development
  builds before the fix) let a project folder's own `agentclient` package run in
  place of the client. Re-run the install command; see
  [hook commands and the project folder](docs/CLIENT.md#hook-commands-and-the-project-folder).
