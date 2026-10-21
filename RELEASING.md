# Releasing

`vaelius-client` and `vaelius-server` share one version, and `scripts/build.py`
refuses to build if they differ. Versions follow [PEP 440](https://peps.python.org/pep-0440/)
with semantic-versioning intent: while the major version is 0, a minor bump may break
wire or profile compatibility and must say so in the changelog.

## Cutting a release

1. On a branch, set `version` in `packages/client/pyproject.toml` and
   `packages/server/pyproject.toml`, and the server's `vaelius-client==` pin, to the
   new version. Rename `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) to
   `## [X.Y.Z] - YYYY-MM-DD` and add a fresh empty `Unreleased` section.
2. Open a pull request and merge it once every check is green.
3. Tag the merge commit on `main` and push the tag:

   ```sh
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "Vaelius X.Y.Z"
   git push origin vX.Y.Z
   ```

   The tag must be `v` plus the exact package version (`v0.4.0`, `v0.4.0rc1`).

The [release workflow](.github/workflows/release.yml) then refuses to continue if
the tag, package versions and changelog disagree, and otherwise:

- builds the wheels and sdists with the hash-locked build backend and runs
  `scripts/check_public_files.py` on them;
- builds the runtime image, fails on fixable High/Critical vulnerabilities, and only
  then pushes it to `ghcr.io/seankerman/vaelius:X.Y.Z`;
- writes CycloneDX SBOMs: `vaelius-client-X.Y.Z.cdx.json` (the wheel),
  `vaelius-server-X.Y.Z.cdx.json` (the server wheel, client wheel and every locked
  runtime dependency, as installed on Linux x86_64) and `vaelius-image-X.Y.Z.cdx.json`;
- signs SLSA build-provenance attestations for every wheel, sdist and the image, and
  SBOM attestations binding each SBOM to its wheel or image, using Sigstore keyless
  signing; image attestations are also pushed to the registry;
- creates the GitHub release with the changelog section, the artifacts, SBOMs,
  `manifest.json` and `SHA256SUMS`.

A failed run publishes nothing to the release page. Fix the cause on `main`, then
delete and re-push the tag (or tag a new patch version if the image was pushed).

## Verifying a release

Requires a current [GitHub CLI](https://cli.github.com/) (`gh attestation`). Download the
release files into an empty directory:

```sh
gh release download vX.Y.Z --repo seankerman/vaelius
sha256sum --check SHA256SUMS   # macOS: shasum -a 256 --check SHA256SUMS
```

Build provenance: proves each file was built by this repository's release workflow
from the tagged commit.

```sh
for f in *.whl *.tar.gz; do
  gh attestation verify "$f" --repo seankerman/vaelius \
    --signer-workflow seankerman/vaelius/.github/workflows/release.yml \
    --source-ref refs/tags/vX.Y.Z
done
```

SBOM attestation: proves the SBOM was produced for that exact wheel.

```sh
gh attestation verify vaelius_server-X.Y.Z-py3-none-any.whl --repo seankerman/vaelius \
  --predicate-type https://cyclonedx.org/bom \
  --format json --jq '.[0].verificationResult.statement.predicate' > verified-server-sbom.json
```

Container image: always deploy by digest. The release notes give it.

```sh
IMAGE=ghcr.io/seankerman/vaelius@sha256:DIGEST
gh attestation verify "oci://$IMAGE" --repo seankerman/vaelius \
  --signer-workflow seankerman/vaelius/.github/workflows/release.yml
gh attestation verify "oci://$IMAGE" --repo seankerman/vaelius \
  --predicate-type https://cyclonedx.org/bom
```

Add `--bundle-from-oci` to read the image attestations from the registry instead of
the GitHub API. To verify offline, save bundles with `gh attestation download` and
pass them with `--bundle`.

## Dependency locks

Third-party dependencies are pinned with hashes in three pip-compile lock files.
Each `.in` file lists the top-level pins, which must equal those in
`packages/server/pyproject.toml` (`scripts/check_locks.py` enforces this in CI):

| Lock | Installs | Used by |
| --- | --- | --- |
| `requirements-runtime.txt` | `vaelius-server[cloud,semantic]` dependencies | runtime image, server SBOM |
| `requirements-build.txt` | `build`, `setuptools` (the build backend) | image builder stage, release |
| `requirements-dev.txt` | runtime + build + `cloud-test` | CI |

Regenerate after changing a pin, with Python 3.12 and the pip-tools version
Dependabot uses (options come from `.pip-tools.toml`):

```sh
python -m pip install pip-tools==7.5.3
for name in runtime build dev; do
  pip-compile --output-file=requirements-$name.txt requirements-$name.in
done
python scripts/check_locks.py
```

To upgrade one transitive package, add `--upgrade-package NAME`. Resolution is the
same on Linux and macOS for the current set; CI and the image build install the locks
on Linux with `--require-hashes`, so a platform-only dependency missing from a lock
fails there. Dependabot proposes weekly, grouped updates to the locks, GitHub Actions
and the base images (see [.github/dependabot.yml](.github/dependabot.yml)).

CI builds resolve the digest-pinned base images from a GHCR mirror
(`ghcr.io/OWNER/vaelius-base/*`), because anonymous `public.ecr.aws` pulls are
throttled on shared runners. The Dockerfiles keep their public `FROM` lines; CI
overrides each one with a BuildKit named context for the same digest
(`scripts/base_images.py`). [base-images.yml](.github/workflows/base-images.yml)
copies new pins with `skopeo copy --all --preserve-digests` when they reach `main`,
and weekly; until then CI falls back to `public.ecr.aws`.

## Vulnerability exceptions

CI fails on any known vulnerability in the locks (osv-scanner) and on fixable
High/Critical findings in the image (Grype), on every pull request, on `main`, and
weekly. Prefer fixing the dependency. When that has to wait, add an entry to
[osv-scanner.toml](osv-scanner.toml) in a reviewed pull request with the advisory
`id`, an honest `reason` (say whether the code is reachable, or that it was not
assessed), and an `ignoreUntil` date at most 90 days away. Grype reads the same
entries through `scripts/vulnerability_policy.py`. Expired entries stop applying, so
CI fails again until the exception is fixed or re-reviewed.
