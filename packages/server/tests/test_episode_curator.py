import hashlib
import json
from pathlib import Path
import unittest

from agenthub.processing.episode_curator import (
    EpisodeError, prepare_episode, prepare_episode_stages, resolution_schema, validate_resolution,
)


def load_fixture(corpus, manifest):
    metadata=json.loads(manifest.read_text())
    if hashlib.sha256(corpus.read_bytes()).hexdigest()!=metadata['corpus_sha256']:
        raise ValueError('fixture_hash_mismatch')
    return json.loads(corpus.read_text()),metadata

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "processing"


class EpisodeCuratorTests(unittest.TestCase):
    def setUp(self):
        self.corpus, self.manifest = load_fixture(
            FIXTURES / "episode_curation_v3.json",
            FIXTURES / "episode_curation_v3_manifest.json",
        )

    def case(self, case_id):
        return next(case for case in self.corpus["cases"] if case["case_id"] == case_id)


    def test_fixture_is_frozen_before_live_evaluation(self):
        raw = (FIXTURES / "episode_curation_v3.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.manifest["corpus_sha256"])
        self.assertEqual(self.manifest["development_cases"], 12)
        self.assertEqual(self.manifest["confirmation_cases"], 6)
        self.assertTrue(self.manifest["confirmation_created_before_live_evaluation"])
        self.assertTrue(self.manifest["development_is_tuning_material"])
        self.assertTrue(self.manifest["authored_cases_are_not_blind_holdout"])
        v1 = json.loads((FIXTURES / "episode_curation_v1_manifest.json").read_text())
        self.assertEqual(self.manifest["split_sha256"]["confirmation"],
                         v1["split_sha256"]["confirmation"])

    def test_episode_is_one_turn_and_transport_duplicates_are_not_model_input(self):
        case = self.case("ep-dev-context-transfer")
        payload = prepare_episode(case["episode"])
        self.assertEqual(payload["episode"]["turn"], "turn-1")
        self.assertEqual([event["kind"] for event in payload["episode"]["events"]],
                         ["UserPromptSubmit", "Stop"])
        self.assertEqual({row["reason"] for row in payload["deterministic_skip_receipts"]},
                         {"context_transfer", "duplicate_transport"})

    def test_oversize_episode_fails_instead_of_silently_curating_a_fragment(self):
        case = self.case("ep-dev-verified-fix")
        with self.assertRaisesRegex(EpisodeError, "episode_requires_staged_curation"):
            prepare_episode(case["episode"], max_chars=20)

    def test_large_episode_stages_keep_one_episode_and_repeat_only_semantic_anchors(self):
        case = json.loads(json.dumps(self.case("ep-dev-verified-fix")))
        for index in range(5):
            base = dict(case["episode"][2])
            base["id"] = f"extra-{index}"
            base["created"] = 2.1 + index / 10
            base["body"] = (f"tool evidence {index} " + "x" * 160)
            case["episode"].insert(-1, base)
        packets, staged = prepare_episode_stages(case["episode"], max_events=5, max_chars=900)
        self.assertTrue(staged)
        self.assertGreater(len(packets), 1)
        self.assertEqual({packet["episode"]["episode_id"] for packet in packets},
                         {packets[0]["episode"]["episode_id"]})
        for packet in packets:
            kinds = [event["kind"] for event in packet["episode"]["events"]]
            self.assertIn("UserPromptSubmit", kinds)
            self.assertIn("Stop", kinds)
            self.assertLessEqual(len(packet["episode"]["events"]), 5)



    def test_mixed_turns_are_rejected(self):
        case = json.loads(json.dumps(self.case("ep-dev-verified-fix")))
        case["episode"][-1]["turn"] = "turn-2"
        with self.assertRaisesRegex(EpisodeError, "mixed_episode_identity"):
            prepare_episode(case["episode"])


    def test_resolution_must_target_a_supplied_artifact(self):
        case = self.case("ep-dev-correction")
        payload = prepare_episode(case["episode"], case["related_artifacts"])
        candidate = {"candidate_key": "timeout-current"}
        schema = resolution_schema(candidate, payload)
        self.assertEqual(schema["properties"]["target_artifact_id"]["enum"],
                         ["", "art-timeout-old"])
        valid = {"candidate_key": "timeout-current", "operation": "CORRECT",
                 "target_artifact_id": "art-timeout-old",
                 "reason": "same_subject_newer_correction"}
        self.assertEqual(validate_resolution(valid, candidate, payload), valid)
        valid["target_artifact_id"] = "not-supplied"
        with self.assertRaisesRegex(EpisodeError, "resolution_target"):
            validate_resolution(valid, candidate, payload)



if __name__ == "__main__":
    unittest.main()
