"""Path and evidence guards for the seal-blind PostgreSQL DEV runner."""

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


RUNNER = Path(__file__).parents[1] / "tools/run_history_dev_postgres.py"
sys.path.insert(0, str(RUNNER.parent))
SPEC = importlib.util.spec_from_file_location("run_history_dev_postgres", RUNNER)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class HistoryDevPostgresRunnerTests(unittest.TestCase):
    def test_frozen_projection_controls_reject_tampering_and_keep_roles_distinct(self):
        bank, development_sha = runner.load_public_bank()
        controls = runner.load_projection_controls(bank, development_sha)
        self.assertEqual(len(controls), 48)
        self.assertEqual(controls['dev-replay_repeat-e2']['episode_kind'], 'transport_receipt')
        self.assertEqual(controls['dev-replay_repeat-e2']['claim_kind'], 'none')
        self.assertEqual(controls['dev-proposal_attempt-e1']['claim_kind'], 'activity_only')
        self.assertEqual(controls['dev-quoted_lineage-e2']['claim_kind'], 'quotation_only')
        self.assertEqual(controls['dev-corrected_report-e1']['claim_kind'], 'reported_unverified')
        self.assertEqual(controls['dev-corrected_report-e2']['temporal']['change'],
                         {'relation': 'corrects', 'prior_event_id': 'dev-corrected_report-e1', 'reviewed': True})
        self.assertEqual(controls['dev-conflicts-e2']['temporal']['change']['relation'], 'conflicts')
        self.assertEqual(controls['dev-late_import-e2']['temporal']['time_basis'],
                         'authored_record_not_source_proven')
        amended = json.loads(json.dumps(bank))
        amended['fixed_curated_records'][0]['source_refs'][0]['quote'] += ' changed'
        with self.assertRaisesRegex(ValueError, 'projection_quote_mismatch'):
            runner.load_projection_controls(amended, development_sha)

    def test_projection_decisions_do_not_promote_activity_or_quotes_to_reusable_claims(self):
        bank, sha = runner.load_public_bank()
        controls = runner.load_projection_controls(bank, sha)
        selected = {row['event_id']: row for row in bank['fixed_curated_records']}
        for ident in ('dev-replay_repeat-e2', 'dev-proposal_attempt-e1',
                      'dev-proposal_attempt-e2', 'dev-quoted_lineage-e2',
                      'dev-concurrency_lifecycle-e4'):
            decision = runner.projection_decision(selected[ident], controls[ident])
            self.assertFalse(decision['reviewed_note'])
        self.assertFalse(runner.projection_decision(selected['dev-replay_repeat-e2'],
                         controls['dev-replay_repeat-e2'])['episode'])
        self.assertTrue(runner.projection_decision(selected['dev-proposal_attempt-e2'],
                        controls['dev-proposal_attempt-e2'])['episode'])
        self.assertTrue(runner.projection_decision(selected['dev-quoted_lineage-e2'],
                        controls['dev-quoted_lineage-e2'])['episode'])

    def test_fixed_records_mirror_oracle_fields_without_query_answers(self):
        bank, _ = runner.load_public_bank()
        expected = {event["id"]: event for event in bank["oracle_events"]}
        fixed = bank["fixed_curated_records"]
        self.assertEqual(len(fixed), 48)
        for record in fixed:
            oracle = expected[record["event_id"]]
            for key in ("actor_id", "project_id", "effective_at", "recorded_at",
                        "source_refs", "status", "time_precision", "truth", "reason"):
                self.assertEqual(record[key], oracle[key])
        self.assertNotIn("expected", fixed[0])

    def test_canonical_coverage_gap_is_preserved_without_oracle_reason(self):
        class EmptySearch:
            def search(self, _ctx, _value):
                return {"answerable": False, "results": [],
                        "coverage_gaps": ["no_authorized_supported_answer"]}

        request = {"id": "toy-q", "reader_id": "ada", "project_id": "toy",
                   "mode": "history", "query": "Any supported lesson?", "as_of": None}
        observed = runner._query(EmptySearch(), {"ada": {}}, None, request,
                                 {}, {}, {}, {}, {})
        self.assertFalse(observed["answerable"])
        self.assertEqual(observed["coverage_gaps"], ["no_authorized_supported_answer"])
        self.assertEqual(observed["abstention_reason"], "no_authorized_supported_answer")

    def test_oak_pagination_calls_canonical_history_with_frozen_byte_bound(self):
        limits = []
        def overview(_store, _ctx, _project, **kwargs):
            limits.append(kwargs["max_bytes"])
            return {"episodes": [], "coverage_gaps": [], "has_more": False,
                    "next_cursor": None}
        request = {"id": "dev-long_pages-q1", "reader_id": "ada",
                   "project_id": "oak-design", "mode": "history",
                   "query": "Show the full authorized Oak history", "as_of": None}
        with patch.object(runner, "project_overview", side_effect=overview):
            observed = runner._query(None, {"ada": {}}, None, request,
                                     {}, {}, {}, {}, {})
        self.assertEqual(limits, [320])
        self.assertEqual(observed["pages"][0]["serialized_bytes"],
                         len(b'{"episodes":[],"coverage_gaps":[],"has_more":false,"next_cursor":null}'))

    def test_receipt_and_services_must_be_private_h_owned_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            runner, "PRIVATE_ROOT", Path(directory).resolve()
        ):
            root = Path(directory).resolve()
            services = root / "pg-h3-v1/services.json"
            services.parent.mkdir()
            services.write_text("{}")
            services.chmod(0o600)
            output = root / "receipts/projection.json"
            self.assertEqual(runner._safe_paths(services, output), (services, output))
            self.assertTrue(output.parent.is_dir())
            with self.assertRaisesRegex(ValueError, "new_h_private_receipt_required"):
                runner._safe_paths(services, root / "other/projection.json")
            output.touch()
            with self.assertRaisesRegex(ValueError, "new_h_private_receipt_required"):
                runner._safe_paths(services, output)
            services.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "h_owned_private_services_required"):
                runner._safe_paths(services, root / "receipts/next.json")

    def test_ref_normalization_preserves_exact_span_without_inventing_fields(self):
        refs = [{"source_id": "actual-id", "version": "v2", "start": 3,
                 "end": 8, "quote": "hello", "private_extra": "drop"}]
        self.assertEqual(runner._normalize_refs(refs, {"actual-id": "fixture-id"}),
                         [{"source_id": "fixture-id", "version": "v2",
                           "start": 3, "end": 8, "quote": "hello"}])

    def test_original_byte_proof_yields_exact_citation_without_oracle_answer(self):
        raw = b'Heading: the narrow bracket is here.'
        class Original:
            def list(self, *_args, **_kwargs):
                return [{'source_id': 'actual', 'version': '1'}]
            def fetch(self, *_args, **_kwargs):
                return io.BytesIO(raw), {'X-Source-SHA256': __import__('hashlib').sha256(raw).hexdigest()}
        request = {'id': 'toy-original', 'reader_id': 'ada', 'project_id': 'oak-design',
                   'mode': 'original', 'query': 'Show original', 'as_of': '1'}
        original = {'actual': {'event_id': 'document-v1', 'ref': {'source_id': 'fixture',
            'version': '1', 'start': 13, 'end': 27, 'quote': 'narrow bracket'}}}
        result = runner._query(None, {'ada': {}}, Original(), request,
                               {}, {}, {}, {'actual': 'fixture'}, original)
        self.assertTrue(result['answerable'])
        self.assertEqual(result['event_ids'], ['document-v1'])
        self.assertEqual(result['evidence_refs'][0]['quote'], 'narrow bracket')
        original['actual']['ref']['quote'] = 'invented quote'
        result = runner._query(None, {'ada': {}}, Original(), request,
                               {}, {}, {}, {'actual': 'fixture'}, original)
        self.assertFalse(result['answerable'])
        self.assertEqual(result['coverage_gaps'], ['original_exact_span_unverified'])


