from vaelius_test_support.fixtures.state import invalidate_source
"""Populated, source-grounded timeline regression shared with the PG rehearsal.

The JSON fixture is synthetic and frozen before production behavior changes.
Scripted extraction tests pipeline validation, never model extraction quality.
"""
import hashlib
import json
from pathlib import Path
import unittest

import test_episode_pipeline as base
from agenthub.processing.episode_pipeline import (
    activate_generation, episode_links_for_document, get_episode_view, run_once,
)

FIXTURE = Path(__file__).parent / "fixtures/local_staging_timeline_v1.json"
FIXTURE_HASH = "b3b29c36bbba24708278f9dcd342f2164a6413a6f058e727eab707fa09e76c64"


class TimelineRunner:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    def __call__(self, home, config, instruction, payload, schema):
        purpose = config["_purpose"]
        self.calls.append(purpose)
        if purpose == "episode_resolve":
            return {"candidate_key": payload["candidate"]["candidate_key"],
                    "operation": "CREATE", "target_artifact_id": "",
                    "reason": "new_claim"}, {}
        if purpose != "durable_memory_curate":
            raise AssertionError(purpose)
        event = next(e for e in payload["episode"]["events"]
                     if e["kind"] == "PostToolUse")
        record = {"title": "Slate dataset saved", "text": self.fixture["claim"],
                  "subject": "Slate dataset", "facets": ["activity"],
                  "actors": ["agent"], "artifact": self.fixture["artifact"],
                  "rationale": None, "state": "observed", "occurred_date": "",
                  "event_id": event["event_id"],
                  "evidence_span_ids": [event["spans"][0]["span_id"]]}
        return {"records": [record]}, {}


