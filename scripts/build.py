#!/usr/bin/env python3
"""Build matching Vaelius wheels/sdists and regenerate package integrity manifests."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    client = ROOT / 'packages/client'
    server = ROOT / 'packages/server'
    versions = [tomllib.loads((p / 'pyproject.toml').read_text())['project']['version'] for p in (client, server)]
    if versions[0] != versions[1]:
        raise ValueError('client_and_server_release_versions_must_match')
    pin = {'client_version': versions[0], 'contract_version': 'enterprise-local-2', 'modules': {}}
    for prefix, folder, names in (
        ('agentclient', client / 'agentclient', ['enterprise_contract', 'general_contract', 'cloud_contract', 'cleaning']),
        ('agenthub.processing', server / 'agenthub/processing', [p.stem for p in (server / 'agenthub/processing').glob('*.py')]),
    ):
        for name in sorted(names):
            pin['modules'][prefix + '.' + name] = hashlib.sha256((folder / (name + '.py')).read_bytes()).hexdigest()
    (server / 'agenthub/CLIENT_PIPELINE_PIN.json').write_text(json.dumps(pin, sort_keys=True, indent=2) + '\n')
    for folder, package in ((client, 'agentclient'), (server, 'agenthub')):
        sources = folder / package
        hashes = {
            str(p.relative_to(sources)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(sources.rglob('*'))
            if p.is_file() and p.suffix in ('.py', '.json', '.sql', '.html')
            and p.name != 'BUILD_ID.json' and '__pycache__' not in p.parts
        }
        build_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        (sources / 'BUILD_ID.json').write_text(json.dumps({'build_id': build_id, 'files': hashes}, sort_keys=True, indent=2) + '\n')
        shutil.rmtree(folder / 'build', ignore_errors=True)
        subprocess.run([sys.executable, '-m', 'build', '--wheel', '--sdist', '--outdir', str(output), str(folder)], check=True)
    artifacts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.suffix in ('.whl', '.gz')}
    (output / 'manifest.json').write_text(json.dumps({'version': versions[0], 'artifacts': artifacts}, sort_keys=True, indent=2) + '\n')
    print(json.dumps({'version': versions[0], 'artifacts': artifacts}, sort_keys=True))


if __name__ == '__main__':
    main()
