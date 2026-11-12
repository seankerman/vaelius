#!/usr/bin/env python3
"""Run against installed packages; database tests opt into disposable services.

`--shard N/M` runs one of M parts. Test modules are split by recorded seconds
(`scripts/test_timings.json`); a module with no recorded time gets the median, so
a new test file lands in a shard without any edit. `--verify-shards` proves from
the shard reports that the parts add up to full discovery, each test once.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TIMINGS = Path(__file__).with_name('test_timings.json')


def parse_shard(value):
    index, _, count = value.partition('/')
    if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
        raise argparse.ArgumentTypeError('expected N/M with 1 <= N <= M')
    return int(index), int(count)


def plan_shards(units, timings, count):
    """Split unit names into `count` lists with near-equal recorded seconds.

    Heaviest first onto the lightest shard; ties go to the name, then to the lower
    shard, so every shard job computes the same plan from the same checkout.
    """
    units = sorted(set(units))
    known = [float(timings[unit]) for unit in units if unit in timings]
    default = statistics.median(known) if known else 1.0
    shards = [[] for _ in range(count)]
    loads = [0.0] * count
    for unit in sorted(units, key=lambda unit: (-float(timings.get(unit, default)), unit)):
        target = min(range(count), key=lambda index: (loads[index], index))
        shards[target].append(unit)
        loads[target] += float(timings.get(unit, default))
    return [sorted(shard) for shard in shards]


def load_timings(path, suite):
    """Recorded seconds per test module of one suite; keys are `suite/module`."""
    if not Path(path).exists():
        return {}
    return {key.split('/', 1)[1]: value for key, value in json.loads(Path(path).read_text()).items()
            if key.startswith(suite + '/')}


def select_shard(discovered, timings, index, count):
    """The discovered tests of shard `index` of `count`, in discovery order."""
    mine = set(plan_shards((unit_of(test) for test in discovered), timings, count)[index - 1])
    return [test for test in discovered if unit_of(test) in mine]


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


class UnitLoader(unittest.TestLoader):
    """Remember which discovered module produced each test.

    A test class imported into another test module is loaded again there under the
    same test id, so the id alone does not say which module a test belongs to.
    """
    def loadTestsFromModule(self, module, *, pattern=None):
        suite = super().loadTestsFromModule(module, pattern=pattern)
        for test in flatten(suite):
            test.shard_unit = module.__name__
        return suite


def unit_of(test):
    unit = getattr(test, 'shard_unit', None)
    if unit is None and type(test).__module__ == 'unittest.loader':
        # A module that failed to import or skipped itself: the method name is the module name.
        unit = test._testMethodName
    if unit is None:
        raise RuntimeError('test_without_a_module:' + test.id())
    return unit


def key_of(test):
    return unit_of(test) + '::' + test.id()


def digest(keys):
    return hashlib.sha256('\n'.join(sorted(keys)).encode()).hexdigest()


class TimedResult(unittest.TextTestResult):
    """Charge the time since the previous test ended, fixtures included, to this test."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seconds = []
        self.started = []
        self.fixture_skips = []
        self._mark = time.perf_counter()

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if not isinstance(test, unittest.TestCase):
            # setUpClass or setUpModule skipped: the tests under it never start.
            self.fixture_skips.append(str(test))

    def startTest(self, test):
        super().startTest(test)
        self.started.append(key_of(test))

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        self.seconds.append((now - self._mark, unit_of(test), test.id()))
        self._mark = now


def run_suite(name, args):
    tests = ROOT / 'packages' / name / 'tests'
    discovered = list(flatten(UnitLoader().discover(str(tests), pattern=args.pattern)))
    selected = discovered
    record = {'suite': name, 'installed': args.installed, 'disposable_postgres': bool(args.services)}
    if args.shard:
        index, count = args.shard
        selected = select_shard(discovered, load_timings(args.timings, name), index, count)
        record.update(shard=index, shards=count)
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(unittest.TestSuite(selected))
    modules = {}
    for seconds, unit, _ in result.seconds:
        item = modules.setdefault(unit, {'tests': 0, 'seconds': 0.0})
        item['tests'] += 1
        item['seconds'] = round(item['seconds'] + seconds, 3)
    not_started = sorted((collections.Counter(key_of(test) for test in selected) - collections.Counter(result.started)).elements())
    if not_started and not result.fixture_skips and result.wasSuccessful():
        raise SystemExit('selected_tests_did_not_run:' + ','.join(not_started[:5]))
    record.update(
        tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped), skip_reasons=sorted(set(reason for _, reason in result.skipped)),
        seconds=round(time.perf_counter() - started, 3), modules=modules,
        slowest=[{'seconds': round(seconds, 3), 'test': test}
                 for seconds, _, test in sorted(result.seconds, reverse=True)[:10]],
        # What was handed to the runner here, and what full discovery found.
        selected=sorted(key_of(test) for test in selected), started=len(result.started),
        not_started=not_started, fixture_skips=sorted(result.fixture_skips),
        discovered=len(discovered), discovered_sha256=digest(key_of(test) for test in discovered))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
    if not result.wasSuccessful():
        raise SystemExit(1)


