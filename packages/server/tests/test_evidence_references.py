import copy
import json
import unittest
from unittest.mock import patch

from vaelius_test_support.fixtures.memory import _fixture, _combined
from agenthub.processing.durable_memory import packet_for, schema, validate_records
from agenthub.processing.evidence_references import EvidenceReferences
from test_durable_memory import FIXTURE, record
from test_memory_evidence_contract import wire


class EvidenceReferenceTests(unittest.TestCase):
    def setUp(self):
        cases, _ = _fixture(FIXTURE)
        self.sources = cases[0]["turns"][0]
        self.first = packet_for(cases[0]["turns"][0])
        self.second = packet_for(cases[0]["turns"][1])

    def test_resume_rebuild_order_independent_and_original_lineage(self):
        refs = EvidenceReferences(self.first)
        sent = refs.packet(self.first)
        event = sent["episode"]["events"][1]
        self.assertLessEqual(len(event["event_id"]), 10)
        self.assertTrue(all(len(s["span_id"]) <= 10 for s in event["spans"]))
        later = EvidenceReferences(_combined([self.first, self.second]),
                                   previous=json.loads(json.dumps(refs.manifest)))
        self.assertEqual(sent, later.packet(self.first))
        reverse = copy.deepcopy(self.first); reverse["episode"]["events"].reverse()
        self.assertEqual(refs.manifest, EvidenceReferences(reverse).manifest)
        raw = wire(record(sent, event=event["event_id"]))
        result = validate_records({"records": [raw]}, self.first, {"artifact-t1"}, references=refs)
        self.assertEqual(result["rejections"], [])
        self.assertEqual(result["records"][0]["event_id"], "artifact-t1")
        self.assertEqual(result["records"][0]["evidence_span_ids"],
                         [s["span_id"] for s in self.first["episode"]["events"][1]["spans"]])
        self.assertEqual(schema(self.first), schema(self.second))

    def test_unknown_other_task_collision_and_changed_manifest_fail_closed(self):
        refs = EvidenceReferences(self.first); sent = refs.packet(self.first)
        event = sent["episode"]["events"][1]
        raw = wire(record(sent, event=event["event_id"]))
        for altered in (dict(raw, event_id="eunknown"), dict(raw, evidence_span_ids=["sunknown"])):
            result = validate_records({"records": [altered]}, self.first, {"artifact-t1"}, references=refs)
            self.assertFalse(result["records"])
        other = copy.deepcopy(self.first); other["episode"]["session"] = "different-task"
        result = validate_records({"records": [raw]}, other, {"artifact-t1"},
                                  references=EvidenceReferences(other))
        self.assertFalse(result["records"])
        with patch("agenthub.processing.evidence_references._handle", return_value="collision"):
            with self.assertRaisesRegex(ValueError, "reference_collision"):
                EvidenceReferences(self.first)
        changed = copy.deepcopy(self.first); changed["episode"]["events"][0]["spans"][0]["text"] += "changed"
        with self.assertRaisesRegex(ValueError, "reference_manifest_changed"):
            EvidenceReferences(changed, previous=refs.manifest)

    def test_redundant_event_handle_in_span_list_is_dropped_but_unknown_span_is_denied(self):
        refs = EvidenceReferences(self.first); sent = refs.packet(self.first)
        event = sent["episode"]["events"][1]
        raw = wire(record(sent, event=event["event_id"]))
        raw["evidence_span_ids"].append(event["event_id"])
        checked = validate_records({"records": [raw]}, self.first, {"artifact-t1"}, references=refs)
        self.assertEqual(len(checked["records"]), 1)
        self.assertEqual(checked["rejections"], [])
        self.assertEqual(checked["reference_repairs"], [{"record_index": 0,
            "kind": "redundant_event_handle_in_span_list", "removed": 1}])
        raw["evidence_span_ids"].append("sunknown")
        denied = validate_records({"records": [raw]}, self.first, {"artifact-t1"}, references=refs)
        self.assertFalse(denied["records"])

    def test_unique_cited_current_event_can_reanchor_a_wrong_event_handle(self):
        refs = EvidenceReferences(self.first); sent = refs.packet(self.first)
        execution = sent["episode"]["events"][1]
        raw = wire(record(sent, event=execution["event_id"]))
        raw["event_id"] = sent["episode"]["events"][0]["event_id"]
        checked = validate_records({"records": [raw]}, self.first, {"artifact-t1"}, references=refs)
        self.assertEqual(len(checked["records"]), 1)
        self.assertEqual(checked["records"][0]["event_id"], "artifact-t1")
        self.assertEqual(checked["reference_repairs"], [{"record_index": 0,
            "kind": "unique_cited_current_event_reanchor"}])
        raw["evidence_span_ids"].extend(sent["episode"]["events"][0]["spans"][0]["span_id"] for _ in range(1))
        ambiguous = validate_records({"records": [raw]}, self.first, {"artifact-t1"}, references=refs)
        self.assertFalse(ambiguous["records"])

    def test_missing_tool_identity_is_explicit_never_inferred_from_body(self):
        packet = copy.deepcopy(self.first)
        event = packet["episode"]["events"][1]; event["tool_name"] = ""
        sent = EvidenceReferences(packet).packet(packet)
        self.assertFalse(sent["episode"]["events"][1]["observed_allowed"])
        self.assertEqual(sent["episode"]["events"][1]["verification_gap"], "tool_identity_missing")
        self.assertTrue(EvidenceReferences(self.first).packet(self.first)["episode"]["events"][1]["observed_allowed"])

    def test_all_structural_event_pointers_use_the_same_short_namespace(self):
        refs = EvidenceReferences(self.first); sent = refs.packet(self.first)
        for field in ("objective_event_ids", "final_event_ids"):
            self.assertTrue(all(ident in refs.events for ident in sent["episode"][field]))
        self.assertNotIn('"artifact-u1"', json.dumps(sent))
        self.assertNotIn('"artifact-s1"', json.dumps(sent))
