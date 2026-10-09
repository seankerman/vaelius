"""Seal-blind, deterministic H1 DEV grader for normalized pipeline observations.

This tool never dispatches a model or opens confirmation material. A runner must
execute the canonical PostgreSQL pipeline separately, then export observations
without passing oracle answers into that pipeline. See --help and fixture contract.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path

_VALIDATOR = Path(__file__).with_name("validate_history_retrieval_fixtures.py")
_SPEC = importlib.util.spec_from_file_location("validate_history_retrieval_fixtures", _VALIDATOR)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
FIXTURES, validate_public = _MODULE.FIXTURES, _MODULE.validate_public


FIELDS = ("actor_id", "speaker_id", "status", "truth", "effective_at",
          "time_precision", "recorded_at", "reason", "evidence_lineage_id",
          "policy_dependencies", "source_refs")


def _ref(ref):
    return tuple(ref.get(key) for key in ("source_id", "version", "start", "end", "quote"))


def _fraction(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def load_public_bank():
    report = validate_public()
    raw = (FIXTURES / "development.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != report["development_sha256"]:
        raise ValueError("fixture_changed")
    return json.loads(raw), report["development_sha256"]


def score_extraction(bank, observed):
    if observed is None:
        return {"status": "unavailable", "reason": "source_to_curated_observations_not_supplied"}
    if not isinstance(observed, list):
        raise ValueError("extraction_observations_shape")
    oracle = {e["id"]: e for e in bank["oracle_events"] if e["status"] != "transport_replay"}
    by_id = {}
    duplicate_ids = []
    extras = []
    replay_as_occurrence = 0
    for row in observed:
        if not isinstance(row, dict) or not isinstance(row.get("event_id"), str):
            raise ValueError("extraction_observation_row")
        ident = row["event_id"]
        if ident in by_id:
            duplicate_ids.append(ident)
        by_id[ident] = row
        if ident not in oracle:
            if any(e["id"] == ident and e["status"] == "transport_replay"
                   for e in bank["oracle_events"]):
                if row.get("kind") != "transport_receipt":
                    replay_as_occurrence += 1
            else:
                extras.append(ident)
    exact = 0
    field_counts = Counter()
    rationale_total = 0
    rationale_ok = 0
    temporal_ok = 0
    pair_total = 0
    pair_ok = 0
    for ident, expected in oracle.items():
        row = by_id.get(ident)
        if expected["reason"] is not None:
            rationale_total += 1
        if row is None:
            continue
        for field in FIELDS:
            if row.get(field) == expected[field]:
                field_counts[field] += 1
        if all(row.get(field) == expected[field] for field in FIELDS):
            exact += 1
        if expected["reason"] is not None and row.get("reason") == expected["reason"] and row.get("actor_id") == expected["actor_id"]:
            rationale_ok += 1
        if row.get("effective_at") == expected["effective_at"] and row.get("time_precision") == expected["time_precision"]:
            temporal_ok += 1
    for family in bank["families"]:
        group = [e for e in oracle.values() if e["family_id"] == family["id"]]
        for i, first in enumerate(group):
            for second in group[i+1:]:
                if first["source_order"] == second["source_order"]:
                    continue
                pair_total += 1
                a, b = by_id.get(first["id"]), by_id.get(second["id"])
                if a and b and type(a.get("source_order")) is int and type(b.get("source_order")) is int:
                    pair_ok += (a["source_order"] < b["source_order"]) == (first["source_order"] < second["source_order"])
    occurrence_ids = [row.get("occurrence_id") for row in by_id.values()
                      if row["event_id"] in oracle and row.get("occurrence_id")]
    false_merges = len(occurrence_ids) - len(set(occurrence_ids))
    return {"status": "scored", "event_coverage": _fraction(exact, len(oracle)),
            "field_accuracy": {field: _fraction(field_counts[field], len(oracle)) for field in FIELDS},
            "event_order": _fraction(pair_ok, pair_total),
            "rationale_attribution": _fraction(rationale_ok, rationale_total),
            "time_precision": _fraction(temporal_ok, len(oracle)),
            "false_merges": false_merges, "invented_event_count": len(extras),
            "duplicate_event_ids": len(duplicate_ids),
            "transport_replay_as_occurrence": replay_as_occurrence,
            "source_to_curated_pass": (exact == len(oracle) and pair_ok == pair_total and
                not extras and not duplicate_ids and not false_merges and not replay_as_occurrence)}


def _pagination(question, row):
    if question["id"] != "dev-long_pages-q1":
        return None
    pages = row.get("pages")
    if not isinstance(pages, list) or not pages:
        return False
    seen = []
    cursors = set()
    for i, page in enumerate(pages):
        if (not isinstance(page, dict) or type(page.get("serialized_bytes")) is not int
                or page["serialized_bytes"] > 320 or page["serialized_bytes"] < 1
                or not isinstance(page.get("event_ids"), list)):
            return False
        seen.extend(page["event_ids"])
        if i < len(pages) - 1:
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not isinstance(cursor, str) or not cursor or cursor in cursors:
                return False
            cursors.add(cursor)
        elif page.get("has_more") or page.get("next_cursor"):
            return False
    return seen == question["expected"]["event_ids"] and len(seen) == len(set(seen))


def score_retrieval(bank, observed):
    if observed is None:
        return {"status": "unavailable", "reason": "fixed_curated_retrieval_observations_not_supplied"}
    if not isinstance(observed, dict) or observed.get("input_layer") != "fixed_curated_records" or not isinstance(observed.get("questions"), list):
        raise ValueError("fixed_retrieval_observations_shape")
    rows = {}
    duplicates = 0
    for row in observed["questions"]:
        if not isinstance(row, dict) or not isinstance(row.get("question_id"), str):
            raise ValueError("fixed_retrieval_question_row")
        duplicates += row["question_id"] in rows
        rows[row["question_id"]] = row
    known = {q["id"]: q for q in bank["questions"]}
    unknown_questions = len(set(rows) - set(known))
    positive_complete = 0
    negative_correct = 0
    evidence_correct = 0
    evidence_total = 0
    missing = 0
    forbidden_deliveries = 0
    pagination_pass = False
    for ident, question in known.items():
        row = rows.get(ident)
        if row is None:
            missing += 1
            continue
        expected = question["expected"]
        event_ids = row.get("event_ids")
        refs = row.get("evidence_refs")
        if not isinstance(event_ids, list) or not isinstance(refs, list):
            raise ValueError("retrieval_evidence_shape")
        expected_refs = Counter(_ref(ref) for ref in expected["evidence_refs"])
        delivered_refs = Counter(_ref(ref) for ref in refs)
        evidence_correct += sum(min(count, expected_refs[ref]) for ref, count in delivered_refs.items())
        evidence_total += sum(delivered_refs.values())
        forbidden = bool(set(event_ids) & set(expected["forbidden_event_ids"]))
        if forbidden:
            forbidden_deliveries += 1
        query_semantics = (row.get("mode") == question["mode"] and row.get("as_of") == question["as_of"])
        if expected["answerable"]:
            if (row.get("answerable") is True and query_semantics and
                    set(expected["event_ids"]) <= set(event_ids) and
                    all(delivered_refs[ref] >= count for ref, count in expected_refs.items()) and
                    not forbidden):
                positive_complete += 1
        elif (row.get("answerable") is False and query_semantics and not event_ids and not refs and
              row.get("abstention_reason") == expected["abstention_reason"]):
            negative_correct += 1
        if ident == "dev-long_pages-q1":
            pagination_pass = _pagination(question, row)
    return {"status": "scored", "positive_structural_complete": _fraction(positive_complete, 36),
            "appropriate_abstention_or_denial": _fraction(negative_correct, 12),
            "evidence_precision": _fraction(evidence_correct, evidence_total),
            "pagination_hard_gate": pagination_pass,
            "forbidden_event_deliveries": forbidden_deliveries,
            "missing_question_observations": missing,
            "unknown_question_observations": unknown_questions,
            "duplicate_question_observations": duplicates,
            "semantic_answer_grading": "unavailable_without_independent_answer_facet_review",
            "fixed_retrieval_structural_pass": (positive_complete == 36 and negative_correct == 12 and
                evidence_correct == evidence_total and pagination_pass and not forbidden_deliveries and
                not missing and not unknown_questions and not duplicates)}


def evaluate(observations):
    bank, fixture_sha = load_public_bank()
    if not isinstance(observations, dict) or observations.get("development_sha256") != fixture_sha:
        raise ValueError("development_identity_mismatch")
    if "sealed_confirmation" in observations or "confirmation" in observations:
        raise ValueError("confirmation_not_admitted")
    extraction = score_extraction(bank, observations.get("source_to_curated"))
    fixed = score_retrieval(bank, observations.get("fixed_curated_retrieval"))
    end = observations.get("end_to_end")
    if end is None:
        end_result = {"status": "unavailable", "reason": "canonical_source_to_delivery_observations_not_supplied"}
    elif not isinstance(end, dict):
        raise ValueError("end_to_end_observations_shape")
    else:
        end_result = {"status": "normalized_observations_scored_not_execution_proof",
                      "source_to_curated": score_extraction(bank, end.get("source_to_curated")),
                      "retrieval": score_retrieval(bank, end.get("retrieval"))}
    return {"protocol": bank["oracle_protocol"], "development_sha256": fixture_sha,
            "confirmation_read": False, "source_to_curated": extraction,
            "fixed_curated_retrieval": fixed, "end_to_end": end_result,
            "canonical_pg_execution_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True,
                        help="Normalized DEV observations from a separate canonical PG runner")
    parser.add_argument("--output", type=Path,
                        help="Private JSON score output; stdout if omitted")
    args = parser.parse_args()
    result = evaluate(json.loads(args.observations.read_text()))
    raw = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(raw)
    else:
        print(raw, end="")


if __name__ == "__main__":
    main()
