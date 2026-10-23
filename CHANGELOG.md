# Changelog

Notable changes to `vaelius-client` and `vaelius-server`, which are released
together under one version. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The release workflow publishes the section whose heading matches the tag.

## [Unreleased]

### Security

- Hash-locked dependencies: `requirements-runtime.txt`, `requirements-build.txt`
  and `requirements-dev.txt` (pip-compile with `--generate-hashes`) replace
  `requirements-runtime.lock`. The image and CI install them with `--require-hashes`
  and build wheels without fetching an unverified build backend.
- `starlette` 1.9.0 (cloud extra and hashed locks) fixes six advisories; the
  server dispatches on the ASGI path rather than the Host-derived `request.url`.
- `certifi` 2026.10.19 removes the emSign root whose intermediate CA keys were
  compromised (GHSA-6q48-6c7w-9gw2).
- Runtime base image updated to Python 3.12.15 (`python:3.12.15-slim-trixie`),
  fixing Python and OpenSSL vulnerabilities that the new image scan reported.
- CI audits the locked dependencies with osv-scanner and scans the runtime image
  with Grype, failing on fixable High/Critical findings. Reviewed, expiring
  exceptions live in `osv-scanner.toml`.
- Releases publish CycloneDX SBOMs for both wheels and the image, and Sigstore-signed
  GitHub build-provenance and SBOM attestations. See [RELEASING.md](RELEASING.md).
- Dependabot (weekly, grouped, 7-day cooldown), an OpenSSF Scorecard workflow, and
  least-privilege, credential-free checkouts in every workflow.

### Added

- Docker `HEALTHCHECK` against the unauthenticated `/health` endpoint.
- One sharing ladder, `private` < `team` < `organization`, for conversation passages
  and, separately, for originals. Effective sharing is the minimum of the author's own
  setting (`vaelius-client sharing`, default private), the scope-file request, the
  project and tenant ceilings and the connection visibility, re-applied on every
  request. Retrieval is project-scoped, with other projects' organization-shared
  content as a labelled later tier and an explicit `scope = "all"`. Migration 045;
  see [the privacy model](docs/PRIVACY_MODEL.md).

## [0.3.0] - 2026-10-09

Initial public alpha. See [release acceptance](docs/RELEASE_ACCEPTANCE.md).