class PopulatedTimelineTests(unittest.TestCase):
    tearDown = base.EpisodePipelineTests.tearDown

    def setUp(self):
        base.EpisodePipelineTests.setUp(self)
        self.fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_HASH)
        self.cfg["episode_curation"]["policy"] = "durable_memory"
        (self.home / "config.json").write_text(json.dumps(self.cfg))
        self.runner = TimelineRunner(self.fixture)
        self.populate()

    def populate(self):
        """Actual completed jobs, validated claims and active generation."""
        with self.state.db:
            for episode in self.fixture["episodes"]:
                events = [(episode["turn"] + "-u", "UserPromptSubmit", self.fixture["intent"], 0)]
                events += [(ident, "PostToolUse", self.fixture["claim"], 1)
                           for ident in episode["tools"]]
                events += [(episode["turn"] + "-s", "Stop", "Saved and verified Slate dataset.", 2)]
                for ident, kind, body, delta in events:
                    self.state.db.execute("""INSERT INTO memories
                        (id,session,project,body,kind,created,active,exit_code,turn)
                        VALUES(?,?,?,?,?,?,1,?,?)""",
                        (ident, episode["session"], self.fixture["project"], body, kind,
                         episode["created"] + delta, 0 if kind == "PostToolUse" else None,
                         episode["turn"]))
                    self.state.db.execute("""INSERT INTO source_event_metadata
                        (source_id,tool_name,source_role,capture_id,event_fields,response_shape,created)
                        VALUES(?,?,?,NULL,'[]','null',?)""",
                        (ident, "exec_command" if kind == "PostToolUse" else "",
                         "episode_evidence", episode["created"] + delta))
        for _ in self.fixture["episodes"]:
            self.assertTrue(run_once(self.state, self.cfg, self.runner))
        activate_generation(self.state.db, self.fixture["generation"])
        view = get_episode_view(self.state.db, self.fixture["generation"],
                                self.fixture["project"], "a-session", "export")
        self.assertEqual(view["completion"], "complete")
        self.document = view["assertions"][0]["document_id"]
        self.revision = view["assertions"][0]["revision_id"]
        # Each extra support quotes the identical actual original assertion. A
        # second original span for export-t exercises join multiplicity, not
        # invented independent corroboration.
        with self.state.db:
            for episode in self.fixture["episodes"]:
                for ident in episode["tools"]:
                    self.state.db.execute("""INSERT INTO knowledge_support
                        VALUES(?,?,?,'supports','unknown',101)
                        ON CONFLICT DO NOTHING""",
                        (self.revision, ident, ident + "-identical-source-span"))
            for duplicate in self.fixture["duplicates"]:
                self.state.db.execute("""INSERT INTO knowledge_support
                    VALUES(?,?,?,'supports','unknown',101) ON CONFLICT DO NOTHING""",
                    (self.revision, duplicate["source_id"], duplicate["span_id"]))

    def links(self, **kwargs):
        return episode_links_for_document(self.state.db, self.fixture["generation"],
                                         self.fixture["project"], self.document, **kwargs)

    @staticmethod
    def identities(links):
        return [e["session"] + "/" + e["source_turn"] for e in links["episodes"]]

    def test_populated_multisession_deduplicated_deterministic_pagination_and_detail(self):
        links = self.links()
        self.assertEqual(self.identities(links), self.fixture["expected"]["before"])
        self.assertFalse(links["independent_support"])
        first = next(e for e in links["episodes"] if e["session"] == "a-session")
        self.assertEqual(first["supporting_source_ids"], self.fixture["expected"]["first_supports"])
        self.assertEqual(len(first["supporting_source_ids"]), len(set(first["supporting_source_ids"])))
        pages = [self.links(limit=1, offset=i) for i in range(4)]
        self.assertEqual([self.identities(p)[0] for p in pages], self.identities(links))
        self.assertEqual([p["has_more"] for p in pages], [True, True, True, False])
        self.assertEqual(self.links(limit=1, offset=4)["episodes"], [])
        for entry in links["episodes"]:
            self.assertTrue(entry["handle"].startswith("ep_"))
            self.assertIsNone(entry["known_event_day"])
            view = get_episode_view(self.state.db, self.fixture["generation"],
                self.fixture["project"], entry["session"], entry["source_turn"])
            self.assertTrue(view["assertions"])
            evidence = view["assertions"][0]["evidence"]
            self.assertTrue(evidence)
            self.assertTrue(all(e["source_id"] in entry["supporting_source_ids"] for e in evidence))
            self.assertIn(self.fixture["claim"], entry["summary"])
            self.assertLessEqual(len(entry["summary"]), 2400)
            self.assertTrue(entry["derived_context"] if "derived_context" in entry else links["derived_context"])

    def test_withdrawn_and_historical_supports_cannot_survive_in_timeline(self):
        invalidate_source(self.state,self.fixture["withdraw"])
        with self.state.db:
            self.state.db.execute("INSERT INTO historical_sources VALUES(?,?)",
                                  (self.fixture["historical"], "synthetic-new-revision"))
        links = self.links()
        self.assertEqual(self.identities(links), self.fixture["expected"]["after"])
        serialized = json.dumps(links)
        self.assertNotIn(self.fixture["withdraw"], serialized)
        self.assertNotIn(self.fixture["historical"], serialized)
        self.assertIsNone(get_episode_view(self.state.db, self.fixture["generation"],
            self.fixture["project"], "c-session", "handoff"))

    def test_unavailable_document_generation_and_page_bounds_are_explicit(self):
        with self.assertRaises(ValueError):
            self.links(limit=0)
        with self.assertRaises(ValueError):
            self.links(offset=1001)
        other = episode_links_for_document(self.state.db, self.fixture["generation"],
                                          "another-project", self.document)
        self.assertEqual(other["episodes"], [])
        self.assertEqual(other["coverage_gaps"], ["document_unavailable"])
        missing = episode_links_for_document(self.state.db, "absent-generation",
                                            self.fixture["project"], self.document)
        self.assertEqual(missing["coverage_gaps"], ["generation_unavailable"])

    def test_retiring_last_member_removes_temporal_children_without_erasing_other_facts(self):
        from agenthub.processing.temporal import select_assertions
        victim = get_episode_view(self.state.db, self.fixture["generation"],
            self.fixture["project"], "c-session", "handoff")["assertions"][0]["document_id"]
        before = [dict(row) for row in self.state.db.execute(
            "SELECT assertion_id,document_id FROM knowledge_temporal_assertions")]
        removed = {row["assertion_id"] for row in before if row["document_id"] == victim}
        retained = {row["assertion_id"] for row in before if row["document_id"] != victim}
        self.assertTrue(removed)
        self.assertTrue(retained)
        # A reviewed=false relation cannot claim corroboration, but must still be
        # purged when either referenced assertion loses its entire document.
        with self.state.db:
            self.state.db.execute("INSERT INTO knowledge_temporal_relations VALUES(?,?,'supersedes',0,100)",
                (sorted(removed)[0], sorted(retained)[0]))
        for as_of in (None, "2026-09-26T00:00:00+00:00"):
            selected = select_assertions(self.state.db, self.fixture["project"],
                authorized_document_ids=[victim], as_of=as_of)
            self.assertTrue(selected["assertions"] or selected["unknown_time"])
        # Exercise canonical compiler retirement; API source deletion/cascade
        # is covered by the general-source and PostgreSQL worker tests.
        from agenthub.processing.knowledge import retire_memory
        with self.state.db:
            members=self.state.db.execute('SELECT memory_id FROM knowledge_document_members WHERE document_id=?',(victim,)).fetchall()
            for member in members:retire_memory(self.state.db,member[0],withdrawn=True)
        invalidate_source(self.state,self.fixture["withdraw"])
        after = {row[0] for row in self.state.db.execute("SELECT assertion_id FROM knowledge_temporal_assertions")}
        self.assertEqual(after, retained)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM knowledge_temporal_evidence e LEFT JOIN knowledge_temporal_assertions a ON a.assertion_id=e.assertion_id WHERE a.assertion_id IS NULL").fetchone()[0], 0)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM knowledge_temporal_relations").fetchone()[0], 0)
        for as_of in (None, "2026-09-26T00:00:00+00:00"):
            selected = select_assertions(self.state.db, self.fixture["project"],
                authorized_document_ids=[victim], as_of=as_of)
            self.assertEqual(selected["assertions"], [])
            self.assertEqual(selected["unknown_time"], [])
        linked = episode_links_for_document(self.state.db, self.fixture["generation"],
            self.fixture["project"], victim)
        self.assertEqual(linked["coverage_gaps"], ["document_unavailable"])
        self.assertIsNone(get_episode_view(self.state.db, self.fixture["generation"],
            self.fixture["project"], "c-session", "handoff"))
        self.assertEqual(self.identities(self.links()), self.fixture["expected"]["before"][:-1])


if __name__ == "__main__":
    unittest.main()
