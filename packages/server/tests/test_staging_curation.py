"""Frozen synthetic replay: contextual dependencies, idle work and recovery.

Scripted curator responses prove validation/lifecycle behavior only. They are not
measurements of extraction quality, natural-session usefulness or cache hits.
"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agentclient.enterprise_contract import VERSION
from agenthub.processing.harness import HarnessError
from agenthub.backend_worker import Worker
from vaelius_test_support.fixtures.enterprise import EnterpriseStore
import test_backend_worker as base

FIXTURE = Path(__file__).parents[1] / "tools/fixtures/local_staging_readiness_v1/curation_sequences.json"
FIXTURE_HASH = "1c38ae08ba1e54f59a4eec12f5aef9b3c44597a07421340438df7d581d23beac"
HANDLE = "00000000-0000-4000-8000-000000000001"


class StagingCurationTests(unittest.TestCase):
    tearDown = base.WorkerTests.tearDown

    def setUp(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_HASH)
        self.fixture = json.loads(FIXTURE.read_text())
        self.cases = {case["id"]: case for case in self.fixture["cases"]}
        self.sources = {}
        base.WorkerTests.setUp(self)
        self.calls = []

    def turn(self, turn):
        # The base setup's initial completed turn is the frozen routine case.
        self.ingest_case("routine")

    def ingest_case(self, name):
        case = self.cases[name]
        for event in case["events"]:
            payload = dict(event, session_id=case["session"], turn_id=case["turn"])
            result = self.store.ingest_general(self.ctx, normalize_capture(payload, "maple", "agent"))
            self.sources[event["event_id"]] = result["source_id"]

    def no_learning(self, home, config, instruction, payload, schema, **kwargs):
        self.calls.append({"purpose": config["_purpose"], "payload": payload,
                           "session": kwargs.get("session_id")})
        return {"records": [], "episode_summary": {"intent": None, "open_work": []}}, {
            "input_tokens": 10, "cached_input_tokens": 7}, HANDLE

    def run_routine(self):
        result = Worker(self.store, self.config, runner=self.no_learning, live=False).run(max_seconds=10)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["calls"], 1)

    def location_record(self, payload):
        event = next(e for e in payload["episode"]["events"] if e.get("tool_name") == "save_file")
        return {"title": "Slate dataset saved", "text": "Saved Slate dataset at /Users/demo/My Data/slate-reviewed.csv.",
                "subject": "Slate dataset", "facets": ["activity"], "actors": ["agent"],
                "artifact": {"name": "slate-reviewed.csv", "location": "/Users/demo/My Data/slate-reviewed.csv"},
                "rationale": None, "state": "observed", "occurred_date": "",
                "event_id": event["event_id"], "evidence_span_ids": [event["spans"][0]["span_id"]]}

    def withdraw(self, ident):
        return self.store.lifecycle(self.ctx, {"version": VERSION, "target_id": ident,
            "expected_revision": "1", "operation": "withdraw",
            "idempotency_key": "staging-withdraw-" + ident,
            "reason": "synthetic influencing-source revocation"})

    def test_frozen_labels_are_source_grounded_and_capture_is_model_free(self):
        self.assertEqual(len(self.cases), 6)
        for case in self.cases.values():
            by_id = {event["event_id"]: event for event in case["events"]}
            for fact in case["expected"]["durable"]:
                event = by_id[fact["event_id"]]
                self.assertIn(fact["quote"], json.dumps(event, ensure_ascii=False))
            for event in case["events"]:
                with patch("agenthub.processing.harness.run_structured", side_effect=AssertionError("client model call")), \
                     patch("agenthub.processing.harness.run_structured_session", side_effect=AssertionError("client model call")):
                    normalized = normalize_capture(dict(event, session_id=case["session"],
                        turn_id=case["turn"]), "maple", "agent")
                self.assertEqual(normalized["conversation"], case["session"])

    def test_no_learning_idle_duplicate_resume_and_reported_cache_receipts(self):
        self.run_routine()
        self.ingest_case("routine")  # Exact transport replay cannot enqueue work.
        restarted = Worker(EnterpriseStore(self.store.home), self.config, runner=self.no_learning, live=False)
        self.assertEqual(restarted.run(max_seconds=10)["calls"], 0)
        self.ingest_case("decision")
        self.assertEqual(restarted.run(max_seconds=10)["completed"], 1)
        self.assertEqual([call["session"] for call in self.calls], [None, HANDLE])
        current = self.calls[-1]["payload"]
        self.assertEqual(len(current["episode"]["events"]), 2)
        self.assertTrue(current["previous_evidence_index"])
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT count(*) FROM knowledge_documents").fetchone()[0], 0)
            self.assertEqual(state.db.execute("SELECT count(*) FROM episode_candidates").fetchone()[0], 0)
            purposes = [row[0] for row in state.db.execute("SELECT purpose FROM backend_worker_receipts WHERE attempt_id IS NOT NULL")]
            self.assertEqual(purposes, ["durable_memory_curate", "durable_memory_curate"])
            usages = [json.loads(row[0]) for row in state.db.execute("SELECT usage FROM backend_worker_receipts WHERE status='returned'")]
            self.assertEqual([usage["cached_input_tokens"] for usage in usages], [7, 7])
        self.assertEqual(restarted.status()["model_calls"], 0)  # Scripted invocations, not live calls.

    def test_uncited_prior_context_revoked_before_dispatch_blocks_new_call(self):
        self.run_routine()
        self.ingest_case("location")
        worker = Worker(self.store, self.config, runner=self.no_learning, live=False)
        original_context = worker._context
        def context_then_revoke(state, job, **kwargs):
            result = original_context(state, job)
            self.withdraw(self.sources["routine-u"])
            return result
        with patch.object(worker, "_context", side_effect=context_then_revoke):
            result = worker.run(max_seconds=10)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["calls"], 0)
        self.assertEqual(len(self.calls), 1)
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT count(*) FROM knowledge_documents").fetchone()[0], 0)
            self.assertIsNone(state.db.execute("SELECT provider_session FROM backend_observers").fetchone()[0])

    def test_narrow_current_citation_cannot_hide_revoked_prior_influence_at_commit(self):
        self.run_routine()
        self.ingest_case("location")
        def runner(home, config, instruction, payload, schema, **kwargs):
            self.assertTrue(payload["previous_evidence_index"])
            record = self.location_record(payload)  # Only cites the current save.
            self.withdraw(self.sources["routine-u"])
            return {"records": [record]}, {}, HANDLE
        result = Worker(self.store, self.config, runner=runner, live=False).run(max_seconds=10)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["completed"], 0)
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT count(*) FROM episode_candidates").fetchone()[0], 0)
            self.assertEqual(state.db.execute("SELECT count(*) FROM knowledge_documents").fetchone()[0], 0)
            self.assertEqual(state.db.execute("SELECT count(*) FROM backend_provider_returns").fetchone()[0], 0)
            observer = state.db.execute("SELECT provider_session,pending_session FROM backend_observers").fetchone()
            self.assertEqual(list(observer.values()), [None, None])

    def test_incomplete_turn_inventory_is_held_without_dispatch(self):
        self.run_routine()
        self.ingest_case("incomplete-handoff")
        worker = Worker(self.store, self.config, runner=self.no_learning, live=False)
        result = worker.run(max_seconds=10)
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(worker.status()["held_episodes"], {"incomplete_capture": 1})

    def test_frozen_decision_location_correction_install_index_deliver_and_invalidate(self):
        from agenthub.processing.episode_pipeline import activate_generation
        resolver_calls = []
        def runner(home, config, instruction, payload, schema, **kwargs):
            if config["_purpose"] == "episode_resolve":
                resolver_calls.append(payload)
                moved = "Moved Slate" in payload["candidate"]["claim"]
                target = next((artifact["artifact_id"] for artifact in payload["active_artifacts"]
                               if "Slate" in json.dumps(artifact["claim"])), "")
                return {"candidate_key": payload["candidate"]["candidate_key"],
                    "operation": "CORRECT" if moved and target else "CREATE",
                    "target_artifact_id": target if moved else "",
                    "reason": "same_subject_newer_correction" if moved and target else "new_claim"}, {}
            events = payload["episode"]["events"]
            user = next(event for event in events if event["kind"] == "UserPromptSubmit")
            user_text = " ".join(span["text"] for span in user["spans"])
            if "process status" in user_text:
                return {"records": []}, {}, HANDLE
            if "Use CSV" in user_text:
                record = {"title": "CSV format decision", "text": "User chose CSV because it preserves a stable header.",
                    "subject": "CSV", "facets": ["decision"], "actors": ["user"], "artifact": None,
                    "rationale": {"actor": "user", "quote": "because it preserves a stable header."},
                    "state": "reported", "occurred_date": "", "event_id": user["event_id"],
                    "evidence_span_ids": [user["spans"][0]["span_id"]]}
            elif "Move Slate" in user_text:
                event = next(event for event in events if event["kind"] == "PostToolUse")
                record = {"title": "Slate dataset moved", "text": "Moved Slate dataset to reviewed/slate-final.csv.",
                    "subject": "Slate dataset", "facets": ["activity"], "actors": ["agent"],
                    "artifact": {"name": "Slate dataset", "location": "reviewed/slate-final.csv"},
                    "rationale": None, "state": "observed", "occurred_date": "", "event_id": event["event_id"],
                    "evidence_span_ids": [event["spans"][0]["span_id"]]}
            else:
                record = self.location_record(payload)
            return {"records": [record]}, {}, HANDLE
        worker = Worker(self.store, self.config, runner=runner, live=False)
        self.assertEqual(worker.run(max_seconds=10)["completed"], 1)
        for case in ("decision", "location", "correction"):
            self.ingest_case(case)
            self.assertEqual(worker.run(max_calls=4, max_seconds=10)["completed"], 1)
        self.assertEqual(len(resolver_calls), 3)
        with self.store.open() as state:
            activate_generation(state.db, "worker-fixture")
            applied = [dict(row) for row in state.db.execute("SELECT candidate_json,document_id FROM episode_candidates ORDER BY created")]
            self.assertEqual(len(applied), 3)
            self.assertEqual(applied[1]["document_id"], applied[2]["document_id"])
            records = [json.loads(row["candidate_json"])["memory_record"] for row in applied]
            self.assertEqual(records[0]["reason_actor"], "user")
            self.assertEqual(records[1]["location"], "/Users/demo/My Data/slate-reviewed.csv")
            self.assertEqual(records[2]["location"], "reviewed/slate-final.csv")
        self.store.refresh_documents()
        delivered = self.store.search(self.ctx, {"version": VERSION, "query": "Where is the Slate dataset?", "project": "maple"})
        self.assertTrue(delivered["answerable"])
        self.assertIn("reviewed/slate-final.csv", json.dumps(delivered))
        self.assertNotIn("/Users/demo/My Data/slate-reviewed.csv", json.dumps(delivered))
        self.assertNotIn("tool_input", json.dumps(delivered))
        self.assertLessEqual(len(json.dumps(delivered, ensure_ascii=True)), 4000)
        self.withdraw(self.sources["correction-t"])
        after = self.store.search(self.ctx, {"version": VERSION, "query": "Where is the Slate dataset?", "project": "maple"})
        self.assertNotIn("reviewed/slate-final.csv", json.dumps(after))
        with self.store.open() as state:
            self.assertIsNone(state.db.execute("SELECT provider_session FROM backend_observers").fetchone()[0])

    def test_expired_handle_holds_and_explicit_recovery_reconstructs_originals(self):
        self.run_routine()
        self.ingest_case("decision")
        def expired(*args, **kwargs):
            self.assertEqual(kwargs["session_id"], HANDLE)
            raise HarnessError("expired_fixture_session")
        worker = Worker(self.store, self.config, runner=expired, live=False)
        self.assertEqual(worker.run(max_seconds=10)["completed"], 0)
        with self.store.open() as state:
            job = state.db.execute("SELECT id FROM backend_jobs WHERE status='held'").fetchone()[0]
        worker.recover(job)
        worker.runner = self.no_learning
        result = worker.run(max_seconds=10)
        self.assertEqual(result["completed"], 1)
        self.assertIsNone(self.calls[-1]["session"])
        self.assertTrue(self.calls[-1]["payload"]["observer_reconstruction"])
        self.assertTrue(self.calls[-1]["payload"]["previous_evidence_index"])

    def test_large_prior_turn_is_omitted_with_explicit_reconstruction_gap(self):
        # Augment the frozen routine intent with synthetic non-durable chatter.
        with self.store.open() as state, state.db:
            state.db.execute("UPDATE memories SET body=body||? WHERE id=?",
                             (" pending status" * 600, self.sources["routine-u"]))
        self.run_routine()
        self.ingest_case("decision")
        cfg = dict(self.config, backend_worker={"max_context_chars": 3000})
        result = Worker(self.store, cfg, runner=self.no_learning, live=False).run(max_seconds=10)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.calls[-1]["payload"]["previous_evidence_index"], [])
        with self.store.open() as state:
            receipt = state.db.execute("SELECT usage FROM backend_worker_receipts WHERE status='bounded_original_context_gap'").fetchone()
            self.assertIsNotNone(receipt)
            self.assertEqual(json.loads(receipt[0])["omitted_sources"], 3)


if __name__ == "__main__":
    unittest.main()
