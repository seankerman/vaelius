"""Validate/freeze deliberately synthetic retrieval fixtures without model calls.

Confirmation questions and labels are supplied from a private path. They are
never printed, copied into repository assets, or synthesized from development
questions. This tool validates fixture labels; it does not evaluate retrieval.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

VERSION = "retrieval-experiments-v1"
FAMILIES = (
    "exact_discovery", "semantic_paraphrase", "artifact_location", "procedures",
    "decisions_rationale", "values_numbers", "multiple_sources", "time_changes",
    "speaker_ownership", "user_preferences", "long_documents_originals", "abstention",
)
DEFAULT_FIXTURES = Path(__file__).parent / "fixtures" / "retrieval_experiments_v1"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def _fail(reason):
    raise ValueError(reason)


def _timestamp(value):
    if value is None:
        return None
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None:
        _fail("fixture_time_requires_zone")
    return point


def validate(corpus, development, confirmation=None):
    """Fail closed on labels lacking current readable, correctly scoped evidence."""
    if corpus.get("version") != VERSION:
        _fail("fixture_version")
    tenants = {t["id"]: t for t in corpus["tenants"]}
    if len(tenants) != 2 or any(len(t["principals"]) < 3 or len(t["projects"]) < 2 for t in tenants.values()):
        _fail("fixture_identity_coverage")
    sources = corpus["sources"]
    if len(sources) != 240:
        _fail("fixture_source_count")
    by_id = {s["id"]: s for s in sources}
    if len(by_id) != len(sources):
        _fail("fixture_duplicate_source")
    scenario_splits = {}
    kind_counts = Counter()
    for source in sources:
        tenant = tenants.get(source["tenant"])
        if not tenant or source["owner"] not in tenant["principals"] or source["project"] not in tenant["projects"]:
            _fail("fixture_invalid_source_identity")
        if source["split"] not in {"development", "confirmation"}:
            _fail("fixture_source_split")
        existing = scenario_splits.setdefault(source["scenario"], source["split"])
        if existing != source["split"]:
            _fail("fixture_scenario_split_leakage")
        readers = source["reader_ids"]
        if not readers or set(readers) - set(tenant["principals"]):
            _fail("fixture_invalid_reader")
        if source.get("visibility") not in {"private", "team", "organization"}:
            _fail("fixture_invalid_visibility")
        if source["visibility"] == "private" and readers != [source["owner"]]:
            _fail("fixture_private_owner_scope")
        if source["kind"] not in {"native_document", "curated_memory"}:
            _fail("fixture_source_kind")
        if not source["text"].strip() or hashlib.sha256(source["text"].encode()).hexdigest() != source["original_sha256"]:
            _fail("fixture_original_hash")
        if source["metadata"]["lifecycle"] not in {"active", "superseded", "withdrawn"}:
            _fail("fixture_lifecycle")
        _timestamp(source["metadata"]["occurred_at"])
        _timestamp(source["metadata"].get("valid_from"))
        _timestamp(source["metadata"].get("valid_to"))
        if source["kind"] == "curated_memory":
            curation = source.get("curation", {})
            record = curation.get("record", {})
            for quote in curation.get("evidence", []):
                if source["text"][quote["start"]:quote["end"]] != quote["quote"]:
                    _fail("fixture_curated_evidence")
            if not curation.get("evidence") or not record.get("text") or not record.get("facets"):
                _fail("fixture_curated_record")
            reason = record.get("rationale")
            if reason and reason["quote"] not in source["text"]:
                _fail("fixture_curated_reason")
        kind_counts[source["kind"]] += 1
    split_counts = {}
    for name, suite in (("development", development), ("confirmation", confirmation)):
        if suite is None:
            continue
        if suite.get("version") != VERSION or suite.get("split") != name:
            _fail("fixture_suite_version")
        questions = suite["questions"]
        if len(questions) != 72 or len({q["id"] for q in questions}) != 72:
            _fail("fixture_question_count")
        family_counts = Counter(q["family"] for q in questions)
        if family_counts != Counter({family: 6 for family in FAMILIES}):
            _fail("fixture_family_count")
        positives = negatives = 0
        for q in questions:
            tenant = tenants.get(q["tenant"])
            if not tenant or q["principal"] not in tenant["principals"] or q["project"] not in tenant["projects"]:
                _fail("fixture_invalid_principal")
            if q["split"] != name or scenario_splits.get(q["scenario"]) != name:
                _fail("fixture_question_split_leakage")
            _timestamp(q.get("as_of"))
            expected = q["expected"]
            facets = expected["facets"]
            evidence_sets = expected["evidence_sets"]
            expected_sources = expected["source_ids"]
            if not expected["answerable"]:
                negatives += 1
                if facets or expected_sources or evidence_sets or expected["response_type"] not in {"abstention", "clarification"}:
                    _fail("fixture_negative_label")
                continue
            positives += 1
            if not facets or not evidence_sets or not expected_sources:
                _fail("fixture_answerable_without_facets")
            if len({f["id"] for f in facets}) != len(facets):
                _fail("fixture_duplicate_facet")
            if any(not group or set(group) - set(expected_sources) for group in evidence_sets):
                _fail("fixture_invalid_evidence_set")
            for source_id in expected_sources:
                source = by_id.get(source_id)
                if not source:
                    _fail("fixture_absent_evidence")
                if source["scenario"] != q["scenario"] or source["split"] != name:
                    _fail("fixture_evidence_split_leakage")
                if source["tenant"] != q["tenant"] or source["project"] != q["project"]:
                    _fail("fixture_wrong_scope")
                if q["principal"] not in source["reader_ids"] and q["principal"] != source["owner"]:
                    _fail("fixture_unreadable_positive")
                meta = source["metadata"]
                if meta["lifecycle"] == "withdrawn":
                    _fail("fixture_withdrawn_positive")
                when = _timestamp(q.get("as_of"))
                start, end = _timestamp(meta.get("valid_from")), _timestamp(meta.get("valid_to"))
                if when is None and meta["lifecycle"] != "active":
                    _fail("fixture_stale_positive")
                if when and (start and when < start or end and when >= end):
                    _fail("fixture_wrong_time")
            for facet in facets:
                if not facet.get("value") or not facet.get("source_ids") or set(facet["source_ids"]) - set(expected_sources):
                    _fail("fixture_missing_required_facet")
                if facet["actor"] not in set(tenant["principals"]) | {"user", "source", "agent"}:
                    _fail("fixture_invalid_actor")
                if facet["type"] not in {"text", "path", "number", "procedure", "reason", "preference", "original", "identifier", "time"}:
                    _fail("fixture_facet_type")
                evidence_source = by_id[facet["source_ids"][0]]
                if evidence_source["text"][facet["start"]:facet["end"]] != facet["quote"] or facet["value"] not in facet["quote"]:
                    _fail("fixture_unsubstantiated_facet")
                if facet["revision"] != evidence_source["revision"]:
                    _fail("fixture_wrong_revision")
            if expected["response_type"] == "original":
                original = by_id.get(expected.get("original_source_id"))
                if not original or expected.get("original_sha256") != original["original_sha256"] or expected.get("original_version") != original["revision"]:
                    _fail("fixture_original_label")
        if (positives, negatives) != (55, 17):
            _fail("fixture_positive_negative_count")
        for family in FAMILIES:
            labels = [q["expected"]["answerable"] for q in questions if q["family"] == family]
            if sum(labels) != (0 if family == "abstention" else 5):
                _fail("fixture_family_balance")
        split_counts[name] = {"questions": 72, "positive": positives, "negative": negatives, "families": dict(family_counts)}
    return {"sources": len(sources), "scenario_groups": len(scenario_splits), "source_kinds": dict(kind_counts), "splits": split_counts}


def freeze(fixtures, sealed_questions, output):
    fixtures, sealed_questions, output = Path(fixtures), Path(sealed_questions), Path(output)
    if sealed_questions.resolve().is_relative_to(fixtures.resolve()):
        _fail("fixture_confirmation_must_be_private")
    corpus, development, confirmation = read(fixtures / "corpus.json"), read(fixtures / "development.json"), read(sealed_questions)
    summary = validate(corpus, development, confirmation)
    manifest = {
        "version": VERSION, "author": "separate fixture-author subagent /root/retrieval_fixtures",
        "classification": "deliberately synthetic source-grounded retrieval fixtures; no extraction or ordinary-use evidence",
        "independent_real_team_held_out": False, "confirmation_label_visibility": "private separate-author; not disclosed to retrieval owner",
        "corpus_sha256": sha256(fixtures / "corpus.json"), "development_sha256": sha256(fixtures / "development.json"),
        "adversarial_sha256": sha256(fixtures / "adversarial.json"),
        "confirmation_sha256": sha256(sealed_questions), "summary": summary,
        "source_provenance": "New fictional business/scientific workflows authored for this experiment; no V1-V6 source or query reuse",
    }
    if output.exists():
        prior = read(output)
        comparable = dict(prior); comparable.pop("frozen_utc", None)
        if comparable != manifest:
            _fail("fixture_frozen_manifest_conflict")
        return prior
    manifest["frozen_utc"] = datetime.now(timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True); handle.write("\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--sealed-questions", type=Path, required=True, help="Private confirmation JSON; content never printed")
    parser.add_argument("--output", type=Path, required=True, help="New immutable content-free manifest")
    args = parser.parse_args()
    print(json.dumps(freeze(args.fixtures, args.sealed_questions, args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
