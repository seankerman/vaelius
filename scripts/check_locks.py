#!/usr/bin/env python3
"""Check that hashed lock files are complete and agree with package metadata."""
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)')


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def pins(lines):
    result = {}
    for line in lines:
        match = PIN.match(line.strip())
        if match:
            result[normalize(match.group(1))] = match.group(2)
    return result


def read_in(name, seen=None):
    """Top-level pins of a .in file, following -r includes."""
    seen = seen or set()
    path = ROOT / name
    if path in seen:
        return {}
    seen.add(path)
    result = {}
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line.startswith('-r '):
            result.update(read_in(line[3:].strip(), seen))
        elif line:
            result.update(pins([line]) or {normalize(line): None})
    return result


def read_lock(name):
    """Map each locked package to (version, hashes); lists unhashed entries."""
    entries, errors = {}, []
    text = (ROOT / name).read_text().replace('\\\n', ' ')
    for line in text.splitlines():
        line = line.split(' #', 1)[0].strip()
        if not line or line.startswith('#'):
            continue
        match = PIN.match(line)
        if not match:
            errors.append(f'{name}: unpinned requirement {line.split()[0]}')
            continue
        hashes = frozenset(re.findall(r'--hash=(sha256:[0-9a-f]{64})', line))
        if not hashes:
            errors.append(f'{name}: {match.group(1)} has no --hash')
        entries[normalize(match.group(1))] = (match.group(2), hashes)
    return entries, errors


def main():
    errors = []
    server = tomllib.loads((ROOT / 'packages/server/pyproject.toml').read_text())
    extras = server['project']['optional-dependencies']
    build_requires = {}
    for package in ('client', 'server'):
        project = tomllib.loads((ROOT / 'packages' / package / 'pyproject.toml').read_text())
        build_requires.update(pins(project['build-system']['requires']))
    expected = {
        'requirements-runtime.in': pins(extras['cloud'] + extras['semantic']),
        'requirements-build.in': build_requires,
    }
    expected['requirements-dev.in'] = {**expected['requirements-runtime.in'],
                                       **expected['requirements-build.in'],
                                       **pins(extras['cloud-test'])}
    locks = {}
    for source, wanted in expected.items():
        declared = read_in(source)
        for name, version in wanted.items():
            if declared.get(name) != version:
                errors.append(f'{source}: {name} must be pinned to {version} as in pyproject.toml')
        lock_name = source.removesuffix('.in') + '.txt'
        locks[lock_name], lock_errors = read_lock(lock_name)
        errors.extend(lock_errors)
        for name, version in declared.items():
            locked = locks[lock_name].get(name)
            if locked is None or (version is not None and locked[0] != version):
                errors.append(f'{lock_name}: stale for {name} (rerun pip-compile)')
    dev = locks['requirements-dev.txt']
    for lock_name in ('requirements-runtime.txt', 'requirements-build.txt'):
        for name, entry in locks[lock_name].items():
            if dev.get(name) != entry:
                errors.append(f'requirements-dev.txt: {name} differs from {lock_name}')
    print(json.dumps({'locks': {k: len(v) for k, v in locks.items()}, 'errors': errors}, indent=2))
    raise SystemExit(bool(errors))


if __name__ == '__main__':
    main()
