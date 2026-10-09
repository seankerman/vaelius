"""Seal-blind H1 DEV scoring contract over deterministic toy observations."""

import copy
import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).parents[1] / "tools/evaluate_history_dev.py"
SPEC = importlib.util.spec_from_file_location("evaluate_history_dev", PATH)
grader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grader)


def observations():
    bank, sha = grader.load_public_bank()
    extraction = []
    for event in bank["oracle_events"]:
        if event["status"] == "transport_replay":
            continue
        extraction.append({**copy.deepcopy(event), "event_id": event["id"],
                           "occurrence_id": event["id"]})
    retrieval = []
    for q in bank["questions"]:
        expected = q["expected"]
        row = {"question_id": q["id"], "mode": q["mode"], "as_of": q["as_of"],
               "answerable": expected["answerable"],
               "event_ids": copy.deepcopy(expected["event_ids"]),
               "evidence_refs": copy.deepcopy(expected["evidence_refs"]),
               "abstention_reason": expected["abstention_reason"]}
        if q["id"] == "dev-long_pages-q1":
            row["pages"] = [{"event_ids": [event_id], "serialized_bytes": 220,
                             "has_more": i < 11,
                             "next_cursor": f"cursor-{i}" if i < 11 else None}
                            for i, event_id in enumerate(expected["event_ids"])]
        retrieval.append(row)
    return {"development_sha256": sha, "source_to_curated": extraction,
            "fixed_curated_retrieval": {"input_layer": "fixed_curated_records", "questions": retrieval}}


class HistoryDevEvaluatorTests(unittest.TestCase):
    def test_exact_denominators_and_unavailable_end_to_end(self):
        result = grader.evaluate(observations())
        self.assertFalse(result["confirmation_read"])
        self.assertEqual(result["source_to_curated"]["event_coverage"]["denominator"], 47)
        self.assertEqual(result["source_to_curated"]["event_coverage"]["numerator"], 47)
        self.assertEqual(result["fixed_curated_retrieval"]["positive_structural_complete"]["denominator"], 36)
        self.assertEqual(result["fixed_curated_retrieval"]["appropriate_abstention_or_denial"]["denominator"], 12)
        self.assertTrue(result["fixed_curated_retrieval"]["pagination_hard_gate"])
        self.assertEqual(result["end_to_end"]["status"], "unavailable")
        self.assertFalse(result["canonical_pg_execution_verified"])

    def test_missing_event_and_forbidden_delivery_fail_separately(self):
        value = observations()
        value["source_to_curated"].pop(0)
        question = next(q for q in value["fixed_curated_retrieval"]["questions"]
                        if q["question_id"] == "dev-identity_private-q4")
        question["event_ids"] = ["dev-identity_private-e2"]
        question["evidence_refs"] = [{"source_id": "dev-identity_private-s2"}]
        result = grader.evaluate(value)
        self.assertEqual(result["source_to_curated"]["event_coverage"]["numerator"], 46)
        self.assertFalse(result["source_to_curated"]["source_to_curated_pass"])
        self.assertEqual(result["fixed_curated_retrieval"]["forbidden_event_deliveries"], 1)
        self.assertFalse(result["fixed_curated_retrieval"]["fixed_retrieval_structural_pass"])

    def test_confirmation_payload_and_wrong_fixture_are_rejected(self):
        value = observations()
        value["confirmation"] = []
        with self.assertRaisesRegex(ValueError, "confirmation_not_admitted"):
            grader.evaluate(value)
        del value["confirmation"]
        value["development_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "development_identity_mismatch"):
            grader.evaluate(value)


if __name__ == "__main__":
    unittest.main()
