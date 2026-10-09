from vaelius_test_support.fixtures.state import invalidate_source
"""Synthetic history cases frozen before the temporal implementation."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agenthub.processing.knowledge import apply_resolved_observation, ingest_observation
from vaelius_test_support.fixtures.state import State
from agenthub.processing.temporal import parse_time_intent, record_assertion, select_assertions


class TemporalKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "config.json").write_text(json.dumps({"observer": {"enabled": False}}))
        self.state = State(self.home)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def add(self, name, *, project="personal-a", title=None):
        claim = {"title": title or name, "lesson": title or name,
                 "knowledge_type": "fact", "subjects": ["dataset"],
                 "domain": "private_memory", "evidence_status": "execution_result"}
        source = "source-" + name
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)",
                (source, "evidence", project, "synthetic tool result", "PostToolUse", 1))
            self.state.db.execute(
                "INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)",
                (name, "curation", project, json.dumps(claim), "Observation", 2))
            self.state.db.execute("INSERT INTO observation_sources VALUES(?,?)", (name, source))
            result = ingest_observation(self.state.db, name, project, "curation",
                {**claim, "evidence": [{"source_id": source}]}, force_new=True)
        return result, source

    def assert_fact(self, name, value, *, project="personal-a", actor="agent",
                    event=None, validity=None, change=None, recorded_at=100):
        document, source = self.add(name, project=project)
        with self.state.db:
            self.state.db.execute("UPDATE memories SET body=? WHERE id=?", (
                "synthetic evidence " + str(value) + " " +
                " ".join(str(part) for part in (
                    (event or {}).get("at"), (validity or {}).get("from"),
                    (validity or {}).get("to")) if part) + " ongoing", source))
        assertion = record_assertion(self.state.db, revision_id=document["revision_id"],
            subject="dataset", predicate="location", value=value, actor=actor,
            event=event, validity=validity, evidence_source_ids=[source],
            change=change, recorded_at=recorded_at)
        return document, assertion, source

    def select(self, *, as_of=None, project="personal-a", actor="agent", now=None):
        return select_assertions(self.state.db, project, subject="dataset",
            predicate="location", actor=actor, as_of=as_of, now=now)

    def curated_location(self, name, location, *, project="personal-a", state="observed",
                         occurred_date="2026-09-21", operation=None, target=None,
                         source_created=None, move_from=None):
        source = "source-" + name
        source_created = (datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp()
                          if source_created is None else source_created)
        context = {"policy": "durable-memory-8", "subject": "dataset",
                   "artifact_name": "survey-data", "location": location,
                   "actors": ["agent"], "reason_actor": "", "reason_quote": "",
                   "state": state, "occurred_date": occurred_date,
                   "source_created": source_created, "event_id": source,
                   "time_basis": "explicit_source_date" if occurred_date else "source_event_time",
                   "facets": ["artifact"], "attribution": (
                       "execution_result" if state == "observed" else
                       "user_reported" if state == "reported" else "execution_attempt")}
        observation = {"title": "Saved survey data", "lesson": "Survey data at " + location,
                       "knowledge_type": "fact", "subjects": ["dataset"],
                       "domain": "private_memory", "memory_context": context,
                       "evidence": [{"source_id": source}]}
        db = self.state.db
        with db:
            body = (("moved survey-data from " + move_from + " to " + location)
                    if move_from else "synthetic save " + location + " on " + occurred_date)
            db.execute("""INSERT INTO memories(id,session,project,body,kind,created,exit_code)
                VALUES(?,?,?,?,?,?,?)""",
                       (source, "source-session", project,
                        body, "UserPromptSubmit" if state == "reported" else "PostToolUse",
                        source_created, 0 if state == "observed" else 1 if state == "attempted" else None))
            db.execute("INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)",
                       (name, "curation", project, json.dumps(observation), "Observation", 2))
            db.execute("INSERT INTO observation_sources VALUES(?,?)", (name, source))
            if operation:
                result = apply_resolved_observation(db, name, project, "curation",
                                                    observation, operation, target)
            else:
                result = ingest_observation(db, name, project, "curation",
                                            observation, force_new=True)
        return result, source

    def test_additive_migration_is_idempotent_and_does_not_date_legacy_rows(self):
        self.add("old-untimed")
        from agenthub.processing.temporal import initialize
        initialize(self.state.db)
        initialize(self.state.db)
        count = self.state.db.execute("SELECT count(*) FROM knowledge_temporal_assertions").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.select()["status"], "absent")
        self.assertEqual(self.state.db.execute(
            "SELECT schema_version FROM knowledge_temporal_schema WHERE singleton=1").fetchone()[0], 1)

    def test_move_has_half_open_boundary_and_historical_answer(self):
        _, old, _ = self.assert_fact("old-location", "/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "America/Denver", "basis": "explicit_source"})
        self.assert_fact("new-location", "/data/B", validity={
            "from": "2026-09-14", "to_status": "ongoing", "precision": "day",
            "timezone": "America/Denver", "basis": "explicit_source"},
            change={"relation": "supersedes", "assertion_id": old["assertion_id"],
                    "reviewed": True})
        before = self.select(as_of="2026-09-13T23:59:59-06:00")
        at = self.select(as_of="2026-09-14T00:00:00-06:00")
        after = self.select(now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertEqual(before["assertions"][0]["value"], "/data/A")
        self.assertEqual(at["assertions"][0]["value"], "/data/B")
        self.assertEqual(after["assertions"][0]["value"], "/data/B")
        self.assertEqual([before["status"], at["status"], after["status"]],
                         ["supported", "supported", "supported"])

    def test_retrospective_correction_removes_false_value_even_as_of_past(self):
        _, false, _ = self.assert_fact("false-location", "/data/wrong", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.assert_fact("correct-location", "/data/right", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"},
            change={"relation": "corrects", "assertion_id": false["assertion_id"],
                    "reviewed": True}, recorded_at=200)
        result = self.select(as_of="2026-09-05T12:00:00Z")
        self.assertEqual(result["status"], "supported")
        self.assertEqual([a["value"] for a in result["assertions"]], ["/data/right"])
        self.assertEqual(result["assertions"][0]["recorded_at"], 200)

    def test_late_report_keeps_monday_event_separate_from_friday_recording(self):
        _, assertion, _ = self.assert_fact("monday-work", "/data/run-1",
            event={"at": "2026-09-21", "precision": "day",
                   "timezone": "America/Denver", "basis": "explicit_source"},
            recorded_at=datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp())
        result = select_assertions(self.state.db, "personal-a", subject="dataset",
            predicate="location", actor="agent", event_day="2026-09-21",
            timezone_name="America/Denver")
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["assertions"][0]["assertion_id"], assertion["assertion_id"])
        self.assertEqual(result["assertions"][0]["event_at"], "2026-09-21")

    def test_scoped_actor_and_predicate_do_not_auto_merge(self):
        self.assert_fact("agent-owner", "agent says A", actor="agent")
        self.assert_fact("user-owner", "user says B", actor="user")
        self.assert_fact("other-project", "other says C", project="personal-b")
        result = self.select(actor="agent")
        self.assertEqual([a["value"] for a in result["unknown_time"]], ["agent says A"])
        self.assertEqual(self.select(actor="user")["unknown_time"][0]["value"], "user says B")
        self.assertEqual(self.select(project="personal-b")["unknown_time"][0]["value"], "other says C")
        with self.assertRaisesRegex(ValueError, "read_scope_missing_receiver"):
            select_assertions(self.state.db, "personal-a", read_projects=["personal-b"])

    def test_unknown_dates_and_overlapping_values_are_not_silently_resolved(self):
        self.assert_fact("unknown-location", "/data/unknown")
        uncertain = self.select(as_of="2026-09-05T12:00:00Z")
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertEqual(uncertain["assertions"], [])
        self.assertEqual(len(uncertain["unknown_time"]), 1)
        self.assert_fact("conflict-a", "/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.assert_fact("conflict-b", "/data/B", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        conflict = self.select(as_of="2026-09-05T12:00:00Z")
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual({a["value"] for a in conflict["assertions"]}, {"/data/A", "/data/B"})

    def test_withdrawn_source_and_generation_rollback_do_not_restore_history(self):
        doc, old, source = self.assert_fact("withdrawn-old", "/data/withdrawn",
            validity={"from": "2026-09-01", "to_status": "ongoing",
                      "precision": "day", "timezone": "UTC", "basis": "explicit_source"})
        db = self.state.db
        db.execute("INSERT INTO knowledge_generations VALUES(?,?,?,?,?,?,?,?)",
            ("generation-old", "fixture", "active", "fixture", "hash", 1, 1, None))
        db.execute("INSERT INTO knowledge_generation_documents VALUES(?,?,?)",
            ("generation-old", doc["document_id"], 1))
        db.execute('INSERT INTO knowledge_generation_state VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET active_generation_id=excluded.active_generation_id', ("generation-old",))
        self.assertEqual(self.select(as_of="2026-09-05T12:00:00Z")["status"], "supported")
        invalidate_source(self.state,source)
        self.assertEqual(self.select(as_of="2026-09-05T12:00:00Z")["status"], "absent")
        db.execute("UPDATE knowledge_generations SET status='retired' WHERE generation_id='generation-old'")
        db.execute("UPDATE knowledge_generations SET status='active' WHERE generation_id='generation-old'")
        db.execute('INSERT INTO knowledge_generation_state VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET active_generation_id=excluded.active_generation_id', ("generation-old",))
        self.assertEqual(self.select(as_of="2026-09-05T12:00:00Z")["status"], "absent")

    def test_query_time_parser_uses_explicit_clock_and_zone(self):
        clock = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)
        yesterday = parse_time_intent("What did I do yesterday?", reference_clock=clock,
                                      timezone_name="America/Denver")
        self.assertEqual(yesterday["kind"], "as_of")
        self.assertEqual(yesterday["day"], "2026-11-01")
        self.assertEqual(yesterday["day_end_utc"] - yesterday["day_start_utc"], 25 * 3600)
        monday = parse_time_intent("What did I do last Monday?", reference_clock=clock,
                                   timezone_name="America/Denver")
        self.assertEqual(monday["day"], "2026-10-26")
        self.assertEqual(parse_time_intent("Where was it as of 2026-09-14?",
                         reference_clock=clock, timezone_name="UTC")["day"], "2026-09-14")
        self.assertEqual(parse_time_intent("Where is it now?", reference_clock=clock,
                         timezone_name="UTC")["kind"], "current")
        self.assertEqual(parse_time_intent("Where was it last spring?", reference_clock=clock,
                         timezone_name="UTC")["kind"], "ambiguous")

    def test_unreviewed_change_does_not_choose_between_conflicting_values(self):
        _, old, _ = self.assert_fact("review-old", "/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.assert_fact("review-new", "/data/B", validity={
            "from": "2026-09-14", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"},
            change={"relation": "supersedes", "assertion_id": old["assertion_id"],
                    "reviewed": False})
        result = self.select(as_of="2026-09-20T00:00:00Z")
        self.assertEqual(result["status"], "conflict")
        self.assertEqual({a["value"] for a in result["assertions"]}, {"/data/A", "/data/B"})

    def test_broad_selection_keeps_distinct_projects_and_predicates_separate(self):
        self.assert_fact("project-a-path", "/data/A", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        self.assert_fact("project-b-path", "/data/B", project="personal-b", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "explicit_source"})
        result = select_assertions(self.state.db, "personal-a",
            read_projects=["personal-a", "personal-b"],
            as_of="2026-09-20T00:00:00Z")
        self.assertEqual(result["status"], "multiple")
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual({group["status"] for group in result["groups"]}, {"supported"})

    def test_temporal_registration_rolls_back_with_outer_transaction(self):
        document, source = self.add("transaction-path")
        db = self.state.db
        try:
            db.execute('BEGIN')
            record_assertion(db, revision_id=document["revision_id"],
                subject="dataset", predicate="location", value="/data/tmp",
                evidence_source_ids=[source], recorded_at=99)
            raise RuntimeError("force rollback")
        except RuntimeError:
            db.rollback()
        self.assertEqual(db.execute("SELECT count(*) FROM knowledge_temporal_assertions").fetchone()[0], 0)

    def test_excluded_source_cannot_be_used_to_answer_history(self):
        _, _, source = self.assert_fact("excluded-path", "/data/restricted",
            validity={"from": "2026-09-01", "to_status": "ongoing",
                      "precision": "day", "timezone": "UTC", "basis": "explicit_source"})
        db = self.state.db
        db.execute("INSERT INTO memory_exclusions VALUES(?,?,?,?)",
                   (source, source, "synthetic exclusion", 1))
        self.assertEqual(self.select(as_of="2026-09-20T00:00:00Z")["status"], "absent")
        other, other_source = self.add("new-exclusion")
        db.execute("INSERT INTO memory_exclusions VALUES(?,?,?,?)",
                   (other_source, other_source, "synthetic exclusion", 1))
        with self.assertRaisesRegex(ValueError, "temporal_evidence_unavailable"):
            record_assertion(db, revision_id=other["revision_id"],
                subject="dataset", predicate="location", value="/data/restricted",
                evidence_source_ids=[other_source])

    def test_inferred_time_is_qualified_as_uncertain(self):
        self.assert_fact("inferred-path", "/data/guess", validity={
            "from": "2026-09-01", "to_status": "ongoing", "precision": "day",
            "timezone": "UTC", "basis": "inferred"})
        result = self.select(as_of="2026-09-20T00:00:00Z")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["unknown_time"][0]["valid_basis"], "inferred")

    def test_explicit_time_requires_date_in_original_evidence(self):
        document, source = self.add("unsupported-date")
        with self.assertRaisesRegex(ValueError, "temporal_time_not_in_source"):
            record_assertion(self.state.db, revision_id=document["revision_id"],
                subject="dataset", predicate="location", value="/data/A",
                event={"at": "2026-09-21", "precision": "day", "timezone": None,
                       "basis": "explicit_source"}, evidence_source_ids=[source])

    def test_curated_observed_location_admits_event_but_attempted_move_does_not(self):
        self.curated_location("observed-save", "/data/A")
        self.curated_location("attempted-move", "/data/B", state="attempted",
                              occurred_date="2026-09-22")
        known = select_assertions(self.state.db, "personal-a", subject="survey-data",
            predicate="location", actor="agent", event_day="2026-09-21",
            timezone_name="UTC")
        attempted = select_assertions(self.state.db, "personal-a", subject="survey-data",
            predicate="location", actor="agent", event_day="2026-09-22",
            timezone_name="UTC")
        self.assertEqual(known["assertions"][0]["value"], "/data/A")
        self.assertIsNone(known["assertions"][0]["event_timezone"])
        self.assertEqual(attempted["status"], "absent")
        self.assertEqual(self.state.db.execute(
            "SELECT count(*) FROM knowledge_temporal_assertions").fetchone()[0], 1)

    def test_curated_correction_and_source_withdrawal_do_not_restore_false_location(self):
        first, _ = self.curated_location("wrong-save", "/data/wrong")
        self.curated_location("correct-save", "/data/right", operation="CORRECT",
                              target=first["document_id"])
        old_day = select_assertions(self.state.db, "personal-a", subject="survey-data",
            predicate="location", actor="agent", event_day="2026-09-21")
        self.assertEqual([a["value"] for a in old_day["assertions"]], ["/data/right"])
        invalidate_source(self.state,"source-correct-save")
        after = select_assertions(self.state.db, "personal-a", subject="survey-data",
            predicate="location", actor="agent", event_day="2026-09-21")
        self.assertEqual(after["status"], "absent")

    def test_curated_automatic_adapter_keeps_projects_and_unknown_dates_distinct(self):
        self.curated_location("project-a-save", "/data/A", occurred_date="",
                              state="reported")
        self.curated_location("project-b-save", "/data/B", project="personal-b",
                              occurred_date="", state="reported")
        result = select_assertions(self.state.db, "personal-a",
            read_projects=["personal-a", "personal-b"], subject="survey-data",
            predicate="location", actor="agent")
        self.assertEqual(result["status"], "multiple")
        self.assertEqual({x["project"] for x in result["unknown_time"]},
                         {"personal-a", "personal-b"})
        self.assertEqual(select_assertions(self.state.db, "personal-a",
            subject="survey-data", predicate="location", actor="agent",
            event_day="2026-09-25")["status"], "absent")

    def test_successful_tool_move_updates_latest_known_location_with_qualified_time(self):
        first_time = datetime(2026, 9, 21, 10, tzinfo=timezone.utc).timestamp()
        second_time = datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()
        self.curated_location("first-save", "/data/A", occurred_date="",
                              source_created=first_time)
        self.curated_location("observed-move", "/data/B", occurred_date="",
                              source_created=second_time, move_from="/data/A")
        result = select_assertions(self.state.db, "personal-a", subject="survey-data",
            predicate="location", actor="agent",
            now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        between = select_assertions(self.state.db, "personal-a", subject="survey-data",
            predicate="location", actor="agent",
            as_of="2026-09-21T11:00:00Z")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual([x["value"] for x in result["unknown_time"]], ["/data/B"])
        self.assertEqual(between["status"], "uncertain")
        self.assertEqual([x["value"] for x in between["unknown_time"]], ["/data/A"])
        self.assertEqual(result["unknown_time"][0]["event_basis"], "source_event_time")
        self.assertEqual(result["unknown_time"][0]["valid_to_status"], "unknown")

    def test_observed_non_artifact_activity_uses_verified_tool_event_time(self):
        stamp = datetime(2026, 9, 21, 18, tzinfo=timezone.utc).timestamp()
        source = "source-completed-report"
        observation = {"title": "Completed report", "lesson": "Completed the survey report.",
            "knowledge_type": "fact", "subjects": ["survey report"],
            "domain": "private_memory", "evidence": [{"source_id": source}],
            "memory_context": {"policy": "durable-memory-8", "subject": "survey report",
                "facets": ["activity"], "actors": ["agent"], "state": "observed",
                "attribution": "execution_result", "event_id": source,
                "occurred_date": "", "source_created": stamp,
                "time_basis": "source_event_time"}}
        db = self.state.db
        with db:
            db.execute("""INSERT INTO memories(id,session,project,body,kind,created,exit_code)
                VALUES(?,?,?,?,?,?,?)""",
                (source, "source-session", "personal-a", "completed the survey report",
                 "PostToolUse", stamp, 0))
            db.execute("""INSERT INTO memories(id,session,project,body,kind,created)
                VALUES(?,?,?,?,?,?)""",
                ("activity", "curation", "personal-a", json.dumps(observation),
                 "Observation", stamp))
            ingest_observation(db, "activity", "personal-a", "curation", observation,
                               force_new=True)
        result = select_assertions(db, "personal-a", subject="survey report",
            predicate="activity", actor="agent", event_day="2026-09-21")
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["assertions"][0]["event_basis"], "source_event_time")


if __name__ == "__main__":
    unittest.main()
