"""Synthetic regressions fixed before any new private-task evaluation."""
import copy
import unittest

from agenthub.processing.durable_memory import schema, validate, validate_records
from agenthub.processing.episode_curator import EpisodeError


def packet():
    # Exact slices: the first already ends in a space. Citation order is arbitrary.
    return {"episode": {"events": [{"event_id": "user", "kind": "UserPromptSubmit",
        "source_created": 1790006400, "spans": [
            {"span_id": "a", "start": 0, "end": 18, "text": "Mira chose SQLite "},
            {"span_id": "b", "start": 18, "end": 37, "text": "because it is local"},
            {"span_id": "c", "start": 37, "end": 52, "text": ". Keep private."}]}]}}


def normalized(**changes):
    value = {"title": "Mira chose SQLite", "text": "Mira reports choosing SQLite because it is local.",
        "subject": "SQLite", "facets": ["decision"], "actors": ["Mira"],
        "artifact_name": "", "location": "", "reason_actor": "Mira",
        "reason_quote": "Mira chose SQLite because it is local", "state": "reported",
        "event_id": "user", "occurred_date": "", "evidence_span_ids": ["b", "a", "a"]}
    value.update(changes)
    return value


def wire(value):
    value = copy.deepcopy(value)
    name, location = value.pop("artifact_name"), value.pop("location")
    actor, quote = value.pop("reason_actor"), value.pop("reason_quote")
    value["artifact"] = {"name": name, "location": location} if name or location else None
    value["rationale"] = {"actor": actor, "quote": quote} if actor or quote else None
    value["facets"] = [f for f in value["facets"] if f != "artifact"] or ["activity"]
    return value


class MemoryEvidenceContractTests(unittest.TestCase):
    def test_adjacent_exact_slices_support_quote_in_any_citation_order(self):
        result = validate({"records": [normalized()]}, packet(), {"user"})
        self.assertEqual(result["records"][0]["reason_quote"], normalized()["reason_quote"])

    def test_no_synthetic_quote_across_gap_or_different_event(self):
        for change in ("gap", "different_event", "inserted_space"):
            source = packet(); value = normalized()
            if change == "gap":
                source["episode"]["events"][0]["spans"][1].update(start=19, end=38)
            elif change == "different_event":
                second = copy.deepcopy(source["episode"]["events"][0])
                second["event_id"] = "other"; second["spans"] = [second["spans"][1]]
                source["episode"]["events"][0]["spans"] = source["episode"]["events"][0]["spans"][:1]
                source["episode"]["events"].append(second)
            else:
                value["reason_quote"] = "Mira chose SQLite  because it is local"
            with self.subTest(change=change), self.assertRaisesRegex(EpisodeError, "memory_reason_evidence"):
                validate({"records": [value]}, source, {"user"})

    def test_grouped_optional_fields_normalize_without_redundant_bookkeeping(self):
        value = wire(normalized(reason_quote="because it is local", actors=[]))
        result = validate_records({"records": [value]}, packet(), {"user"})
        self.assertEqual(result["rejections"], [])
        self.assertEqual(result["records"][0]["actors"], ["Mira"])
        self.assertEqual(result["records"][0]["location"], "")
        source = packet()
        source["episode"]["events"][0]["spans"] = [{"span_id": "file", "start": 0, "end": 35,
            "text": "Mira saved Juniper to data/batch.csv."}]
        value.update(rationale=None, artifact={"name": "Juniper", "location": "data/batch.csv"},
                     facets=["activity"], evidence_span_ids=["file"])
        result = validate_records({"records": [value]}, source, {"user"})
        self.assertEqual(result["rejections"], [])
        self.assertEqual(result["records"][0]["facets"], ["activity", "artifact"])
        self.assertEqual(result["records"][0]["location"], "data/batch.csv")

    def test_grouped_fields_remain_strict_and_grounded(self):
        for change in ({"rationale": {"actor": "Other", "quote": "because it is local"}},
                       {"rationale": {"actor": "Mira", "quote": "invented motive"}},
                       {"artifact": {"name": "dataset", "location": "made-up.csv"}},
                       {"artifact": {"name": "dataset"}}, {"artifact": []},
                       {"rationale": {"actor": "Mira", "quote": ""}},
                       {"facets": ["artifact"]}, {"location": "legacy-field"}):
            value = wire(normalized(reason_quote="because it is local")); value.update(change)
            with self.subTest(change=change):
                result = validate_records({"records": [value]}, packet(), {"user"})
                self.assertEqual(result["records"], [])
                self.assertEqual(len(result["rejections"]), 1)

    def test_schema_has_no_turn_dependent_or_duplicate_fields(self):
        self.assertEqual(schema(packet()), schema({"episode": {"events": []}}))
        fields = schema()["properties"]["records"]["items"]["properties"]
        self.assertIn("artifact", fields); self.assertIn("rationale", fields)
        self.assertNotIn("location", fields); self.assertNotIn("reason_actor", fields)
        self.assertNotIn("artifact", fields["facets"]["items"]["enum"])

    def test_execution_anchor_uses_contiguous_location_evidence(self):
        first = "Saved Juniper to datasets/"; second = "juniper.csv"
        source = {"episode": {"events": [
            {"event_id": "exec", "kind": "PostToolUse", "tool_name": "exec_command",
             "exit_code": 0, "source_created": 1790006400, "spans": [
                 {"span_id": "a", "start": 0, "end": len(first), "text": first},
                 {"span_id": "b", "start": len(first), "end": len(first + second), "text": second}]},
            {"event_id": "stop", "kind": "Stop", "source_created": 1790006401, "spans": [
                {"span_id": "c", "start": 0, "end": 6, "text": "Saved."}]}]}}
        value = normalized(title="Juniper saved", text="Saved Juniper to datasets/juniper.csv.",
            subject="Juniper", actors=["agent"], reason_actor="", reason_quote="",
            facets=["activity", "artifact"], artifact_name="Juniper", location="datasets/juniper.csv",
            state="observed", event_id="stop", evidence_span_ids=["b", "c", "a"])
        result = validate_records({"records": [wire(value)]}, source, {"exec", "stop"})
        self.assertEqual(result["rejections"], [])
        self.assertEqual(result["records"][0]["event_id"], "exec")
        source["episode"]["events"][0]["spans"][1]["start"] += 1
        result = validate_records({"records": [wire(value)]}, source, {"exec", "stop"})
        self.assertEqual(result["records"], [])