@unittest.skipUnless(os.environ.get('AGENTNETWORK_PG_SERVICES'), 'H-owned PostgreSQL fixture required')
class FixedProjectionPostgresTests(unittest.TestCase):
    def test_all_fixed_records_project_without_scored_questions(self):
        from agenthub.cloud_runtime import CloudStore
        from agenthub.document_ingest import DocumentStore
        from agenthub.source_objects import FileSourceObjects
        fixture = runner.PostgresFixture()
        with patch('agenthub.pipeline_pin.verify', return_value={'source_only_pin_patch': True}):
            fixture.postgres_setup(tenant_id='synthetic-lab', bootstrap=False)
            try:
                store = CloudStore(Path(fixture.temp.name) / 'fixed-all', fixture.dsn,
                                   'synthetic-lab')
                store.create_organization('synthetic-lab')
                for actor in ('ada', 'ben', 'alex-42'):
                    store.create_principal('synthetic-lab', actor)
                bank, sha = runner.load_public_bank()
                controls = runner.load_projection_controls(bank, sha)
                source_names = {ref['source_id'] for row in bank['fixed_curated_records']
                                for ref in row['source_refs']}
                sources = [row for row in bank['sources'] if row['id'] in source_names]
                for project in {row['project_id'] for row in sources}:
                    store.create_project('synthetic-lab', project)
                    for actor in ('ada', 'ben', 'alex-42'):
                        store.set_membership('synthetic-lab', project, actor, True)
                ctx = {}
                for actor in ('ada', 'ben', 'alex-42'):
                    token = store.enroll('synthetic-lab', actor, 'all-' + actor,
                        ['ingest', 'read', 'source_read', 'correct', 'policy'])
                    ctx[actor] = store.authenticate(token)
                connections = {}
                for source in sources:
                    if source['kind'] == 'native_document':
                        project = source['project_id']
                        if project not in connections:
                            connection = 'all-doc-' + project
                            store.enroll_connection(ctx['ada'], connection, 'synthetic', project,
                                ['document'], visibility='team', reader_ids=['ada','ben'])
                            connections[project] = connection
                documents = DocumentStore(store, FileSourceObjects(Path(fixture.temp.name) / 'objects'))
                actual = {row['id']: runner._source_record(store, ctx, documents, connections, row)
                          for row in sources}
                small = {'sources': sources, 'fixed_curated_records': bank['fixed_curated_records']}
                _, notes, occurrences, _, counts = runner._fixed_projection(
                    store, ctx, small, actual, controls)
                self.assertEqual(counts['transport_receipts'], 1)
                self.assertEqual(counts['historical_occurrences'], 47)
                self.assertEqual(counts['temporal_assertions'], 20)
                self.assertEqual(counts['reviewed_relations'], 7)
                self.assertNotIn('dev-replay_repeat-e2', occurrences.values())
                self.assertNotIn('dev-quoted_lineage-e2', notes.values())
                self.assertNotIn('dev-concurrency_lifecycle-e4', notes.values())
                with store.open() as state:
                    relation_count = state.db.execute(
                        'SELECT count(*) FROM knowledge_temporal_relations').fetchone()[0]
                    self.assertEqual(relation_count, 7)
                    late = state.db.execute("""SELECT valid_from,valid_basis,recorded_at
                        FROM knowledge_temporal_assertions
                        WHERE subject='Birch queue' AND value_json='"30-item"'
                        ORDER BY recorded_at""").fetchall()
                    self.assertEqual(len(late), 2)
                    self.assertEqual(late[0]['valid_from'], '2026-05-10')
                    self.assertEqual(late[0]['valid_basis'], 'inferred')
                    imported = next(row for row in bank['fixed_curated_records']
                                    if row['event_id'] == 'dev-late_import-e2')
                    from datetime import datetime
                    self.assertEqual(late[0]['recorded_at'],
                        datetime.fromisoformat(imported['recorded_at'].replace('Z','+00:00')).timestamp())
                    self.assertGreater(late[0]['recorded_at'],
                        datetime.fromisoformat('2026-05-10T00:00:00+00:00').timestamp())
            finally:
                fixture.postgres_teardown()

    def test_toy_subset_projects_roles_temporal_relations_and_denial(self):
        from agenthub.cloud_runtime import CloudStore
        fixture = runner.PostgresFixture()
        with patch('agenthub.pipeline_pin.verify', return_value={'source_only_pin_patch': True}):
            fixture.postgres_setup(tenant_id='synthetic-lab', bootstrap=False)
            try:
                store = CloudStore(Path(fixture.temp.name) / 'fixed-projector', fixture.dsn,
                                   'synthetic-lab')
                store.create_organization('synthetic-lab')
                for actor in ('ada', 'ben'):
                    store.create_principal('synthetic-lab', actor)
                bank, sha = runner.load_public_bank()
                controls = runner.load_projection_controls(bank, sha)
                selected = {'dev-reversion-e1', 'dev-reversion-e2',
                    'dev-corrected_report-e1', 'dev-corrected_report-e2',
                    'dev-conflicts-e1', 'dev-conflicts-e2',
                    'dev-replay_repeat-e1', 'dev-replay_repeat-e2',
                    'dev-quoted_lineage-e2',
                    'dev-proposal_attempt-e1', 'dev-proposal_attempt-e2'}
                records = [row for row in bank['fixed_curated_records']
                           if row['event_id'] in selected]
                source_names = {ref['source_id'] for row in records for ref in row['source_refs']}
                sources = [row for row in bank['sources'] if row['id'] in source_names]
                for project in {row['project_id'] for row in sources}:
                    store.create_project('synthetic-lab', project)
                    for actor in ('ada', 'ben'):
                        store.set_membership('synthetic-lab', project, actor, True)
                ctx = {}
                for actor in ('ada', 'ben'):
                    token = store.enroll('synthetic-lab', actor, 'toy-' + actor,
                        ['ingest', 'read', 'source_read', 'correct', 'policy'])
                    ctx[actor] = store.authenticate(token)
                actual = {row['id']: runner._source_record(store, ctx, None, {}, row)
                          for row in sources}
                small = {'sources': sources, 'fixed_curated_records': records}
                _, notes, occurrences, _, counts = runner._fixed_projection(
                    store, ctx, small, actual, controls)
                self.assertEqual(counts['transport_receipts'], 1)
                self.assertEqual(counts['historical_occurrences'], len(records) - 1)
                self.assertEqual(counts['reviewed_notes'], 6)
                self.assertEqual(counts['temporal_assertions'], 6)
                self.assertEqual(counts['reviewed_relations'], 3)
                self.assertNotIn('dev-replay_repeat-e2', occurrences.values())
                self.assertIn('dev-proposal_attempt-e2', occurrences.values())
                self.assertIn('dev-quoted_lineage-e2', occurrences.values())
                self.assertNotIn('dev-proposal_attempt-e2', notes.values())
                self.assertNotIn('dev-quoted_lineage-e2', notes.values())
                with store.open() as state:
                    relations = state.db.execute(
                        'SELECT relation,reviewed FROM knowledge_temporal_relations').fetchall()
                    self.assertEqual(sorted(row['relation'] for row in relations),
                                     ['conflicts', 'corrects', 'supersedes'])
                    self.assertTrue(all(row['reviewed'] for row in relations))
                    inferred = state.db.execute(
                        'SELECT DISTINCT valid_basis FROM knowledge_temporal_assertions').fetchall()
                    self.assertEqual([row[0] for row in inferred], ['inferred'])
                store.set_membership('synthetic-lab', 'cedar-routing', 'ben', False)
                request = {'id': 'toy-denial', 'reader_id': 'ben', 'project_id': 'cedar-routing',
                           'mode': 'history', 'query': 'What happened?', 'as_of': None}
                denied = runner._query(store, ctx, None, request, {}, {}, {}, {}, {})
                self.assertFalse(denied['answerable'])
                self.assertEqual(denied['abstention_reason'], 'current_policy_denial')
                self.assertEqual(denied['event_ids'], [])
            finally:
                fixture.postgres_teardown()


if __name__ == "__main__":
    unittest.main()
