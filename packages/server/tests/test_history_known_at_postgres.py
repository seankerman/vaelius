"""Synthetic current/effective/known-at lifecycle tests on the canonical PostgreSQL store.

Run with an explicit disposable services manifest. Provider-free source test;
installed-package proof is a separate H9 gate.
"""
from datetime import datetime, timezone
from contextlib import nullcontext
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.enterprise_contract import VERSION
from agenthub.processing.temporal import record_assertion
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Denied
from test_cloud_postgres import PostgresFixture


SERVICES = os.environ.get('AGENTNETWORK_PG_SERVICES')


def when(day):
    return datetime(2026, 9, day, 12, tzinfo=timezone.utc).timestamp()


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL manifest required')
class KnownAtPostgresTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        # Source checkout is intentionally ahead of its frozen installed pin.
        pin_context = nullcontext() if os.environ.get('AGENTNETWORK_INSTALLED_TEST') == '1' else patch('agenthub.pipeline_pin.verify', return_value={})
        with pin_context:
            self.postgres_setup()
            self.store = CloudStore(Path(self.temp.name) / 'cloud', self.dsn, 'acme')
        self.store.retrieval_selection_policy = 'baseline'
        self.ctx = self.store.authenticate(self.tokens['alice'])

    def tearDown(self): self.postgres_teardown()

    def add(self, suffix, location, effective_day, recorded_day, prior=None):
        source = self.store.ingest(self.ctx, {'version': VERSION,
            'external_id': 'location-' + suffix, 'session': 'chat-' + suffix,
            'turn': '1', 'project': 'maple', 'kind': 'Stop',
            'body': f'Maple dataset is at {location} effective 2026-09-{effective_day:02d}; ongoing.',
            'visibility': 'team', 'occurred_at': f'2026-09-{effective_day:02d}T12:00:00Z'})['source_id']
        doc = self.store.accept_reviewed_note(self.ctx, source, 'Maple dataset location ' + suffix,
            f'The Maple dataset location is {location} effective 2026-09-{effective_day:02d}.')
        with self.store.open() as state, state.db:
            assertion = record_assertion(state.db, revision_id=doc['revision_id'],
                subject='Maple dataset', predicate='location', value=location, actor='alice',
                evidence_source_ids=[source],
                validity={'from': f'2026-09-{effective_day:02d}',
                          'to_status': 'ongoing', 'precision': 'day', 'timezone': 'UTC',
                          'basis': 'explicit_source'},
                recorded_at=when(recorded_day),
                change=({'relation': 'supersedes', 'assertion_id': prior, 'reviewed': True}
                        if prior else None))
        return assertion['assertion_id'], source

    def query(self, mode):
        return self.store.search(self.ctx, {'version': VERSION,
            'query': 'Where was the Maple dataset as of 2026-09-10?',
            'project': 'maple', 'as_of': '2026-09-10', 'time_mode': mode})

    def test_late_evidence_diverges_and_detail_rechecks_present_policy(self):
        old, source = self.add('old', '/tmp/old.csv', 1, 1)
        new, _ = self.add('new', '/tmp/new.csv', 5, 20, prior=old)
        known = self.query('known_at')
        effective = self.query('effective_at')
        self.assertEqual([r['id'] for r in known['results']], [old])
        self.assertEqual([r['id'] for r in effective['results']], [new])
        current = self.store.search(self.ctx, {'version': VERSION,
            'query': 'Where is the Maple dataset?', 'project': 'maple',
            'time_mode': 'current'})
        self.assertEqual([r['id'] for r in current['results']], [new])
        mismatch = self.store.search(self.ctx, {'version': VERSION,
            'query': 'Where was the Maple dataset as of 2026-09-09?',
            'project': 'maple', 'as_of': '2026-09-10', 'time_mode': 'known_at'})
        self.assertFalse(mismatch['answerable'])
        self.assertEqual(mismatch['coverage_gaps'], ['ambiguous_temporal_cutoff'])
        detail = self.store.detail(self.ctx, old, as_of='2026-09-10', time_mode='known_at')
        self.assertIn('/tmp/old.csv', detail['claim']['text'])
        bob = self.store.authenticate(self.tokens['bob'])
        self.store.set_membership('acme', 'maple', 'bob', False)
        self.assertFalse(self.store.search(bob, {'version': VERSION,
            'query': 'Where was the Maple dataset as of 2026-09-10?',
            'project': 'maple', 'as_of': '2026-09-10', 'time_mode': 'known_at'})['answerable'])
        with self.assertRaises(Denied):
            self.store.detail(bob, old, as_of='2026-09-10', time_mode='known_at')
        self.store.lifecycle(self.ctx, {'version': VERSION, 'target_id': source,
            'expected_revision': '1', 'idempotency_key': 'withdraw-old',
            'reason': 'synthetic owner withdrew old evidence', 'operation': 'withdraw'})
        self.assertFalse(self.query('known_at')['answerable'])


if __name__ == '__main__': unittest.main()
