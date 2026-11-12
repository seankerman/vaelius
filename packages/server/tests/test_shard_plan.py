"""The shard plan of scripts/test.py: every test in exactly one shard, heavy modules apart.

No database and no model. CI checks the same property again from the reports of
the shards that actually ran (`scripts/test.py --verify-shards`).
"""
import collections
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[2]
spec = importlib.util.spec_from_file_location('vaelius_test_runner', ROOT / 'scripts/test.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PlanTests(unittest.TestCase):
    timings = {'heavy_a': 194.0, 'heavy_b': 182.0, 'heavy_c': 147.0, 'mid': 40.0,
               **{'small_%02d' % n: 1.0 + n / 10 for n in range(40)}}

    def test_every_module_lands_in_exactly_one_shard_whatever_the_count_or_order(self):
        units = list(self.timings) + ['new_file_without_a_time']
        for count in range(1, 8):
            plan = runner.plan_shards(units, self.timings, count)
            self.assertEqual(len(plan), count)
            self.assertEqual(sorted(unit for shard in plan for unit in shard), sorted(units))
            self.assertEqual(plan, runner.plan_shards(reversed(units + units), dict(reversed(self.timings.items())), count))

    def test_the_heaviest_modules_are_apart_and_shards_are_balanced_by_seconds(self):
        for count in (3, 4, 5):
            plan = runner.plan_shards(self.timings, self.timings, count)
            homes = {unit: index for index, shard in enumerate(plan) for unit in shard}
            self.assertEqual(len({homes['heavy_a'], homes['heavy_b'], homes['heavy_c']}), 3)
            loads = [sum(self.timings[unit] for unit in shard) for shard in plan]
            self.assertLessEqual(max(loads) - min(loads), max(self.timings.values()))
        # By file count the same split would be lopsided: the plan follows seconds.
        sizes = [len(shard) for shard in runner.plan_shards(self.timings, self.timings, 4)]
        self.assertGreater(max(sizes) - min(sizes), 5)

    def test_a_module_with_no_recorded_time_is_planned_at_the_median(self):
        plan = runner.plan_shards(['a', 'b', 'c', 'new'], {'a': 10.0, 'b': 2.0, 'c': 1.0, 'gone': 99.0}, 2)
        self.assertEqual(plan, [['a'], ['b', 'c', 'new']])
        self.assertEqual(runner.plan_shards(['x', 'y'], {}, 2), [['x'], ['y']])

    def test_shard_argument_is_n_of_m(self):
        self.assertEqual(runner.parse_shard('2/4'), (2, 4))
        for bad in ('0/4', '5/4', '2', '2/', 'a/b', '-1/4'):
            with self.assertRaises(Exception):
                runner.parse_shard(bad)


class DiscoveryTests(unittest.TestCase):
    def discover(self, folder, pattern='test*.py'):
        return list(runner.flatten(runner.UnitLoader().discover(str(folder), pattern=pattern)))

    def test_this_suite_is_split_with_every_test_in_exactly_one_shard(self):
        discovered = self.discover(TESTS)
        expected = collections.Counter(runner.key_of(test) for test in discovered)
        self.assertIn('test_shard_plan', {runner.unit_of(test) for test in discovered})
        timings = runner.load_timings(runner.TIMINGS, 'server')
        for count in range(1, 7):
            parts = [runner.select_shard(discovered, timings, index, count) for index in range(1, count + 1)]
            self.assertEqual(sum(len(part) for part in parts), len(discovered))
            self.assertEqual(collections.Counter(runner.key_of(test) for part in parts for test in part), expected)
            modules = [{runner.unit_of(test) for test in part} for part in parts]
            self.assertEqual(sum(len(units) for units in modules), len(set().union(*modules)))

    def test_a_class_imported_into_a_second_module_is_counted_in_both(self):
        # unittest loads an imported TestCase again under the same id; both runs must be planned.
        tag = uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as folder:
            base, user = 'test_shardbase_' + tag, 'test_sharduser_' + tag
            Path(folder, base + '.py').write_text(
                'import unittest\nclass Shared(unittest.TestCase):\n    def test_one(self): pass\n')
            Path(folder, user + '.py').write_text(
                'from %s import Shared\nclass Own(Shared):\n    def test_two(self): pass\n' % base)
            Path(folder, 'test_shardbroken_' + tag + '.py').write_text('import module_that_does_not_exist_' + tag + '\n')
            try:
                discovered = self.discover(folder)
            finally:
                sys.path[:] = [entry for entry in sys.path if entry != folder]
                for name in (base, user):
                    sys.modules.pop(name, None)
        keys = sorted(runner.key_of(test) for test in discovered)
        self.assertEqual(len(keys), 5)
        self.assertEqual(len(set(keys)), 5)
        self.assertEqual(sum(test.id() == base + '.Shared.test_one' for test in discovered), 2)
        # A module that fails to import still belongs to a shard, where it fails.
        self.assertIn('test_shardbroken_' + tag, {runner.unit_of(test) for test in discovered})
        parts = [runner.select_shard(discovered, {}, index, 3) for index in (1, 2, 3)]
        self.assertEqual(sorted(runner.key_of(test) for part in parts for test in part), keys)


class VerifyTests(unittest.TestCase):
    keys = ['test_a::test_a.A.test_1', 'test_a::test_a.A.test_2', 'test_b::test_a.A.test_1', 'test_c::test_c.C.test_1']

    def record(self, shard, selected, *, shards=2, discovered=None):
        discovered = self.keys if discovered is None else discovered
        return {'suite': 'server', 'shard': shard, 'shards': shards, 'selected': sorted(selected),
                'modules': {key.split('::')[0]: {'tests': 1, 'seconds': 1.0} for key in selected},
                'started': len(selected), 'not_started': [], 'tests': len(selected), 'skipped': 0, 'seconds': 1.0,
                'discovered': len(discovered), 'discovered_sha256': runner.digest(discovered)}

    def verify(self, *records):
        with tempfile.TemporaryDirectory() as folder:
            for number, record in enumerate(records):
                Path(folder, 'shard-%d.json' % number).write_text(json.dumps({'suites': [record]}))
                Path(folder, 'shard-%d-server.json' % number).write_text(json.dumps(record))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                runner.verify_shards(folder, ('server',))
            return json.loads(output.getvalue())

    def test_complete_reports_pass_and_say_what_ran(self):
        summary = self.verify(self.record(1, self.keys[:2]), self.record(2, self.keys[2:]))
        self.assertTrue(summary['every_test_in_exactly_one_shard'])
        self.assertEqual((summary['suites'][0]['discovered'], summary['suites'][0]['started']), (4, 4))

    def test_a_missing_shard_a_dropped_test_and_a_repeated_test_all_fail(self):
        cases = {
            'shard_reports_missing': [],
            'shard_reports_missing:server': [dict(self.record(1, self.keys, shards=1), suite='client')],
            'shard_reports_incomplete': [self.record(1, self.keys[:2])],
            'shards_do_not_add_up_to_discovery': [self.record(1, self.keys[:2]), self.record(2, self.keys[3:])],
            'module_in_two_shards': [self.record(1, self.keys[:2]), self.record(2, self.keys[1:])],
            'shards_disagree_on_discovery': [self.record(1, self.keys[:2]),
                                             self.record(2, self.keys[2:], discovered=self.keys[:3])],
            'selected_tests_not_started': [self.record(1, self.keys[:2]),
                                           dict(self.record(2, self.keys[2:]), started=1)],
        }
        for reason, records in cases.items():
            with self.subTest(reason=reason), self.assertRaises(SystemExit) as raised:
                self.verify(*records)
            self.assertIn(reason, str(raised.exception.code))
        # The same test listed by two shards of different modules cannot hide a dropped one.
        with self.assertRaises(SystemExit) as raised:
            self.verify(self.record(1, self.keys[:2]), self.record(2, [self.keys[2], self.keys[2]]))
        self.assertIn('shards_do_not_add_up_to_discovery', str(raised.exception.code))


if __name__ == '__main__':
    unittest.main()