def verify_shards(directory, suites=('client', 'server')):
    """Fail unless the shard reports under `directory` cover full discovery exactly once."""
    found = {}
    for path in sorted(Path(directory).rglob('*.json')):
        data = json.loads(path.read_text())
        for item in data.get('suites', [data]):
            # A shard writes each suite's record twice: on its own and in the combined report.
            if 'shards' in item and found.setdefault((item['suite'], item['shard']), item) != item:
                raise SystemExit(f"two_different_reports_for_one_shard:{item['suite']}:{item['shard']}")
    records = list(found.values())
    if not records:
        raise SystemExit('shard_reports_missing')
    summary = []
    for suite in suites:
        parts = sorted((item for item in records if item['suite'] == suite), key=lambda item: item['shard'])
        if not parts:
            raise SystemExit(f'shard_reports_missing:{suite}')
        count = parts[0]['shards']
        if [item['shards'] for item in parts] != [count] * len(parts) or [item['shard'] for item in parts] != list(range(1, count + 1)):
            raise SystemExit(f'shard_reports_incomplete:{suite}:' + ','.join(f"{item['shard']}/{item['shards']}" for item in parts))
        if len(set(item['discovered_sha256'] for item in parts)) != 1:
            raise SystemExit(f'shards_disagree_on_discovery:{suite}')
        owners = {}
        for item in parts:
            for unit in set(key.split('::', 1)[0] for key in item['selected']):
                owners.setdefault(unit, []).append(item['shard'])
        repeated = sorted(unit for unit, shards in owners.items() if len(shards) > 1)
        if repeated:
            raise SystemExit(f'module_in_two_shards:{suite}:' + ','.join(repeated))
        union = [key for item in parts for key in item['selected']]
        if len(union) != parts[0]['discovered'] or digest(union) != parts[0]['discovered_sha256']:
            raise SystemExit(f'shards_do_not_add_up_to_discovery:{suite}:{len(union)}!={parts[0]["discovered"]}')
        started = sum(item['started'] for item in parts)
        skipped_by_fixture = sum(len(item['not_started']) for item in parts)
        if started + skipped_by_fixture != len(union):
            raise SystemExit(f'selected_tests_not_started:{suite}:{started}+{skipped_by_fixture}!={len(union)}')
        summary.append({'suite': suite, 'shards': count, 'discovered': parts[0]['discovered'], 'started': started,
                        'skipped_by_class_or_module_fixture': skipped_by_fixture,
                        'tests': [item['tests'] for item in parts], 'skipped': sum(item['skipped'] for item in parts),
                        'seconds': [item['seconds'] for item in parts]})
    print(json.dumps({'every_test_in_exactly_one_shard': True, 'suites': summary}, sort_keys=True, indent=2))


def record_timings(reports, target):
    """Rewrite the shard timings from reports written by this script."""
    timings = {}
    for path in reports:
        data = json.loads(Path(path).read_text())
        for item in data.get('suites', [data]):
            for unit, measured in item.get('modules', {}).items():
                timings[item['suite'] + '/' + unit] = round(measured['seconds'], 2)
    target.write_text(json.dumps(timings, sort_keys=True, indent=1) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--services', type=Path)
    parser.add_argument('--suite', choices=('client', 'server', 'all'), default='all')
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--shard', type=parse_shard, metavar='N/M', help='run part N of M')
    parser.add_argument('--timings', type=Path, default=TIMINGS, help='seconds per test module, used to balance shards')
    parser.add_argument('--verify-shards', type=Path, metavar='DIR', help='check the shard reports under DIR and exit')
    parser.add_argument('--record-timings', nargs='+', metavar='REPORT', help='rewrite --timings from reports and exit')
    args = parser.parse_args()
    suites = ('client', 'server') if args.suite == 'all' else (args.suite,)
    if args.verify_shards:
        return verify_shards(args.verify_shards, suites)
    if args.record_timings:
        return record_timings(args.record_timings, args.timings)
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
    process_seconds = {}
    for name in suites:
        if name == 'server':
            sys.path.insert(0, str(ROOT / 'packages' / name / 'tests'))
        # Isolate duplicate test module names between packages in separate processes.
        if args.suite == 'all':
            command = [sys.executable, str(Path(__file__)), '--suite', name, '--pattern', args.pattern,
                       '--timings', str(args.timings)]
            if args.services:
                command += ['--services', str(args.services)]
            if args.installed:
                command += ['--installed']
            if args.shard:
                command += ['--shard', '%d/%d' % args.shard]
            report = (args.report.parent / (args.report.stem + '-' + name + '.json')) if args.report else None
            if report:
                command += ['--report', str(report)]
            started = time.perf_counter()
            subprocess.run(command, check=True)
            # Includes test discovery and the schema cleanup at interpreter exit.
            process_seconds[name] = round(time.perf_counter() - started, 3)
            if report:
                combined.append(json.loads(report.read_text()))
        else:
            run_suite(name, args)
    if args.suite == 'all' and args.report:
        args.report.write_text(json.dumps({'suites': combined, 'process_seconds': process_seconds}, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
