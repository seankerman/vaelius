"""Operator backfill of server-side conversation history: always owner-private.

Synthetic events and a disposable PostgreSQL schema; no real archive or export.
"""
import json
from pathlib import Path
import tempfile
import unittest

from agenthub import server_backfill
from test_cloud_postgres import PostgresFixture, SERVICES

FULL = ['ingest', 'read', 'source_read', 'withdraw', 'share', 'policy']


def line(**fields):
    value = {'session': 'chat-1', 'timestamp': '2026-03-01T17:04:05Z', 'author': 'alice@example.invalid',
             'workspace': 'maple-repo', 'role': 'user', 'text': 'Where is the maple import kept?'}
    value.update(fields)
    return json.dumps(value)


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schemas required')
class ServerBackfillTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.alice = self.store.authenticate(self.store.enroll('acme', 'alice', 'alice-full', FULL))
        self.bob = self.store.authenticate(self.tokens['bob'])
        # Everything that could widen a live capture is set wide: the workspace inherits,
        # the organization reads it, and both people share by default.
        self.store.operator_workspace('settings', 'maple', item_default='shared')
        self.store.operator_workspace('set-entry', 'maple', subject_kind='organization', role='reader')
        for ctx in (self.alice, self.bob):
            self.store.sharing_setting(ctx, {'contributions_default': 'shared'})
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.input = folder / 'export';self.input.mkdir()
        (self.input / 'a.jsonl').write_text('\n'.join([
            line(),
            line(role='tool', tool='Read', paths=['src/import.py'], text='def load(): ...'),
            line(role='assistant', text='It is loaded by src/import.py.'),
            line(session='chat-2', role='user', text='Rotate the staging keys? token=ghp_' + 'A' * 36),
            line(author='bob@example.invalid', text='Bob asks in maple.'),
            line(author='mallory@example.invalid'),              # unmapped author
            line(workspace='elsewhere'),                         # unmapped workspace
            line(author='carol@example.invalid'),                # mapped to a principal that does not exist
            line(author='admin@example.invalid'),                # not a writer in maple
            line(sharing='organization'),                        # no sharing field exists: invalid
            '{not json',
        ]) + '\n')
        self.mapping = folder / 'map.json'
        self.mapping.write_text(json.dumps({
            'principals': {'alice@example.invalid': 'alice', 'bob@example.invalid': 'bob',
                           'carol@example.invalid': 'carol', 'admin@example.invalid': 'admin'},
            'workspaces': {'maple-repo': 'maple'}}))

    def tearDown(self):
        self.postgres_teardown()

    def run_backfill(self, **options):
        return server_backfill.run(self.store, self.input, self.mapping, run_by='synthetic-operator', **options)

    def sources(self):
        with self.store.open() as state:
            return [dict(r) for r in state.db.execute(
                "SELECT s.*,r.connection FROM enterprise_sources s JOIN backend_source_revisions r ON r.source_id=s.id "
                "WHERE r.connection LIKE 'backfill-%' ORDER BY s.created")]

    def visible(self, ctx, source):
        with self.store.open() as state:
            row = state.db.execute('SELECT * FROM enterprise_sources WHERE id=?', (source['id'],)).fetchone()
            return self.store._visible_source(state.db, ctx, row, raw=True)

    def test_dry_run_counts_only_and_writes_nothing_but_the_audit_row(self):
        report = self.run_backfill()
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['skipped'], {'invalid': 2, 'unmapped_author': 1, 'unmapped_workspace': 1,
                                             'unknown_principal': 1, 'not_writer': 1, 'unknown_workspace': 0})
        self.assertEqual([(g['principal'], g['workspace'], g['sessions'], g['events']) for g in report['groups']],
                         [('alice', 'maple', 2, 4), ('bob', 'maple', 1, 1)])
        self.assertNotIn('maple import', json.dumps(report))
        self.assertEqual(self.sources(), [])
        with self.store.open() as state:
            self.assertIsNone(state.db.execute("SELECT 1 FROM backend_connections WHERE id LIKE 'backfill-%'").fetchone())
        audit = self.audit()
        self.assertEqual([(a['actor'], a['disposition']) for a in audit], [('local_operator', 'dry_run')])
        recorded = json.loads(audit[0]['target_id'])
        self.assertEqual((recorded['run_by'], recorded['groups']), ('synthetic-operator', [['alice', 'maple', 4], ['bob', 'maple', 1]]))
        self.assertNotIn('maple import', audit[0]['target_id'])

    def audit(self):
        with self.store.open() as state:
            return [dict(r) for r in state.db.execute(
                "SELECT actor,disposition,target_id FROM enterprise_audit WHERE operation='server_backfill' ORDER BY id")]

    def test_mapped_events_land_owner_private_and_no_wider(self):
        report = self.run_backfill(apply=True)
        self.assertEqual(report['written']['accepted'] + report['written']['redacted'], 5, report)
        sources = self.sources()
        self.assertEqual(len(sources), 5)
        self.assertEqual({s['owner'] for s in sources}, {'alice', 'bob'})
        # Private to the owner, whatever the workspace, the people and the organization entry say.
        for source in sources:
            self.assertEqual((source['private'], source['external_project']), (1, 'maple'), source['id'])
            self.assertIsNone(source['origin_ceiling'])  # the owner can still share it later
            owner, other = (self.alice, self.bob) if source['owner'] == 'alice' else (self.bob, self.alice)
            self.assertTrue(self.visible(owner, source))
            self.assertFalse(self.visible(other, source))
        with self.store.open() as state:
            connections = [dict(r) for r in state.db.execute(
                "SELECT owner,visibility,namespace FROM backend_connections WHERE id LIKE 'backfill-%' ORDER BY owner")]
            bodies = [r[0] for r in state.db.execute('SELECT body FROM memories WHERE id=ANY(?::text[])',
                                                       ([x['id'] for x in sources],))]
        self.assertEqual(connections, [{'owner': 'alice', 'visibility': 'private', 'namespace': 'backfill'},
                                       {'owner': 'bob', 'visibility': 'private', 'namespace': 'backfill'}])
        self.assertTrue(any('Rotate the staging keys' in b for b in bodies))
        self.assertFalse(any('ghp_' + 'A' * 36 in b for b in bodies))  # redaction ran
        self.assertEqual(self.audit()[-1]['disposition'], 'applied')

    def test_rerun_is_idempotent(self):
        self.run_backfill(apply=True)
        before = len(self.sources())
        again = self.run_backfill(apply=True)
        self.assertEqual(again['written']['duplicate'], 5, again)
        self.assertEqual(len(self.sources()), before)

    def test_budget_stops_and_the_next_run_resumes(self):
        first = self.run_backfill(apply=True, max_events=2)
        self.assertEqual(first['stopped'], 'event_budget')
        self.assertEqual(len(self.sources()), 2)
        second = self.run_backfill(apply=True)
        self.assertEqual((second['written']['duplicate'], len(self.sources())), (2, 5))

    def test_writes_wait_out_the_rate_limit(self):
        from unittest.mock import patch
        from agenthub.write_limits import RateLimited
        calls, waits = [], []
        def limited(store, ctx, size):
            calls.append(ctx['actor'])
            if len(calls) == 1:
                raise RateLimited('principal', 7)
        with patch('agenthub.write_limits.check', side_effect=limited):
            report = self.run_backfill(apply=True, sleep=waits.append)
        self.assertEqual((waits, len(calls), len(self.sources())), ([7], 6, 5))
        self.assertNotIn('stopped', report)

    def test_backfilled_items_stay_the_authors_own(self):
        self.run_backfill(apply=True)
        source = next(s for s in self.sources() if s['owner'] == 'alice')
        self.assertTrue(self.visible(self.alice, source))
        self.assertFalse(self.visible(self.bob, source), 'no per-item sharing: it stays owner-private')

    def test_refresh_keeps_owner_access_and_lets_former_writers_expire(self):
        self.run_backfill(apply=True)
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE backend_connections SET permission_observed=0 WHERE id LIKE 'backfill-%'")
        source = next(s for s in self.sources() if s['owner'] == 'alice')
        self.assertFalse(self.visible(self.alice, source))  # a stale connection hides its items
        self.store.set_membership('acme', 'maple', 'bob', False)
        self.assertEqual(server_backfill.refresh_connections(self.store), {'refreshed': 1, 'left_to_expire': 1})
        self.assertTrue(self.visible(self.alice, source))

    def test_mapping_and_input_are_validated(self):
        bad = Path(self.tmp.name) / 'bad.json'
        for value in ({'principals': {}}, {'principals': {'a': 'alice'}, 'workspaces': {'w': 'maple'}, 'sharing': 'x'},
                      {'principals': {'a': 'not a principal'}, 'workspaces': {'w': 'maple'}}):
            bad.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                server_backfill.run(self.store, self.input, bad)
        with self.assertRaises(ValueError):
            server_backfill.run(self.store, Path(self.tmp.name) / 'missing', self.mapping)


class FormatTests(unittest.TestCase):
    def test_every_event_is_built_without_a_sharing_request(self):
        for role, extra in (('user', {}), ('assistant', {}), ('tool', {'tool': 'Read', 'paths': ['a.py']})):
            record = server_backfill._record(line(role=role, **extra).encode())
            value = server_backfill.event(record, 'maple', 'backfill-x', 'backfill:' + 'a' * 64)
            self.assertNotIn('sharing', value)
            self.assertEqual((value['source_type'], value['origin'], value['revision']),
                             ('conversation', 'server_backfill', '1'))

    def test_unknown_fields_and_roles_are_refused(self):
        for raw in (line(sharing='organization'), line(role='system'), line(paths='a.py'), line(text=None), '[]'):
            self.assertIsNone(server_backfill._record(raw.encode()))


if __name__ == '__main__':
    unittest.main()
