#!/usr/bin/env python3
"""Check the tracked release tree, local links and distributable contents."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.jsonl', '.log', '.pem', '.key', '.p12', '.pfx'}
PRIVATE_PATH = re.compile(r'/Users/(?!example(?:/|\b)|demo(?:/|\b)|synthetic(?:/|\b)|alice(?:/|\b)|developer(?:/|\b))[A-Za-z0-9._-]+/')


def files():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True)
    if result.returncode:
        raise RuntimeError('run_in_initialized_release_repository')
    return [Path(name) for name in result.stdout.decode().split('\0') if name]


def check_tree():
    errors = []
    paths = files()
    for relative in paths:
        path = ROOT / relative
        if path.is_symlink():
            errors.append(str(relative) + ': symlinks are not part of the release allowlist')
            continue
        if path.suffix in FORBIDDEN_SUFFIXES or '.env' == path.name:
            errors.append(str(relative) + ': runtime/private extension')
        if any(part in {'.venv', 'site-packages', 'profiles', 'objects', 'receipts', 'models'} for part in relative.parts):
            errors.append(str(relative) + ': runtime data directory')
        if path.suffix not in {'.py', '.md', '.toml', '.json', '.yaml', '.yml', '.txt', '.sql'}:
            continue
        text = path.read_text()
        if PRIVATE_PATH.search(text):
            errors.append(str(relative) + ': personal home path')
        if re.search(r'git@github[.]com:', text) or '.gitmodules' == path.name:
            errors.append(str(relative) + ': private remote/submodule dependency')
        if path.suffix == '.md':
            for match in re.finditer(r'\]\(([^)]+)\)', text):
                target = match.group(1).split('#', 1)[0].strip('<>')
                if not target or '://' in target or target.startswith('mailto:'):
                    continue
                if not (path.parent / target).resolve().exists():
                    errors.append(str(relative) + ': broken link ' + target)
    for folder, namespace in (('client', 'agentclient'), ('server', 'agenthub')):
        package = ROOT / 'packages' / folder / namespace
        if list(package.rglob('fixtures')):
            errors.append(namespace + ': runtime fixtures')
        manifest = json.loads((package / 'BUILD_ID.json').read_text())
        for name, digest in manifest['files'].items():
            if hashlib.sha256((package / name).read_bytes()).hexdigest() != digest:
                errors.append(namespace + ': stale build manifest ' + name)
    staged = subprocess.check_output(['git', 'ls-files', '--stage'], cwd=ROOT, text=True)
    if any(line.startswith('160000 ') for line in staged.splitlines()):
        errors.append('nested Git submodule in release tree')
    return paths, errors


def check_artifacts(directory):
    errors = []
    counts = {}
    for path in sorted(directory.glob('*')):
        if path.suffix == '.whl':
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
            forbidden = [name for name in names if any(part in {'tests', 'fixtures', 'research', 'tools', 'profiles', 'objects', 'models'} for part in Path(name).parts)]
            if forbidden:
                errors.append(path.name + ': non-runtime files in wheel')
            if not any(name.endswith('/LICENSE') for name in names):
                errors.append(path.name + ': missing license')
            counts[path.name] = len(names)
        elif path.name.endswith('.tar.gz'):
            with tarfile.open(path) as archive:
                members = archive.getmembers()
            for member in members:
                parts = Path(member.name).parts
                if member.issym() or member.islnk() or any(p in {'.git', '.venv', 'profiles', 'objects', 'models', 'receipts'} for p in parts):
                    errors.append(path.name + ': excluded file in source distribution')
            counts[path.name] = len(members)
    if not counts:
        errors.append('no release artifacts found')
    return counts, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    paths, errors = check_tree()
    counts = {}
    if args.artifacts:
        counts, artifact_errors = check_artifacts(args.artifacts)
        errors.extend(artifact_errors)
    print(json.dumps({'tracked_files': len(paths), 'artifact_entries': counts, 'errors': errors}, indent=2))
    raise SystemExit(bool(errors))


if __name__ == '__main__':
    main()
