#!/usr/bin/env python3
"""Run against installed packages; database tests opt into disposable services."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services', type=Path)
    parser.add_argument('--suite', choices=('client', 'server', 'all'), default='all')
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.services:
        manifest = json.loads(args.services.read_text())
        if not manifest.get('disposable'):
            raise ValueError('test_runner_requires_explicit_disposable_manifest')
        os.environ['AGENTNETWORK_PG_SERVICES'] = str(args.services.resolve())
        os.environ['CLOUD_TEST_SERVICES'] = str(args.services.resolve())
        if manifest.get('moto_endpoint'):
            os.environ['CLOUD_TEST_MOTO_ENDPOINT'] = manifest['moto_endpoint']
    if args.installed:
        for name in ('agentclient', 'agenthub'):
            module = __import__(name)
            if 'site-packages' not in Path(module.__file__).parts:
                raise RuntimeError('installed_package_required:' + name)
        os.environ['AGENTNETWORK_INSTALLED_TEST'] = '1'
    sys.path.insert(0, str(ROOT / 'packages/server/research'))
    combined = []
    suites = ('client', 'server') if args.suite == 'all' else (args.suite,)
    for name in suites:
        tests = ROOT / 'packages' / name / 'tests'
        if name == 'server':
            sys.path.insert(0, str(tests))
        # Isolate duplicate test module names between packages in separate processes.
        if args.suite == 'all':
            command = [sys.executable, str(Path(__file__)), '--suite', name, '--pattern', args.pattern]
            if args.services:
                command += ['--services', str(args.services)]
            if args.installed:
                command += ['--installed']
            report = (args.report.parent / (args.report.stem + '-' + name + '.json')) if args.report else None
            if report:
                command += ['--report', str(report)]
            subprocess.run(command, check=True)
            if report:
                combined.append(json.loads(report.read_text()))
        else:
            suite = unittest.defaultTestLoader.discover(str(tests), pattern=args.pattern)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            record = {'suite': name, 'tests': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skipped': len(result.skipped),
                      'skip_reasons': sorted(set(reason for _, reason in result.skipped)),
                      'installed': args.installed, 'disposable_postgres': bool(args.services)}
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
            if not result.wasSuccessful():
                raise SystemExit(1)
    if args.suite == 'all' and args.report:
        args.report.write_text(json.dumps({'suites': combined}, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
