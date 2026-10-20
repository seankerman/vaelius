"""Data lifecycle governance on the canonical PostgreSQL authority.

Synthetic content carries a unique marker. Verification scans every text, JSON
and binary column of every table plus the object store, so a store that a purge
forgets is found without enumerating it by hand. Vectors are float arrays and
are counted per dependent document instead.
"""
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agentclient.enterprise_contract import VERSION
from agenthub.enterprise import Denied
from agenthub.postgres import PostgresConnection
from test_cloud_postgres import PostgresFixture, SERVICES

DAY = 86400


class _Embedder:
    def embed_documents(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


def scan(db, marker):
    """Count rows containing marker in any content-capable column of any table."""
    hits = {}
    columns = db.execute("""SELECT table_name,column_name,data_type FROM information_schema.columns
        WHERE table_schema=current_schema() AND data_type IN ('text','jsonb','json','bytea','character varying')
        ORDER BY table_name,column_name""").fetchall()
    for column in columns:
        name, table = column['column_name'], column['table_name']
        if column['data_type'] == 'bytea':
            predicate = f'position(convert_to(?,\'UTF8\') in "{name}")>0'
        else:
            predicate = f'position(? in lower("{name}"::text))>0'
        count = db.execute(f'SELECT count(*) FROM "{table}" WHERE {predicate}', (marker,)).fetchone()[0]
        if count:
            hits[table + '.' + name] = count
    return hits


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class DataLifecycleTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        from agenthub.cloud_runtime import CloudStore
        from agenthub.conversation_segments import ConversationSegments
        from agenthub.document_ingest import DocumentStore
        from agenthub.source_objects import FileSourceObjects
        self.root = Path(self.temp.name)
        self.store = CloudStore(self.root / 'hub', self.dsn, 'acme')
        self.objects = FileSourceObjects(self.root / 'objects')
        self.store.conversation_segments = ConversationSegments(self.store, self.objects)
        self.store.source_objects = self.objects
        self.store.semantic_model_key = 'synthetic-lifecycle-model'
        self.documents = DocumentStore(self.store, self.objects)
        self.ctx = self.store.authenticate(self.tokens['alice'])
        self.bob = self.store.authenticate(self.tokens['bob'])
        self.holder = self.store.authenticate(self.store.enroll('acme', 'admin', 'admin-hold', ['legal_hold', 'audit']))
        for ctx, name in ((self.ctx, 'alice'), (self.bob, 'bob')):
            self.store.enroll_connection(ctx, 'conv-' + name, 'conv-' + name, 'maple', ['agent', 'conversation'], visibility='team')
            self.store.enroll_connection(ctx, 'docs-' + name, 'docs-' + name, 'maple', ['document'], visibility='team')
        from agenthub.cloud_maintenance import IndexFreshness
        self.index = IndexFreshness(self.store, model_key='synthetic-lifecycle-model')
        self.index.ensure_generation()

    def tearDown(self):
        self.postgres_teardown()

    # Synthetic content ---------------------------------------------------
    def seed(self, ctx, marker, tag='a'):
        name = ctx['actor']
        hook = self.store.ingest(ctx, {'version': VERSION, 'external_id': 'hook-' + tag, 'session': 's-' + tag,
            'turn': '1', 'project': 'maple', 'kind': 'Stop', 'occurred_at': 1, 'visibility': 'team',
            'body': f'The {marker} dataset is kept at /srv/synthetic/{marker}.csv for the team.'})['source_id']
        note = self.store.accept_reviewed_note(ctx, hook, f'{marker} dataset location',
            f'The {marker} dataset is kept at /srv/synthetic/{marker}.csv for reuse.')['document_id']
        event = normalize_capture({'hook_event_name': 'UserPromptSubmit', 'event_id': 'prompt-' + tag,
            'session_id': 'conversation-' + tag, 'turn_id': 'turn-' + tag, 'timestamp': '2026-09-01T00:00:00Z',
            'prompt': f'Remember the {marker} orchard plan for the planning review.', 'revision': '1'},
            'maple', 'conv-' + name)
        conversation = self.store.ingest_general(ctx, event)['source_id']
        from agenthub.source_index import SourceIndex
        SourceIndex(self.store).run(max_sources=20, max_seconds=20)
        document = self.documents.ingest(ctx, 'docs-' + name, 'guide-' + tag, '1', f'{marker}-guide.md',
            io.BytesIO(f'# {marker} guide\n\nThe {marker} widget ships in blue.\n'.encode()),
            title=f'{marker} guide')['source_id']
        self.embed()
        self.curation_rows(hook, note, marker, tag)
        return {'hook': hook, 'note': note, 'conversation': conversation, 'document': document}

    def curation_rows(self, source, document, marker, tag):
        """Stores only the optional curation pipeline writes, seeded directly."""
        now = time.time()
        with self.store.open() as state, state.db:
            db = state.db
            project = db.execute('SELECT internal_project FROM enterprise_sources WHERE id=?', (source,)).fetchone()[0]
            db.execute("INSERT INTO knowledge_generations VALUES(?,?,?,?,?,?,NULL,NULL)",
                       ('gen-' + tag, 'v1', 'retired', 'sources', 'hash', now))
            db.execute('''INSERT INTO curation_episode_jobs(id,generation_id,episode_id,project,session,turn,
                source_ids,source_hash,created,updated,version) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       ('job-' + tag, 'gen-' + tag, 'episode', project, 's-' + tag, '1', json.dumps([source]), 'hash', now, now, 'v1'))
            db.execute('INSERT INTO episode_occurrences VALUES(?,?,?,?,?)', ('occ-' + tag, project, 's-' + tag, '1', now))
            db.execute('''INSERT INTO episode_revisions VALUES(?,?,?,?,1,NULL,'hash',?,'learned',?,'initial_curation',?)''',
                       ('erv-' + tag, 'occ-' + tag, 'gen-' + tag, 'job-' + tag, json.dumps([source]),
                        json.dumps({'atoms': [{'text': f'{marker} episode summary'}]}), now))
            db.execute('INSERT INTO episode_revision_evidence VALUES(?,?,?,?)', ('erv-' + tag, 'atom', source, ''))
            db.execute("INSERT INTO memory_metadata VALUES(?,1,'unknown','observation','[]','[]',?,'{}','unknown','r','[]',?)",
                       (source, f'{marker} applicability', now))
            revision = db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?', (document,)).fetchone()[0]
            db.execute("INSERT INTO knowledge_candidate_keys VALUES(?,'subject',?,?,?) ON CONFLICT DO NOTHING",
                       (project, marker + '-subject', document, revision))

    def embed(self):
        self.index.reconcile()
        while self.index.run_once(_Embedder())['status'] != 'idle':
            pass

    def scan(self, marker):
        with self.store.open() as state:
            return scan(state.db, marker)

    def object_hits(self, marker):
        return sorted(p.name for p in self.objects.root.rglob('*') if p.is_file() and marker.encode() in p.read_bytes())

    def search(self, ctx, marker):
        return [r['id'] for r in self.store.search(ctx, {'version': VERSION, 'query': marker})['results']]

    def vectors(self, sources):
        with self.store.open() as state:
            return state.db.execute('''SELECT count(*) FROM cloud_document_vectors WHERE document_id IN
                (SELECT document_id FROM enterprise_dependencies WHERE source_id=ANY(?::text[]))''',
                (list(sources),)).fetchone()[0]

    def delete(self, ctx, source, key):
        with self.store.open() as state:
            revision = state.db.execute('SELECT source_version FROM enterprise_sources WHERE id=?', (source,)).fetchone()[0]
        return self.store.lifecycle(ctx, {'version': VERSION, 'target_id': source, 'expected_revision': str(revision),
            'operation': 'delete', 'idempotency_key': key, 'reason': 'synthetic owner deletion'})

    def mcp_search(self, token, marker):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        store = self.store
        registry = type('Registry', (), {'store_for_token': lambda _, value: store})()
        with TestClient(create_app(registry, allowed_hosts=['testserver'])) as client:
            response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'search_memory', 'arguments': {'query': marker}}},
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream',
                         'MCP-Protocol-Version': '2025-11-25', 'X-AgentNetwork-Project': 'maple'})
        self.assertEqual(response.status_code, 200, response.status_code)
        return response.text

    # Deletion verification ---------------------------------------------
    def test_owner_deletion_removes_every_store_original_and_vector(self):
        sources = self.seed(self.ctx, 'qzxalpha')
        self.assertTrue(self.search(self.ctx, 'qzxalpha'))
        self.assertTrue(self.object_hits('qzxalpha'))
        self.assertGreater(self.vectors(sources.values()), 0)
        self.assertIn('qzxalpha', self.mcp_search(self.tokens['alice'], 'qzxalpha'))
        for name in ('hook', 'conversation', 'document'):
            self.delete(self.ctx, sources[name], 'delete-' + name)
        self.assertEqual(self.scan('qzxalpha'), {})
        self.assertEqual(self.object_hits('qzxalpha'), [])
        self.assertEqual(self.vectors(sources.values()), 0)
        self.assertEqual(self.search(self.ctx, 'qzxalpha'), [])
        self.assertNotIn('qzxalpha', self.mcp_search(self.tokens['alice'], 'qzxalpha'))
        with self.store.open() as state:
            journal = state.db.execute("SELECT count(*) FROM enterprise_deletion_journal WHERE operation='delete'").fetchone()[0]
            pending = state.db.execute('SELECT count(*) FROM enterprise_object_purges WHERE purged IS NULL').fetchone()[0]
        self.assertEqual((journal, pending), (3, 0))

    def test_verification_detects_derivatives_when_extended_purge_is_disabled(self):
        sources = self.seed(self.ctx, 'qzxbravo')
        with patch('agenthub.data_lifecycle.purge_retained_derivatives', lambda *args: None):
            for name in ('hook', 'conversation', 'document'):
                self.delete(self.ctx, sources[name], 'unprotected-' + name)
        leaked = self.scan('qzxbravo')
        # The pre-existing purge left the original document, its metadata, an
        # episode summary, reconciliation keys and every vector behind.
        self.assertIn('backend_document_versions.title', leaked)
        self.assertIn('episode_revisions.payload_json', leaked)
        self.assertIn('knowledge_candidate_keys.key_value', leaked)
        self.assertTrue(self.object_hits('qzxbravo'))
        self.assertGreater(self.vectors(sources.values()), 0)

    # Subject erasure ------------------------------------------------------
    def test_subject_erasure_report_absence_isolation_and_no_resurrection(self):
        alice = self.seed(self.ctx, 'qzxcharlie', 'a')
        self.seed(self.bob, 'qzxdelta', 'b')
        preview = self.store.erase_subject('alice')
        self.assertTrue(preview['dry_run'])
        self.assertEqual(preview['candidates'], 3)
        self.assertGreater(preview['before']['source_text'], 0)
        self.assertGreater(preview['before']['vectors'], 0)
        self.assertTrue(self.scan('qzxcharlie'))
        report = self.store.erase_subject('alice', apply=True, max_sources=2)
        self.assertTrue(report['more'])
        self.assertFalse(report['verified'])
        report = self.store.erase_subject('alice', apply=True)
        self.assertTrue(report['verified'], report)
        self.assertEqual(report['object_check']['present'], 0)
        self.assertGreater(report['object_check']['checked'], 0)
        self.assertNotIn('qzxcharlie', json.dumps(report))
        self.assertEqual(self.scan('qzxcharlie'), {})
        self.assertEqual(self.object_hits('qzxcharlie'), [])
        self.assertEqual(self.vectors(alice.values()), 0)
        for ctx in (self.ctx, self.bob):
            self.assertEqual(self.search(ctx, 'qzxcharlie'), [])
        self.assertNotIn('qzxcharlie', self.mcp_search(self.tokens['bob'], 'qzxcharlie'))
        # Another principal's content and originals are untouched.
        self.assertTrue(self.search(self.bob, 'qzxdelta'))
        self.assertTrue(self.object_hits('qzxdelta'))
        # Re-running derivation and index repair must not resurrect content.
        from agenthub.source_index import SourceIndex
        SourceIndex(self.store).reconcile()
        SourceIndex(self.store).run(max_sources=20, max_seconds=20)
        self.store.refresh_documents()
        self.embed()
        self.assertEqual(self.scan('qzxcharlie'), {})
        self.assertEqual(self.vectors(alice.values()), 0)
        self.assertEqual(self.search(self.ctx, 'qzxcharlie'), [])
        again = self.store.erase_subject('alice', apply=True)
        self.assertEqual((again['candidates'], again['deleted']), (0, 0))
        self.assertTrue(again['verified'])
        stored = self.store.lifecycle_report(report['run_id'])
        self.assertEqual((stored['kind'], stored['status']), ('erasure', 'complete'))
        with self.store.open() as state:
            audits = {(r['operation'], r['disposition']) for r in state.db.execute(
                "SELECT operation,disposition FROM enterprise_audit WHERE actor='operator'")}
        self.assertIn(('subject_erasure', 'deleted'), audits)
        self.assertIn(('erasure_run', 'complete'), audits)
        with self.assertRaises(ValueError):
            self.store.erase_subject('nobody')

    # Legal holds ----------------------------------------------------------
    def test_legal_hold_preserves_but_hides_then_release_completes_erasure(self):
        sources = self.seed(self.ctx, 'qzxecho')
        for ctx in (self.bob, self.store.authenticate(self.tokens['admin']),
                    self.store.authenticate(self.store.enroll('acme', 'bob', 'bob-hold', ['legal_hold']))):
            with self.assertRaises(Denied):
                self.store.create_legal_hold(ctx, 'principal', 'alice', 'synthetic matter 1')
        with self.assertRaises(ValueError):
            self.store.create_legal_hold(self.holder, 'principal', 'alice', 'password=hunter2-synthetic-secret')
        hold = self.store.create_legal_hold(self.holder, 'principal', 'alice', 'synthetic matter 1')
        # Owner deletion of covered content is hidden and deferred, not purged.
        result = self.delete(self.ctx, sources['document'], 'held-delete')
        from agentclient.enterprise_contract import validate_response
        validate_response('/enterprise/v1/lifecycle', result)
        with self.store.open() as state:
            journal = sorted(r[0] for r in state.db.execute(
                'SELECT operation FROM enterprise_deletion_journal WHERE source_id=?', (sources['document'],)))
        self.assertEqual(journal, ['delete_held', 'withdraw'])
        with self.assertRaises(Denied):
            self.documents.describe(self.ctx, sources['document'])
        report = self.store.erase_subject('alice', apply=True)
        self.assertFalse(report['verified'])
        self.assertEqual(report['held'], 3)
        self.assertEqual(report['deleted'], 0)
        self.assertEqual(self.search(self.ctx, 'qzxecho'), [])
        self.assertTrue(self.object_hits('qzxecho'))
        self.assertIn('memories.body', self.scan('qzxecho'))
        self.assertEqual(self.store.lifecycle_report(report['run_id'])['status'], 'held')
        # Retention cannot expire held content either.
        self.store.set_retention_policies([{'project': '*', 'source_type': '*', 'max_age_days': 1}])
        retention = self.store.run_retention(apply=True, now=time.time() + 10 * DAY)
        self.assertEqual(retention['deleted'], 0)
        self.assertTrue(self.object_hits('qzxecho'))
        self.assertEqual([h['id'] for h in self.store.legal_holds(self.holder)], [hold['hold_id']])
        released = self.store.release_legal_hold(self.holder, hold['hold_id'], 'synthetic matter closed')
        self.assertEqual(released['disposition'], 'released')
        self.assertEqual(self.store.legal_holds(self.holder), [])
        report = self.store.erase_subject('alice', apply=True)
        self.assertTrue(report['verified'], report)
        self.assertEqual(self.scan('qzxecho'), {})
        self.assertEqual(self.object_hits('qzxecho'), [])
        with self.store.open() as state:
            audits = [(r['actor'], r['operation'], r['disposition']) for r in state.db.execute(
                "SELECT actor,operation,disposition FROM enterprise_audit WHERE operation IN ('legal_hold','delete') ORDER BY id")]
        self.assertEqual(audits, [('admin', 'legal_hold', 'created'), ('alice', 'delete', 'held'),
                                  ('admin', 'legal_hold', 'released')])

    def test_project_and_source_holds_scope_exactly(self):
        sources = self.seed(self.ctx, 'qzxfoxtrot')
        with self.assertRaises(Denied):
            self.store.create_legal_hold(self.holder, 'project', 'unknown-project', 'synthetic matter 2')
        with self.assertRaises(Denied):
            self.store.create_legal_hold(self.holder, 'tenant', 'other-tenant', 'synthetic matter 2')
        self.store.create_legal_hold(self.holder, 'source', sources['document'], 'synthetic matter 2')
        report = self.store.erase_subject('alice', apply=True)
        self.assertEqual((report['deleted'], report['held']), (2, 1))
        hits = self.scan('qzxfoxtrot')
        self.assertIn('backend_document_versions.title', hits)
        self.assertNotIn('events.payload', hits)

    # Retention --------------------------------------------------------------
    def age(self, sources, days):
        with self.store.open() as state, state.db:
            state.db.execute('UPDATE enterprise_sources SET created=created-? WHERE id=ANY(?::text[])',
                             (days * DAY, list(sources)))

    def hook(self, ctx, tag, marker):
        return self.store.ingest(ctx, {'version': VERSION, 'external_id': 'retained-' + tag, 'session': 'r', 'turn': tag,
            'project': 'maple', 'kind': 'Stop', 'occurred_at': 1, 'visibility': 'team',
            'body': f'Retention fixture {marker} entry {tag}.'})['source_id']

    def test_retention_policy_validation(self):
        for policies in ([{'project': 'unknown', 'max_age_days': 30}], [{'source_type': 'email', 'max_age_days': 30}],
                         [{'max_age_days': 0}], [{'max_age_days': 30, 'owner': 'alice'}],
                         [{'max_age_days': 30}, {'max_age_days': 40}]):
            with self.assertRaises(ValueError):
                self.store.set_retention_policies(policies)
        self.assertEqual(self.store.retention_policies(), [])
        self.assertEqual(self.store.run_retention(apply=True)['deleted'], 0)

    def test_scheduled_retention_dry_run_bounded_resumable_specific_policy_and_hold(self):
        expired = [self.hook(self.ctx, str(i), 'qzxgolf') for i in range(5)]
        recent = self.hook(self.ctx, 'recent', 'qzxhotel')
        held = self.hook(self.bob, 'held', 'qzxindia')
        document = self.documents.ingest(self.ctx, 'docs-alice', 'kept', '1', 'kept.md',
            io.BytesIO(b'# Kept\n\nqzxjuliet long-lived reference.\n'), title='Kept')['source_id']
        self.age(expired + [held, document], 40)
        self.store.create_legal_hold(self.holder, 'source', held, 'synthetic matter 3')
        self.store.set_retention_policies([{'project': '*', 'source_type': '*', 'max_age_days': 3650},
                                           {'project': 'maple', 'source_type': 'agent', 'max_age_days': 30}])
        preview = self.store.run_retention(max_sources=10)
        self.assertEqual((preview['eligible'], preview['held_skipped'], preview['dry_run']), (5, 1, True))
        self.assertTrue(self.scan('qzxgolf'))
        deleted = []
        for _ in range(4):
            report = self.store.run_retention(apply=True, max_sources=2)
            deleted.append(report['deleted'])
            self.assertTrue(report['verified'])
            if report['cycle_complete']:
                break
        self.assertEqual(sum(deleted), 5)
        self.assertEqual(deleted[:2], [2, 2])
        self.assertEqual(self.scan('qzxgolf'), {})
        self.assertTrue(self.scan('qzxhotel'))
        self.assertTrue(self.scan('qzxindia'))
        self.assertTrue(self.object_hits('qzxjuliet'))
        rescan = self.store.run_retention()
        # A completed cycle restarts from the oldest source, not a stale cursor.
        self.assertEqual((rescan['scanned'], rescan['eligible'], rescan['held_skipped']), (7, 0, 1))
        self.assertEqual(self.store.run_retention(apply=True)['deleted'], 0)
        # A deletion deferred by a hold completes once the hold is released.
        self.delete(self.bob, held, 'held-retention-delete')
        self.assertTrue(self.scan('qzxindia'))
        hold = self.store.legal_holds(self.holder)[0]['id']
        self.store.release_legal_hold(self.holder, hold, 'synthetic matter closed')
        report = self.store.run_retention(apply=True)
        self.assertEqual(report['deferred_eligible'], 1)
        self.assertEqual(self.scan('qzxindia'), {})
        self.assertTrue(self.scan('qzxhotel'))

    def test_owner_retention_skips_held_sources(self):
        first = self.hook(self.ctx, 'owner-1', 'qzxkilo')
        second = self.hook(self.ctx, 'owner-2', 'qzxlima')
        self.store.create_legal_hold(self.holder, 'source', second, 'synthetic matter 4')
        result = self.store.retention(self.ctx, 'maple', time.time(), apply=True)
        self.assertEqual((result['applied'], result['held']), (1, 1))
        self.assertEqual(self.scan('qzxkilo'), {})
        self.assertIn('memories.body', self.scan('qzxlima'))
        with self.store.open() as state:
            active = {r['id']: r['active'] for r in state.db.execute('SELECT id,active FROM enterprise_sources')}
        self.assertEqual((active[first], active[second]), (0, 1))

    # Recovery ---------------------------------------------------------------
    def test_restored_snapshot_replays_deletion_journal(self):
        import os
        from agenthub.cloud_recovery import backup, replay_deletions, restore_snapshot
        from agenthub.cloud_runtime import CloudStore
        from agenthub.source_objects import FileSourceObjects
        sources = self.seed(self.ctx, 'qzxmike')
        snapshot = self.root / 'snapshot'
        backup(self.store, self.objects, snapshot)
        self.store.erase_subject('alice', apply=True)
        journal = self.store.deletion_journal()
        self.assertTrue(journal['entries'])
        self.assertNotIn('qzxmike', json.dumps(journal))
        target = PostgresFixture()
        target.postgres_setup(bootstrap=False)
        try:
            restored = CloudStore(self.root / 'restored', target.dsn, 'acme')
            objects = FileSourceObjects(self.root / 'restored-objects')
            restore_snapshot(snapshot, restored, objects, admin_dsn=target.admin_dsn)
            with restored.open() as state:
                self.assertIn('memories.body', scan(state.db, 'qzxmike'))
            exported = self.root / 'journal.json'
            exported.write_text(json.dumps(journal))
            os.chmod(exported, 0o600)
            preview = replay_deletions(exported, restored, objects)
            self.assertEqual((preview['pending'], preview['ready']), (3, False))
            result = replay_deletions(exported, restored, objects, apply=True)
            self.assertEqual(result['deleted'], 3)
            with restored.open() as state:
                self.assertEqual(scan(state.db, 'qzxmike'), {})
                remaining = state.db.execute('SELECT count(*) FROM cloud_document_vectors').fetchone()[0]
                ready = state.db.execute('SELECT ready FROM cloud_recovery_state WHERE singleton=1').fetchone()[0]
            self.assertEqual(remaining, 0)
            self.assertEqual(ready, 0)
            self.assertEqual([p for p in objects.root.rglob('*') if p.is_file() and b'qzxmike' in p.read_bytes()], [])
            again = replay_deletions(exported, restored, objects, apply=True)
            self.assertEqual((again['deleted'], again['already']), (0, 3))
        finally:
            target.postgres_teardown()
        del sources

    # Bounded SQL ------------------------------------------------------------
    def test_hold_check_and_inventory_use_constant_statements(self):
        from agenthub.data_lifecycle import inventory
        sources = [self.hook(self.ctx, str(i), 'qzxnovember') for i in range(6)]
        counts = []
        for selected in (sources[:1], sources):
            statements = []
            execute = PostgresConnection.execute

            def counted(db, query, params=None):
                statements.append(query)
                return execute(db, query, params)
            with patch.object(PostgresConnection, 'execute', counted):
                with self.store.open() as state:
                    inventory(state.db, 'acme', source_ids=selected)
            counts.append(len(statements))
        self.assertEqual(counts[0], counts[1])
        lifecycle = []
        for index, holds in enumerate((0, 3)):
            for number in range(holds):
                self.store.create_legal_hold(self.holder, 'source', sources[-1 - number], f'synthetic matter {number}')
            statements = []
            execute = PostgresConnection.execute

            def counted(db, query, params=None):
                statements.append(query)
                return execute(db, query, params)
            with patch.object(PostgresConnection, 'execute', counted):
                self.delete(self.ctx, sources[index], 'count-' + str(index))
            lifecycle.append(len(statements))
        self.assertEqual(lifecycle[0], lifecycle[1])

    def test_operator_commands_validate_inputs(self):
        from agenthub.cloud_maintenance import lifecycle_command
        with self.assertRaises(ValueError):
            lifecycle_command(self.store, 'legal-hold', {'action': 'list'}, self.objects)
        with self.assertRaises(ValueError):
            lifecycle_command(self.store, 'subject-erase', {}, self.objects)
        with self.assertRaises(ValueError):
            lifecycle_command(self.store, 'retention-run', {'apply': True}, self.objects)
        with self.assertRaises(ValueError):
            lifecycle_command(self.store, 'deletion-journal', {}, self.objects)
        policies = lifecycle_command(self.store, 'retention-policy', {'policies': [{'max_age_days': 400}]}, self.objects)
        self.assertEqual(policies['policies'][0]['max_age_days'], 400)
        output = self.root / 'journal' / 'page.json'
        result = lifecycle_command(self.store, 'deletion-journal', {}, self.objects, output=output)
        self.assertEqual(result['entries'], 0)
        self.assertEqual(output.stat().st_mode & 0o077, 0)


@unittest.skipUnless(SERVICES, 'explicit local PostgreSQL services manifest required')
class OperatorLifecycleTests(unittest.TestCase):
    def test_cli_holds_require_token_and_block_tenant_retirement_until_released(self):
        import os
        import tempfile
        import uuid
        from psycopg import sql
        from agenthub.cloud_maintenance import execute, private_input, retire
        from agenthub.cloud_profile import setup_profile
        from agenthub.cloud_runtime import registry_from_settings
        from agenthub.postgres import connect
        from agenthub.source_objects import FileSourceObjects
        services = json.loads(Path(SERVICES).read_text())
        namespace = 'test_lifecycle_' + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'profile'
            setup_profile(profile, SERVICES, namespace=namespace)
            try:
                registry = registry_from_settings(private_input(profile / 'runtime.json'), profile / 'runtime-state')
                store = registry.resolve('acme')
                token = Path(temp) / 'holds.token'
                token.write_text(store.enroll('acme', 'settings-admin', 'holds-admin', ['legal_hold']))
                os.chmod(token, 0o600)
                value = Path(temp) / 'hold.json'

                def run(command, payload, **kwargs):
                    if value.exists():
                        value.unlink()
                    value.write_text(json.dumps(payload))
                    os.chmod(value, 0o600)
                    return execute(profile, 'acme', command, value=private_input(value), **kwargs)['result']
                with self.assertRaises(ValueError):
                    run('legal-hold', {'action': 'list'})
                created = run('legal-hold', {'action': 'create', 'scope': 'tenant', 'target': 'acme',
                                             'reason': 'synthetic matter'}, token_file=token)
                objects = FileSourceObjects(Path(temp) / 'objects')
                with self.assertRaisesRegex(ValueError, 'tenant_retirement_blocked_by_legal_hold'):
                    retire(profile, 'acme', expected_database=namespace + '_acme', objects=objects, apply=True)
                registry.require_active('acme')
                preview = run('subject-erase', {'principal': 'alice'})
                self.assertTrue(preview['dry_run'])
                policies = run('retention-policy', {'policies': [{'max_age_days': 365}]})
                self.assertEqual(policies['policies'][0]['max_age_days'], 365)
                run('legal-hold', {'action': 'release', 'hold_id': created['hold_id'], 'reason': 'synthetic closed'},
                    token_file=token)
                self.assertEqual(retire(profile, 'acme', expected_database=namespace + '_acme',
                                        objects=objects)['status'], 'preview')
                receipts = [json.loads(p.read_text()) for p in (profile / 'maintenance-receipts').glob('*.json')]
                self.assertEqual(sorted(r['command'] for r in receipts),
                                 ['legal-hold', 'legal-hold', 'retention-policy', 'subject-erase'])
            finally:
                with connect(services['admin_dsn'], autocommit=True) as db:
                    for name in (namespace + '_acme', namespace + '_bravo', namespace + '_control'):
                        db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
                    for role in (namespace + '_acme_app', namespace + '_bravo_app', namespace + '_router'):
                        db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


if __name__ == '__main__':
    unittest.main()
