"""H1 oracle and seal-blind guard checks, using no provider or installed service."""

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest


VALIDATOR = Path(__file__).parents[1] / "tools/validate_history_retrieval_fixtures.py"
SPEC = importlib.util.spec_from_file_location("validate_history_retrieval_fixtures", VALIDATOR)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class HistoryFixtures(unittest.TestCase):
    def test_public_oracle_is_grounded_and_seal_blind(self):
        result = validator.validate_public()
        self.assertTrue(result["pass"])
        self.assertFalse(result["sealed_questions_read"])
        self.assertEqual((result["families"], result["questions"], result["positive"]), (12, 48, 36))

    def test_hash_guard_rejects_toy_tampering(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            for filename in ("development.json", "manifest.json"):
                (folder / filename).write_bytes((validator.FIXTURES / filename).read_bytes())
            value = json.loads((folder / "development.json").read_text())
            value["sources"][0]["text"] += " toy mutation"
            (folder / "development.json").write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "fixture_changed"):
                validator.validate_public(folder)

    def test_oracle_rejects_rehashed_toy_span_corruption(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            value = json.loads((validator.FIXTURES / "development.json").read_text())
            value["oracle_events"][0]["source_refs"][0]["quote"] = "invented quote"
            raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            (folder / "development.json").write_bytes(raw)
            manifest = json.loads((validator.FIXTURES / "manifest.json").read_text())
            manifest["development_sha256"] = hashlib.sha256(raw).hexdigest()
            (folder / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "event_span"):
                validator.validate_public(folder)

    def test_independent_scoring_layers_and_required_hard_cases(self):
        value = json.loads((validator.FIXTURES / "development.json").read_text())
        self.assertEqual(set(value["scoring_layers"]), {"source_to_curated", "fixed_curated_retrieval", "end_to_end"})
        self.assertEqual(len(value["invariants"]), 4)
        self.assertIn("dev-long_pages-q1", {x.get("question_id") for x in value["invariants"]})
        self.assertEqual(len([q for q in value["questions"] if q["expected"]["abstention_reason"]]), 12)


if __name__ == "__main__":
    unittest.main()
