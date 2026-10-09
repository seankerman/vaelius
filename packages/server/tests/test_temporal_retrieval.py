from vaelius_test_support.fixtures.state import invalidate_source
"""Frozen synthetic cases for question-facing historical memory cards."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agenthub.processing.knowledge import ingest_observation
from vaelius_test_support.fixtures.state import State
from agenthub.processing.temporal import record_assertion
from agenthub.processing.temporal_retrieval import (
    detail_for_assertion, historical_cards, recheck_card,
)


class HistoricalCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "config.json").write_text(json.dumps({"observer": {"enabled": False}}))
        self.state = State(self.home)
        self.clock = datetime(2026, 9, 25, 18, tzinfo=timezone.utc)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_ordinary_last_error_wording_uses_general_search_path(self):
        self.assertIsNone(historical_cards(self.state,'What caused the last retry error?',
            'personal-a',reference_clock=self.clock))

    def add(self, name, *, project="personal-a", subject="dataset", predicate="location",
            value="/data/A", actor="agent", event=None, validity=None, change=None):
        db = self.state.db
        source = "source-" + name
        observation = {"title": name, "lesson": f"{subject} {predicate}: {value}",
                       "knowledge_type": "fact", "subjects": [subject],
                       "domain": "private_memory", "evidence_status": "execution_result"}
        body = "synthetic evidence " + str(value) + " " + " ".join(str(part) for part in (
            (event or {}).get("at"), (validity or {}).get("from"),
            (validity or {}).get("to")) if part) + " ongoing"
        with db:
            db.execute("INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)",
                       (source, "source-session", project, body, "PostToolUse", 1))
            db.execute("INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)",
                       (name, "curation", project, json.dumps(observation), "Observation", 2))
            db.execute("INSERT INTO observation_sources VALUES(?,?)", (name, source))
            doc = ingest_observation(db, name, project, "curation",
                {**observation, "evidence": [{"source_id": source}]}, force_new=True)
            assertion = record_assertion(db, revision_id=doc["revision_id"],
                subject=subject, predicate=predicate, value=value, actor=actor,
                event=event, validity=validity, evidence_source_ids=[source], change=change)
        return doc, assertion, source

    def add_move(self):
        _, old, _ = self.add("old-location", value="/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        _, new, source = self.add("new-location", value="/data/B", validity={
            "from": "2026-09-14", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"},
            change={"relation": "supersedes", "assertion_id": old["assertion_id"],
                    "reviewed": True})
        return old, new, source

    def search(self, query, *, read_projects=None):
        return historical_cards(self.state, query, "personal-a",
            read_projects=read_projects, timezone_name="UTC", reference_clock=self.clock)

    def test_as_of_and_explicit_current_have_answer_bearing_qualified_cards(self):
        old, new, _ = self.add_move()
        earlier = self.search("Where was dataset as of 2026-09-13?")
        later = self.search("Where was dataset as of 2026-09-14?")
        current = self.search("Where is dataset now?")
        self.assertEqual([r["item"]["assertion_id"] for r in earlier], [old["assertion_id"]])
        self.assertEqual([r["item"]["assertion_id"] for r in later], [new["assertion_id"]])
        self.assertEqual([r["item"]["assertion_id"] for r in current], [new["assertion_id"]])
        self.assertIn("/data/B", later[0]["card"]["text"])
        self.assertIn("2026-09-14", later[0]["card"]["text"])
        self.assertTrue(later[0]["card"]["as_of"].startswith("2026-09-14T"))
        self.assertEqual(later[0]["card"]["expand"], ["detail"])
        self.assertIn("Latest supported", current[0]["card"]["text"])
        self.assertEqual(later[0]["card"]["source_project"], "personal-a")
        self.assertIsNone(self.search("Where was dataset saved?"))

    def test_event_day_activity_and_relative_clock(self):
        _, activity, _ = self.add("monday-work", subject="survey report",
            predicate="activity", value="Completed the survey report",
            event={"at": "2026-09-21", "precision": "day",
                   "timezone": None, "basis": "explicit_source"})
        result = self.search("What did I work on last Monday?")
        self.assertEqual([r["item"]["assertion_id"] for r in result], [activity["assertion_id"]])
        self.assertIn("Completed the survey report", result[0]["card"]["text"])
        self.assertIn("2026-09-21", result[0]["card"]["text"])
        self.assertEqual(result[0]["card"]["event_day"], "2026-09-21")
        self.assertEqual(self.search("What did I work on last spring?"), [])

    def test_conflict_and_unknown_validity_abstain(self):
        self.add("undated", value="/data/unknown")
        self.assertEqual(self.search("Where was dataset as of 2026-09-05?"), [])
        self.add("conflict-a", value="/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.add("conflict-b", value="/data/B", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.assertEqual(self.search("Where was dataset as of 2026-09-05?"), [])

    def test_recheck_and_detail_drop_withdrawn_evidence(self):
        _, new, source = self.add("single-location", value="/data/verified", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        entry = self.search("Where was dataset as of 2026-09-05?")[0]
        self.assertEqual(recheck_card(self.state, entry, "personal-a")["card"]["id"],
                         new["assertion_id"])
        detail = detail_for_assertion(self.state, new["assertion_id"], "personal-a",
                                      as_of=entry["card"]["as_of"])
        self.assertIn("/data/verified", detail["text"])
        self.assertEqual(detail["evidence"][0]["source_id"], source)
        invalidate_source(self.state,source)
        self.assertIsNone(recheck_card(self.state, entry, "personal-a"))
        self.assertIsNone(detail_for_assertion(self.state, new["assertion_id"],
                          "personal-a", as_of=entry["card"]["as_of"]))

    def test_correction_and_project_scope_apply_to_details(self):
        _, false, _ = self.add("false-location", value="/data/wrong", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        _, corrected, _ = self.add("correct-location", value="/data/right", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"},
            change={"relation": "corrects", "assertion_id": false["assertion_id"],
                    "reviewed": True})
        self.add("other-project", project="personal-b", value="/data/private",
                 validity={"from": "2026-09-01", "to_status": "ongoing",
                           "precision": "day", "timezone": "UTC", "basis": "explicit_source"})
        result = self.search("Where was dataset as of 2026-09-05?")
        self.assertEqual([r["item"]["assertion_id"] for r in result],
                         [corrected["assertion_id"]])
        self.assertIsNone(detail_for_assertion(self.state, false["assertion_id"],
                          "personal-a", as_of=result[0]["item"]["as_of_utc"]))
        with self.assertRaisesRegex(ValueError, "read_scope_missing_receiver"):
            self.search("Where was dataset as of 2026-09-05?",
                        read_projects=["personal-b"])
        self.assertEqual(len(self.search("Where was dataset as of 2026-09-05?",
                                         read_projects=["personal-a", "personal-b"])), 0)

    def test_recheck_reconsiders_new_cross_project_collision(self):
        self.add("initial-location", value="/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        entry = self.search("Where was dataset as of 2026-09-05?",
            read_projects=["personal-a", "personal-b"])[0]
        self.add("later-collision", project="personal-b", value="/data/B", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.assertIsNone(recheck_card(self.state, entry, "personal-a",
                          read_projects=["personal-a", "personal-b"]))


if __name__ == "__main__":
    unittest.main()
