"""Synthetic historical capture contracts; no private Codex rollouts or provider calls."""
import base64
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from agentclient.enterprise_backfill import Limits, apply_manifest, discover, preflight, status_manifest, _event


class _Backend:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.parts = []

    def request(self, path, value):
        if self.fail:
            raise ConnectionError("synthetic outage")
        self.parts.append((path, value))
        return {"digest": value["digest"], "complete": True, "source_id": "synthetic-source"}


class EnterpriseBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.project = self.root / "project"
        self.project.mkdir()
        self.rollouts = self.codex / "sessions" / "2026" / "09" / "29"
        self.rollouts.mkdir(parents=True)

    def write_session(self, ident="session-1", *, subagent=False):
        path = self.rollouts / f"rollout-{ident}.jsonl"
        header = {"type": "session_meta", "payload": {"id": ident, "cwd": str(self.project)}}
        if subagent:
            header["payload"]["source"] = {"subagent": {"name": "excluded"}}
        rows = [header,
            {"type": "event_msg", "timestamp": "2026-09-29T12:00:00Z", "payload": {
                "type": "task_started", "turn_id": "turn-1"}},
            {"type": "event_msg", "timestamp": "2026-09-29T12:00:01Z", "payload": {
                "type": "item_completed", "thread_id": ident, "turn_id": "turn-1", "item": {
                    "id": "question-1", "type": "UserMessage", "content": "Where is the synthetic dataset?"}}},
            {"type": "event_msg", "timestamp": "2026-09-29T12:00:02Z", "payload": {
                "type": "item_completed", "thread_id": ident, "turn_id": "turn-1", "item": {
                    "id": "answer-1", "type": "AgentMessage", "content": "The synthetic dataset is in data/example.csv.", "phase": "final"}}},
            {"type": "event_msg", "timestamp": "2026-09-29T12:00:03Z", "payload": {
                "type": "task_complete", "turn_id": "turn-1", "last_agent_message": "The synthetic dataset is in data/example.csv."}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def manifest(self):
        return discover(self.codex, [{"root": str(self.project), "project": "personal",
            "connection": "personal-codex"}], max_sessions=10)

    def test_manifest_is_allowlisted_and_excludes_subagents(self):
        self.write_session()
        self.write_session("child", subagent=True)
        manifest = self.manifest()
        self.assertEqual(len(manifest["sessions"]), 1)
        self.assertEqual(manifest["summary"]["subagents"], 1)
        self.assertEqual(manifest["sessions"][0]["project"], "personal")

    def test_provider_free_preflight_and_resumable_acknowledged_delivery(self):
        self.write_session()
        manifest = self.manifest()
        preview = preflight(manifest, Limits(max_lines=10))
        self.assertEqual(preview["completed_turns"], 1)
        self.assertEqual(preview["deliverable_events"], 3)
        self.assertNotIn("synthetic dataset", json.dumps(preview))
        home = self.root / "private-profile"
        self.assertEqual(status_manifest(manifest, home)["bytes_committed"], 0)
        first = apply_manifest(manifest, home, _Backend(fail=True), Limits(max_lines=2))
        self.assertEqual(first["status"], "transport_blocked")
        self.assertEqual(first["source_lines_advanced"], 2)
        paused = status_manifest(manifest, home)
        self.assertGreater(paused["bytes_committed"], 0)
        self.assertGreater(paused["pending_events"], 0)
        second_backend = _Backend()
        self.assertEqual(apply_manifest(manifest, home, second_backend,
                                        Limits(max_lines=10))["status"], "transport_blocked")
        time.sleep(1.05)  # Existing outbox retry backoff is durable across runs.
        second = apply_manifest(manifest, home, second_backend, Limits(max_lines=10))
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["outbox"]["pending_events"], 0)
        self.assertEqual(len(second_backend.parts), 3)
        repeated = apply_manifest(manifest, home, second_backend, Limits(max_lines=10))
        self.assertEqual(repeated.get("source_lines_advanced", 0), 0)
        self.assertEqual(len(second_backend.parts), 3)
        completed = status_manifest(manifest, home)
        self.assertEqual((completed["status"], completed["bytes_remaining"],
                          completed["pending_events"]), ("complete", 0, 0))
        self.assertNotIn("synthetic dataset", json.dumps(completed))

    def test_same_native_item_id_in_distinct_sessions_has_distinct_source_identity(self):
        first = self.write_session("session-1")
        second = self.write_session("session-2")
        items = self.manifest()["sessions"]
        events = []
        for path, item in zip((first, second), items):
            line = path.read_bytes().splitlines(keepends=True)[2]
            event, _, _ = _event(item, line, 100, "turn-1")
            events.append(event)
        self.assertEqual([value["event"]["kind"] for value in events], ["UserPromptSubmit"] * 2)
        self.assertNotEqual(events[0]["external_id"], events[1]["external_id"])

    def test_deleted_checkout_can_still_be_explicitly_allowlisted(self):
        self.write_session()
        self.project.rmdir()
        manifest = self.manifest()
        self.assertEqual(len(manifest["sessions"]), 1)
        self.assertEqual(preflight(manifest, Limits(max_lines=10))["completed_turns"], 1)

    def test_malformed_json_value_is_a_gap_not_an_import_crash(self):
        self.write_session()
        item = self.manifest()["sessions"][0]
        event, _, kind = _event(item, b"[]\n", 123, "turn-1")
        self.assertEqual(kind, "gap")
        self.assertEqual(event["disposition"], "incomplete")

    def test_historical_outbox_keeps_pending_evidence_past_forward_capture_expiry(self):
        self.write_session()
        manifest = self.manifest()
        home = self.root / "long-paused-backfill"
        first = apply_manifest(manifest, home, _Backend(fail=True), Limits(max_lines=2))
        self.assertEqual(first["status"], "transport_blocked")
        with sqlite3.connect(home / "capture-outbox.sqlite") as db:
            db.execute("UPDATE pending SET created=created-172800,next_attempt=0")
        backend = _Backend()
        resumed = apply_manifest(manifest, home, backend, Limits(max_lines=10))
        self.assertEqual(resumed["status"], "complete")
        restored = [json.loads(base64.b64decode(part["data"])) for _, part in backend.parts]
        self.assertIn("UserPromptSubmit", [value["event"]["kind"] for value in restored])

    def test_two_importers_cannot_advance_the_same_private_cursor(self):
        self.write_session()
        manifest = self.manifest()
        home = self.root / "contended-backfill"
        home.mkdir(mode=0o700)
        fd = os.open(home / "historical-backfill.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(fcntl.flock, fd, fcntl.LOCK_UN)
        with self.assertRaisesRegex(ValueError, "historical_backfill_already_running"):
            apply_manifest(manifest, home, _Backend(), Limits(max_lines=10))


if __name__ == "__main__":
    unittest.main()
