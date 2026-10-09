"""The fast local operator path must preserve exact validated event bytes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentclient.enterprise_capture import normalize_capture
from agentclient.general_contract import split_event
from agenthub.historical_backfill import LocalPartBackend


class _Store:
    def __init__(self):
        self.events = []

    def authenticate(self, token):
        if token != "synthetic-token":
            raise ValueError("wrong_token")
        return {"actor": "synthetic"}

    def require_ready(self):
        return None

    def ingest_general(self, ctx, event):
        self.events.append(event)
        return {"source_id": "synthetic-source", "disposition": "accepted"}


class _Registry:
    def __init__(self, store):
        self.store = store

    def store_for_token(self, token):
        return self.store


class HistoricalBackfillTests(unittest.TestCase):
    def test_large_event_reassembles_once_with_exact_digest_and_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "token"
            path.write_text("synthetic-token")
            path.chmod(0o600)
            store = _Store()
            with patch("agenthub.historical_backfill.runtime", return_value=({}, _Registry(store))):
                backend = LocalPartBackend(temporary, path)
            event = normalize_capture({"hook_event_name": "UserPromptSubmit", "event_id": "item-1",
                "session_id": "chat-1", "turn_id": "turn-1", "prompt": "synthetic " * 23000},
                "project", "connection")
            parts = split_event(event)
            self.assertGreater(len(parts), 1)
            receipts = [backend.request("/enterprise/v2/parts", part) for part in parts]
            self.assertFalse(receipts[0]["complete"])
            self.assertTrue(receipts[-1]["complete"])
            self.assertEqual(store.events, [event])
            with self.assertRaises(ValueError):
                backend.request("/enterprise/v2/parts", {**parts[0], "part_count": 1,
                    "digest": "0" * 64})


if __name__ == "__main__":
    unittest.main()
