"""Outage, durable acknowledgement and expiry against frozen synthetic capture."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentclient.capture_outbox import Outbox
from agentclient.enterprise_capture import normalize_capture

FIXTURE = Path(__file__).parent / "fixtures/local_staging_transport_v1.json"
FIXTURE_HASH = "048c2bf50b48d6ea4ec816d01ad28799fb8e7c8beb0e413775ccaf1fc5b94e25"


class Receiver:
    def __init__(self):
        self.mode = "outage"
        self.parts = []

    def request(self, path, part):
        self.parts.append(part)
        if self.mode == "outage":
            raise ConnectionError("synthetic unavailable backend")
        return {"digest": part["digest"], "complete": self.mode == "durable",
                "source_id": "synthetic-durable-source"}


class StagingTransportTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_HASH)
        self.fixture = json.loads(FIXTURE.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        with patch("agenthub.processing.harness.run_structured", side_effect=AssertionError("client inference")), \
             patch("agenthub.processing.harness.run_structured_session", side_effect=AssertionError("client inference")):
            self.event = normalize_capture(self.fixture["event"], "maple", "staging-agent")
        self.box = Outbox(self.home)
        self.ident = self.box.put(self.event)

    def tearDown(self):
        self.box.close()
        self.temp.cleanup()

    def retry_now(self):
        with self.box.db:
            self.box.db.execute("UPDATE pending SET next_attempt=0")

    def test_outage_restart_partial_ack_and_durable_ack_preserve_boundaries(self):
        pending = self.box.db.execute("SELECT payload FROM pending").fetchone()[0]
        self.assertNotIn(self.fixture["expected"]["secret"], pending)
        self.assertIn(self.fixture["expected"]["path"], pending)
        receiver = Receiver()
        result = self.box.drain(receiver)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["pending_events"], 1)
        self.assertFalse(result["searchable"])
        self.assertFalse(result["local_corpus_opened"])
        self.box.close()
        self.box = Outbox(self.home)
        self.retry_now()
        receiver.mode = "partial"
        self.assertEqual(self.box.drain(receiver)["acknowledged_events"], 0)
        self.assertEqual(self.box.status()["pending_events"], 1)
        self.retry_now()
        receiver.mode = "durable"
        result = self.box.drain(receiver)
        self.assertEqual(result["acknowledged_events"], 1)
        self.assertEqual(result["pending_events"], 0)
        self.assertEqual(self.box.db.execute("SELECT source_id FROM receipts").fetchone()[0], "synthetic-durable-source")
        self.assertEqual(self.box.put(self.event), self.ident)
        self.assertEqual(self.box.status()["pending_events"], 0)
        self.assertFalse((self.home / "knowledge.sqlite").exists())

    def test_expired_payload_becomes_explicit_gap_without_raw_content(self):
        with self.box.db:
            self.box.db.execute("UPDATE pending SET created=0")
        receiver = Receiver()
        self.box.drain(receiver)
        event = json.loads(self.box.db.execute("SELECT payload FROM pending").fetchone()[0])
        self.assertEqual(event["disposition"], "incomplete")
        self.assertEqual(event["event"]["kind"], "gap")
        self.assertEqual(event["blocks"], [])
        self.assertNotIn(self.fixture["expected"]["payload_canary"], json.dumps(event))
        self.assertEqual(self.box.status()["gaps"], {"expired_payload_replaced_by_gap": 1})


if __name__ == "__main__":
    unittest.main()
