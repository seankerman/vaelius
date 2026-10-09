"""Frozen H6 overview contract over the H1 synthetic DEV oracle (no providers)."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from agenthub.project_history import project_episode_detail, project_overview
from agentclient.cloud_contract import validate_response


FIXTURE = Path(__file__).parents[1] / "tools/fixtures/history_retrieval_v1/development.json"
DEV = json.loads(FIXTURE.read_text())


class Store:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE knowledge_generations(generation_id TEXT,status TEXT,created REAL);
            CREATE TABLE episode_occurrences(occurrence_id TEXT,project TEXT,session TEXT,turn TEXT,created REAL);
            CREATE TABLE episode_revisions(revision_id TEXT,occurrence_id TEXT,generation_id TEXT,
                revision_number INTEGER,source_ids TEXT,payload_json TEXT,recorded_at REAL);
            CREATE TABLE enterprise_sources(id TEXT,active INTEGER,payload_hash TEXT,readers TEXT,
                tenant TEXT,internal_project TEXT);
        """)
        self.db.execute("INSERT INTO knowledge_generations VALUES('gen-h1','active',1)")
        self.views = {}

    @contextmanager
    def open(self):
        yield type("State", (), {"db": self.db})()

    def _need(self, ctx, action):
        if action not in ctx["actions"]:
            raise PermissionError("no_read")

    def _readable_scopes(self, db, ctx, project=None):
        return [project] if project in ctx["projects"] else []

    def _visible_source(self, db, ctx, source):
        return bool(source and source["active"] and source["tenant"] == ctx["tenant"]
                    and ctx["actor"] in json.loads(source["readers"]))

    def load(self, family):
        sources = [s for s in DEV["sources"] if s["family_id"] == family]
        events = [e for e in DEV["oracle_events"] if e["family_id"] == family]
        for s in sources:
            self.db.execute("INSERT INTO enterprise_sources VALUES(?,?,?,?,?,?)",
                            (s["id"], 1, s["text_sha256"], json.dumps(s["reader_ids"]),
                             s["tenant_id"], s["project_id"]))
        for i, e in enumerate(events):
            occ = e["id"]
            session = f"session-{i}"
            self.db.execute("INSERT INTO episode_occurrences VALUES(?,?,?,?,?)",
                            (occ, e["project_id"], session, f"turn-{i}", float(i)))
            source_ids = [r["source_id"] for r in e["source_refs"]]
            self.db.execute("INSERT INTO episode_revisions VALUES(?,?,?,?,?,?,?)",
                            (f"rev:{occ}:1", occ, "gen-h1", 1, json.dumps(source_ids),
                             json.dumps({"event_id": occ}), float(i)))
            self.views[(e["project_id"], session, f"turn-{i}")] = {
                "episode_id": occ, "occurrence_id": occ, "curated_revision_id": f"rev:{occ}:1",
                "summary_revision": f"summary:{occ}:1", "summary": e["source_refs"][0]["quote"],
                "assertions": [{"state": e["status"], "rationale": {"actor": e["actor_id"],
                    "quote": e["reason"]} if e["reason"] else None,
                    "occurred_date": e["effective_at"], "evidence": e["source_refs"]}],
                "coverage_gaps": [], "completion": "complete", "source_range": {
                    "captured_at_start": float(i), "captured_at_end": float(i),
                    "basis": "capture_time_not_event_time"},
                "project": e["project_id"], "session": session, "source_turn": f"turn-{i}"}
        self.db.commit()

    def view_loader(self, db, generation, project, session, turn, source_authorizer):
        value = self.views.get((project, session, turn))
        if value is None:
            return None
        if any(not source_authorizer(r["source_id"])
               for a in value["assertions"] for r in a["evidence"]):
            return None
        return value


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.ctx = {"tenant": "synthetic-lab", "actor": "ben", "actions": ["read"],
                    "projects": ["oak-build", "cedar-routing", "spruce-shift"]}

    def tearDown(self):
        self.store.db.close()

    def call(self, project, **kwargs):
        return project_overview(self.store, self.ctx, project,
                                view_loader=self.store.view_loader, **kwargs)

    def test_long_fixture_has_early_middle_recent_and_complete_bounded_pages(self):
        self.store.load("long_pages")
        seen = []
        cursor = None
        anchors = None
        for _ in range(20):
            page = self.call("oak-build", cursor=cursor, page_limit=4, max_bytes=900)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=True, separators=(",", ":")).encode()), 900)
            if anchors is None:
                anchors = page["phase_anchors"]
                self.assertEqual([x["phase"] for x in anchors], ["early", "middle", "recent"])
                self.assertEqual(anchors[0]["episode_id"], "dev-long_pages-e01")
                self.assertEqual(anchors[-1]["episode_id"], "dev-long_pages-e12")
            seen.extend(x["episode_id"] for x in page["episodes"])
            if not page["has_more"]:
                break
            self.assertTrue(page["next_cursor"])
            cursor = page["next_cursor"]
        expected = [f"dev-long_pages-e{i:02d}" for i in range(1, 13)] + [
            "dev-long_pages-document1", "dev-long_pages-document2"]
        self.assertEqual(set(seen), set(expected))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(seen.index("dev-long_pages-e01"), 0)
        self.assertEqual(page["coverage"], "available_authorized_history")

    def test_long_fixture_320_byte_pages_preserve_every_occurrence_and_stale_cursor(self):
        self.store.load("long_pages")
        seen = []
        cursor = None
        first_cursor = None
        for _ in range(30):
            page = self.call("oak-build", cursor=cursor, page_limit=20, max_bytes=320)
            validate_response('/enterprise/v3/project-history', page)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=True,
                separators=(",", ":")).encode()), 320)
            self.assertTrue(page["episodes"])
            self.assertIn("capture_coverage_unverified", page["coverage_gaps"])
            seen.extend(item["episode_id"] for item in page["episodes"])
            if not page["has_more"]:
                break
            self.assertTrue(page["next_cursor"])
            first_cursor = first_cursor or page["next_cursor"]
            cursor = page["next_cursor"]
        expected = [f"dev-long_pages-e{i:02d}" for i in range(1, 13)] + [
            "dev-long_pages-document1", "dev-long_pages-document2"]
        self.assertEqual(set(seen), set(expected))
        self.assertEqual(len(seen), len(expected))
        self.assertEqual([item for item in seen if '-document' not in item], expected[:12])
        self.store.db.execute("UPDATE enterprise_sources SET active=0 WHERE id='dev-long_pages-phase02'")
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, "project_history_cursor_stale"):
            self.call("oak-build", cursor=first_cursor, page_limit=20, max_bytes=320)

    def test_320_byte_page_with_opaque_occurrence_id_and_three_honest_gaps(self):
        from agenthub.project_history import _minimal_overview, _size
        entries=[{'episode_id':'occ_'+('a'*64)}, {'episode_id':'occ_'+('b'*64)}]
        page=_minimal_overview(entries,'oak-build','c'*64,0,20,320,
            ['capture_coverage_unverified','unknown_event_time','curation_partial'])
        self.assertEqual(page['episodes'][0]['episode_id'],entries[0]['episode_id'])
        self.assertTrue(page['has_more'])
        self.assertLessEqual(_size(page),320)
        self.assertIn('capture_coverage_unverified',page['coverage_gaps'])
        self.assertIn('unknown_event_time',page['coverage_gaps'])

    def test_revision_and_source_policy_changes_stale_cursor_and_summary(self):
        self.store.load("reversion")
        first = self.call("cedar-routing", page_limit=1, max_bytes=1200)
        self.assertTrue(first["next_cursor"])
        before = first["summary_revision"]
        self.store.db.execute("UPDATE enterprise_sources SET active=0 WHERE id='dev-reversion-s2'")
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, "project_history_cursor_stale"):
            self.call("cedar-routing", cursor=first["next_cursor"], page_limit=1, max_bytes=1200)
        current = self.call("cedar-routing", page_limit=3, max_bytes=1500)
        self.assertNotEqual(before, current["summary_revision"])
        self.assertNotIn("dev-reversion-e2", [x["episode_id"] for x in current["episodes"]])
        self.assertNotIn("dev-reversion-s2", json.dumps(current))

    def test_unknown_event_date_and_capture_gap_are_labeled_without_inference(self):
        self.store.load("unknown_dst")
        page = self.call("spruce-shift", page_limit=3, max_bytes=1600,
                         authorized_gaps=("capture_gap", "unprocessed_range"))
        undated = next(x for x in page["episodes"] if x["episode_id"] == "dev-unknown_dst-e1")
        self.assertIsNone(undated["event_time"])
        self.assertEqual(undated["time_basis"], "unknown")
        self.assertIn("unknown_event_time", page["coverage_gaps"])
        self.assertIn("capture_gap", page["coverage_gaps"])
        self.assertIn("unprocessed_range", page["coverage_gaps"])
        self.assertEqual([x["event_time"] for x in page["episodes"] if x["event_time"]],
                         ["2026-11-01T01:30:00-06:00", "2026-11-01T01:30:00-07:00"])

    def test_detail_expands_cited_reason_and_current_authorization(self):
        self.store.load("reversion")
        detail = project_episode_detail(self.store, self.ctx, "cedar-routing", "dev-reversion-e2",
                                        view_loader=self.store.view_loader)
        self.assertEqual(detail["assertions"][0]["rationale"]["quote"], "route A bridge closed")
        self.assertEqual(detail["assertions"][0]["evidence"][0]["source_id"], "dev-reversion-s2")
        self.store.db.execute("UPDATE enterprise_sources SET active=0 WHERE id='dev-reversion-s2'")
        self.store.db.commit()
        with self.assertRaises(PermissionError):
            project_episode_detail(self.store, self.ctx, "cedar-routing", "dev-reversion-e2",
                                   view_loader=self.store.view_loader)

    def test_project_access_is_required_even_when_source_is_readable(self):
        self.store.load("reversion")
        self.ctx["projects"] = []
        with self.assertRaises(PermissionError):
            self.call("cedar-routing")


if __name__ == "__main__":
    unittest.main()
