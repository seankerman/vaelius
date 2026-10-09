import copy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from vaelius_test_support.fixtures.memory import _fixture
from agenthub.processing.durable_memory import packet_for, validate, intent, schema
from agenthub.processing.episode_curator import EpisodeError

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/processing/durable_memory_v1.json"


def record(packet, *, event, refs=None, **changes):
    ids = refs or [event]
    value = {"title": "Juniper dataset saved", "text": "Exported the Juniper dataset.",
        "subject": "Juniper", "facets": ["activity", "artifact"], "actors": ["agent"],
        "artifact_name": "Juniper", "location": "datasets/juniper-v1.csv",
        "reason_actor": "", "reason_quote": "", "state": "observed", "event_id": event,
        "occurred_date": "", "evidence_span_ids": [s["span_id"]
            for e in packet["episode"]["events"] if e["event_id"] in ids for s in e["spans"]]}
    value.update(changes)
    return value


class DurableMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cases, self.manifest = _fixture(FIXTURE)

    def tearDown(self):
        self.temp.cleanup()



    def test_fixture_precedes_behavior_and_last_monday_timezone(self):
        self.assertEqual(len(self.cases), self.manifest["case_count"])
        plan = intent("What did I work on last Monday?",
            now=datetime.fromisoformat("2026-09-22T01:00:00+00:00"), zone="America/Denver")
        # Still Monday locally: 'last Monday' means the preceding Monday.
        self.assertEqual(plan["day"], "2026-09-14")
        self.assertTrue(intent("What did I work on a while ago?")["unsupported_time"])




    def test_successful_location_reanchors_only_to_unique_cited_current_execution(self):
        sources = self.cases[0]["turns"][0]; packet = packet_for(sources)
        value = record(packet, event="artifact-s1", refs=["artifact-s1", "artifact-t1"])
        result = validate({"records": [value]}, packet, {s["id"] for s in sources})
        actual = result["records"][0]
        self.assertEqual(actual["event_id"], "artifact-t1")
        self.assertEqual(actual["source_created"], sources[1]["created"])
        self.assertEqual(actual["anchor_repair"]["from"], "artifact-s1")
        self.assertEqual(actual["evidence_span_ids"], value["evidence_span_ids"])
        self.assertEqual(value["event_id"], "artifact-s1")
        for change in ("failed", "ambiguous", "earlier_turn", "uncited", "unrelated_location"):
            altered = copy.deepcopy(packet); item = dict(value)
            current = {s["id"] for s in sources}
            if change == "failed": altered["episode"]["events"][1]["exit_code"] = 1
            if change == "ambiguous":
                other = copy.deepcopy(altered["episode"]["events"][1]); other["event_id"] = "second-success"
                for s in other["spans"]: s["span_id"] += "-second"
                altered["episode"]["events"].append(other); current.add(other["event_id"])
                item["evidence_span_ids"] = item["evidence_span_ids"] + [s["span_id"] for s in other["spans"]]
            if change == "earlier_turn": current.remove("artifact-t1")
            if change == "uncited": item["evidence_span_ids"] = [packet["episode"]["events"][2]["spans"][0]["span_id"]]
            if change == "unrelated_location":
                altered["episode"]["events"][1]["spans"][0]["text"] = "Saved Juniper to other.csv"
            with self.subTest(change=change), self.assertRaises(EpisodeError):
                validate({"records": [item]}, altered, current)

    def test_response_schema_stable_but_unknown_evidence_is_still_rejected(self):
        first = packet_for(self.cases[0]["turns"][0]); second = packet_for(self.cases[0]["turns"][1])
        self.assertEqual(schema(first), schema(second))
        item = record(first, event="artifact-t1", evidence_span_ids=["fabricated-reference"])
        with self.assertRaisesRegex(EpisodeError, "memory_evidence"):
            validate({"records": [item]}, first, {"artifact-t1"})



    def test_validation_prevents_fabricated_paths_stale_context_and_failed_success(self):
        sources = self.cases[0]["turns"][0]; packet = packet_for(sources)
        value = record(packet, event="artifact-t1")
        for replacement, error in (({"location": "invented/juniper.csv"}, "memory_location_evidence"),
                                   ({"event_id": "artifact-u1", "evidence_span_ids": [packet["episode"]["events"][0]["spans"][0]["span_id"]]}, "memory_location_evidence")):
            with self.assertRaisesRegex(EpisodeError, error):
                validate({"records": [dict(value, **replacement)]}, packet, {s["id"] for s in sources})
        with self.assertRaisesRegex(EpisodeError, "memory_current_evidence"):
            validate({"records": [value]}, packet, {"different-turn"})
        failed = copy.deepcopy(packet)
        failed["episode"]["events"][1]["exit_code"] = 1
        with self.assertRaisesRegex(EpisodeError, "memory_unverified_outcome"):
            validate({"records": [value]}, failed, {s["id"] for s in sources})


    def test_observed_failure_is_attempted_and_empty_artifact_is_activity(self):
        sources = self.cases[0]["turns"][2]; packet = packet_for(sources)
        value = record(packet, event="artifact-t3", refs=["artifact-u3", "artifact-t3"],
            location="published/juniper.csv", text="Juniper move failed: permission denied.")
        result = validate({"records": [value]}, packet, {s["id"] for s in sources})
        self.assertEqual(result["records"][0]["state"], "attempted")
        value.update(location="", artifact_name="", state="attempted")
        result = validate({"records": [value]}, packet, {s["id"] for s in sources})
        self.assertEqual(result["records"][0]["facets"], ["activity"])









if __name__ == "__main__":
    unittest.main()
