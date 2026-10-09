#!/usr/bin/env python3
"""Run a pinned, checksum-verified Gitleaks against tracked public files only."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '8.30.1'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='use an explicitly supplied scanner')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='vaelius-secret-scan-') as temporary:
        folder = Path(temporary)
        binary = args.binary.resolve() if args.binary else None
        if binary is None:
            system = platform.system().lower()
            arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'AMD64': 'x64'}.get(platform.machine())
            if system not in {'darwin', 'linux'} or arch is None:
                raise ValueError('supply_scanner_binary_for_this_platform')
            name = f'gitleaks_{VERSION}_{system}_{arch}.tar.gz'
            base = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/'
            archive = folder / name
            urllib.request.urlretrieve(base + name, archive)
            checksums = urllib.request.urlopen(base + f'gitleaks_{VERSION}_checksums.txt', timeout=30).read().decode()
            expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == name)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise RuntimeError('scanner_archive_checksum_mismatch')
            binary = folder / 'gitleaks'
            with tarfile.open(archive) as bundle, bundle.extractfile('gitleaks') as stream:
                binary.write_bytes(stream.read())
            binary.chmod(0o700)
        snapshot = folder / 'tracked'; snapshot.mkdir()
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        for name in filter(None, paths):
            source = ROOT / name
            if not source.is_file() or source.is_symlink():
                raise ValueError('only_regular_tracked_release_files_allowed')
            target = snapshot / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        report = folder / 'report.json'
        command = [str(binary), 'dir', '--redact=100', '--no-banner', '--log-level=error',
                   '--report-format=json', '--report-path', str(report)]
        if (ROOT / '.gitleaks.toml').exists():
            command += ['--config', str(ROOT / '.gitleaks.toml')]
        result = subprocess.run(command + [str(snapshot)], capture_output=True, text=True)
        findings = json.loads(report.read_text()) if report.exists() else []
        print(json.dumps({'scanner_version': VERSION, 'tracked_files': len(list(filter(None, paths))),
                          'findings': [{'file': x['File'], 'line': x['StartLine'], 'rule': x['RuleID']} for x in findings],
                          'exit_code': result.returncode}, indent=2))
        if result.returncode not in (0, 1):
            print(result.stderr[-2000:])
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
