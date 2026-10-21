#!/usr/bin/env python3
"""Mirror the digest-pinned public.ecr.aws base images to GHCR, and point CI builds at them.

Anonymous pulls from public.ecr.aws are throttled on shared GitHub runners (HTTP 429
"Data limit exceeded" while resolving the manifest). The Dockerfiles keep their
public FROM lines; CI overrides each one with a BuildKit named context that resolves
the same digest from ghcr.io. A copy that cannot preserve the digest fails, and a
missing mirror falls back to the original reference.

  base_images.py mirror PREFIX            copy missing images (skopeo; run after login)
  base_images.py contexts PREFIX FILE...  print --build-context flags for mirrored images
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = ('Dockerfile', 'containers/Dockerfile.test-postgres')
FROM = re.compile(r'^FROM\s+(public\.ecr\.aws/docker/library/([a-z0-9._-]+):([A-Za-z0-9._-]+)'
                  r'@(sha256:[0-9a-f]{64}))(?:\s|$)', re.M)


def pinned(files):
    images = {}
    for name in files:
        for source, repository, tag, digest in FROM.findall((ROOT / name).read_text()):
            images[source] = (repository, tag, digest)
    return images


def mirror_ref(prefix, repository, suffix):
    return f'{prefix}/{repository}{suffix}'


def exists(command, reference):
    try:
        return subprocess.run([*command, reference], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('mirror', 'contexts'))
    parser.add_argument('prefix', help='for example ghcr.io/OWNER/vaelius-base')
    parser.add_argument('files', nargs='*')
    args = parser.parse_args()
    if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+', args.prefix):
        raise SystemExit('prefix must be a lower-case ghcr.io path')
    images = pinned(args.files or DOCKERFILES)
    if not images:
        raise SystemExit('no digest-pinned public.ecr.aws base images found')
    if args.action == 'mirror':
        for source, (repository, tag, digest) in sorted(images.items()):
            if exists(['skopeo', 'inspect', '--raw'], 'docker://' + mirror_ref(args.prefix, repository, '@' + digest)):
                print('present', source, file=sys.stderr)
                continue
            # skopeo rejects a reference with both a tag and a digest; copy by digest.
            subprocess.run(['skopeo', 'copy', '--all', '--preserve-digests',
                            'docker://public.ecr.aws/docker/library/' + repository + '@' + digest,
                            'docker://' + mirror_ref(args.prefix, repository, ':' + tag)],
                           check=True)
            print('mirrored', source, file=sys.stderr)
        return
    flags = []
    for source, (repository, tag, digest) in sorted(images.items()):
        target = mirror_ref(args.prefix, repository, '@' + digest)
        if exists(['docker', 'buildx', 'imagetools', 'inspect', '--raw'], target):
            # The context name must equal the FROM reference exactly (tag and digest).
            flags.append(f'--build-context={source}=docker-image://{target}')
        else:
            print('not mirrored, using', source, file=sys.stderr)
    print(' '.join(flags))


if __name__ == '__main__':
    main()
