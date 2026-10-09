"""Frozen oversized H6 episode detail contract (source remains synthetic)."""

import json
import unittest

from agenthub.project_history import project_episode_detail
from test_project_history import Store


class DetailBoundTests(unittest.TestCase):
    def test_oversized_assertion_chunks_are_bounded_and_recoverable(self):
        store = Store()
        try:
            store.load("reversion")
            ctx = {"tenant": "synthetic-lab", "actor": "ben", "actions": ["read"],
                   "projects": ["cedar-routing"]}
            key = ("cedar-routing", "session-1", "turn-1")
            view = store.views[key]
            original = "Synthetic bridge inspection detail. " * 260
            view["assertions"][0]["text"] = original
            view["has_more"] = True
            offset = 0
            chunks = []
            for _ in range(10):
                page = project_episode_detail(store, ctx, "cedar-routing", "dev-reversion-e2",
                    offset=0, limit=1, text_offset=offset, view_loader=store.view_loader)
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=True,
                    separators=(",", ":")).encode()), 4000)
                chunks.append(page["assertions"][0]["text"])
                if page["next_text_offset"] is None:
                    self.assertEqual(page["next_offset"], 1)
                    break
                self.assertTrue(page["assertions"][0]["text_truncated"])
                self.assertIn("assertion_text_continuation", page["coverage_gaps"])
                offset = page["next_text_offset"]
            self.assertEqual("".join(chunks), original)
            with self.assertRaisesRegex(ValueError, "project_episode_page"):
                project_episode_detail(store, ctx, "cedar-routing", "dev-reversion-e2",
                    text_offset=-1, view_loader=store.view_loader)
        finally:
            store.db.close()


if __name__ == "__main__":
    unittest.main()
