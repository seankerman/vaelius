"""Labels require real source evidence, valid identities and whole-scenario splits."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("retrieval_fixture_builder", ROOT / "tools/build_retrieval_experiment_fixtures.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class RetrievalExperimentFixtures(unittest.TestCase):
    def setUp(self):
        self.corpus = builder.read(builder.DEFAULT_FIXTURES / "corpus.json")
        self.development = builder.read(builder.DEFAULT_FIXTURES / "development.json")

    def assertInvalid(self, reason, change):
        corpus, development = copy.deepcopy(self.corpus), copy.deepcopy(self.development)
        change(corpus, development)
        with self.assertRaisesRegex(ValueError, reason):
            builder.validate(corpus, development)

    def test_frozen_development_has_grounded_facets_and_balanced_families(self):
        summary = builder.validate(self.corpus, self.development)
        self.assertEqual(summary["sources"], 240)
        self.assertEqual(summary["scenario_groups"], 24)
        self.assertEqual(summary["source_kinds"], {"native_document": 220, "curated_memory": 20})
        self.assertEqual(summary["splits"]["development"]["positive"], 55)
        self.assertEqual(summary["splits"]["development"]["negative"], 17)
        self.assertEqual(len({s["original_sha256"] for s in self.corpus["sources"]}), 240)

    def test_absent_evidence_is_rejected(self):
        self.assertInvalid("fixture_absent_evidence", lambda c, d: d["questions"][0]["expected"].update(source_ids=["missing"], evidence_sets=[["missing"]]))

    def test_invalid_requester_is_rejected(self):
        self.assertInvalid("fixture_invalid_principal", lambda c, d: d["questions"][0].update(principal="not-enrolled"))

    def test_whole_scenario_must_stay_in_one_split(self):
        self.assertInvalid("fixture_scenario_split_leakage", lambda c, d: c["sources"][1].update(split="confirmation"))

    def test_positive_without_all_required_facets_is_rejected(self):
        self.assertInvalid("fixture_answerable_without_facets", lambda c, d: d["questions"][0]["expected"].update(facets=[]))
        self.assertInvalid("fixture_missing_required_facet", lambda c, d: d["questions"][0]["expected"]["facets"][0].update(source_ids=[]))

    def test_invented_quote_and_wrong_revision_are_rejected(self):
        self.assertInvalid("fixture_unsubstantiated_facet", lambda c, d: d["questions"][0]["expected"]["facets"][0].update(quote="invented evidence"))
        self.assertInvalid("fixture_wrong_revision", lambda c, d: d["questions"][0]["expected"]["facets"][0].update(revision="999"))

    def test_private_other_owner_positive_is_rejected(self):
        index = next(i for i, q in enumerate(self.development["questions"]) if q["family"] == "user_preferences" and q["principal"] == "bob")
        self.assertInvalid("fixture_unreadable_positive", lambda c, d: d["questions"][index].update(principal="alice"))

    def test_private_visibility_cannot_grant_other_readers(self):
        index = next(i for i, s in enumerate(self.corpus["sources"]) if s["visibility"] == "private")
        self.assertInvalid("fixture_private_owner_scope", lambda c, d: c["sources"][index].update(reader_ids=["alice", "bob"]))

    def test_old_current_version_and_incorrect_original_are_rejected(self):
        index = next(i for i, q in enumerate(self.development["questions"]) if q["as_of"])
        self.assertInvalid("fixture_stale_positive", lambda c, d: d["questions"][index].update(as_of=None))
        index = next(i for i, q in enumerate(self.development["questions"]) if q["expected"]["response_type"] == "original")
        self.assertInvalid("fixture_original_label", lambda c, d: d["questions"][index]["expected"].update(original_sha256="0" * 64))

    def test_curated_records_pass_the_existing_canonical_validator(self):
        from agenthub.processing.durable_memory import packet_for, validate_records, observation_for
        curated = [s for s in self.corpus["sources"] if s["kind"] == "curated_memory"]
        for source in curated:
            with self.subTest(source=source["id"]):
                rows = [{"id": source["id"] + "-" + kind, "project": "enterprise:synthetic-retrieval",
                    "session": source["scenario"], "turn": "1", "kind": kind,
                    "body": source["text"] if kind == "UserPromptSubmit" else "Recorded the synthetic report.",
                    "created": 1790438400.0, "occurred_at": source["metadata"]["occurred_at"]}
                    for kind in ("UserPromptSubmit", "Stop")]
                packet = packet_for(rows)
                event = next(e for e in packet["episode"]["events"] if e["kind"] == "UserPromptSubmit")
                record = copy.deepcopy(source["curation"]["record"])
                record.update(event_id=event["event_id"], evidence_span_ids=[s["span_id"] for s in event["spans"]])
                checked = validate_records({"records": [record]}, packet, {e["event_id"] for e in packet["episode"]["events"]})
                self.assertEqual(checked["rejections"], [])
                self.assertEqual(len(checked["records"]), 1)
                claim = observation_for(checked["records"][0], packet)
                self.assertTrue(claim["evidence"])
                self.assertEqual(claim["lesson"], source["curation"]["record"]["text"])

    def test_manifest_records_real_sha_and_refuses_conflicting_freeze(self):
        # Use deliberately broken confirmation metadata to prove validation
        # happens before a manifest is reserved. This never reads sealed labels.
        with tempfile.TemporaryDirectory() as tmp:
            private, output = Path(tmp) / "confirmation.json", Path(tmp) / "manifest.json"
            private.write_text(json.dumps({"version": "wrong"}))
            with self.assertRaisesRegex(ValueError, "fixture_suite_version"):
                builder.freeze(builder.DEFAULT_FIXTURES, private, output)
            self.assertFalse(output.exists())

    def test_conflicting_manifest_cannot_overwrite_an_existing_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            private, output = Path(tmp) / "confirmation.json", Path(tmp) / "manifest.json"
            private.write_text("{}")
            # Stub only suite validation for a manifest writer unit test; this
            # cannot stand in for confirmation fixture/evaluation evidence.
            with patch.object(builder, "validate", return_value={"unit_test": True}):
                manifest = builder.freeze(builder.DEFAULT_FIXTURES, private, output)
                self.assertEqual(manifest["corpus_sha256"], builder.sha256(builder.DEFAULT_FIXTURES / "corpus.json"))
                original = output.read_bytes()
                private.write_text('{"changed": true}')
                with self.assertRaisesRegex(ValueError, "fixture_frozen_manifest_conflict"):
                    builder.freeze(builder.DEFAULT_FIXTURES, private, output)
                self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
