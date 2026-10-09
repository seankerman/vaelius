"""Validate only public H1 development fixtures; never open the private seal."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures/history_retrieval_v1"


def validate_public(directory: Path = FIXTURES) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    raw = (directory / manifest["development_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["development_sha256"]:
        raise ValueError("fixture_changed")
    if len(manifest.get("confirmation_sha256", "")) != 64 or manifest.get("confirmation_questions") != 48:
        raise ValueError("confirmation_manifest_shape")
    if manifest.get("S_confirmation_sha256") != "a41fea2bc3983d8e99ea3dcdfcd7f419d4250ac325e0bd2fb151979c086fb79f":
        raise ValueError("S_confirmation_identity")
    bank = json.loads(raw)
    families = {f["id"]: f for f in bank["families"]}
    sources = {s["id"]: s for s in bank["sources"]}
    events = {e["id"]: e for e in bank["oracle_events"]}
    questions = {q["id"]: q for q in bank["questions"]}
    if any(len(x) != len(y) for x, y in ((families, bank["families"]),
                                          (sources, bank["sources"]),
                                          (events, bank["oracle_events"]),
                                          (questions, bank["questions"]))):
        raise ValueError("duplicate_id")
    if len(families) != 12 or len(questions) != 48:
        raise ValueError("suite_shape")
    if Counter(q["family_id"] for q in questions.values()) != {f: 4 for f in families}:
        raise ValueError("family_question_shape")
    if sum(q["expected"]["answerable"] for q in questions.values()) != 36:
        raise ValueError("answerability_shape")
    for source in sources.values():
        if source["family_id"] not in families or source["project_id"] != families[source["family_id"]]["project_id"]:
            raise ValueError("source_family")
        if hashlib.sha256(source["text"].encode()).hexdigest() != source["text_sha256"]:
            raise ValueError("source_text_changed")
        if source["kind"] not in {"conversation_segment", "native_document", "policy_record"}:
            raise ValueError("source_kind")
        if not source["session_id"] or not source["speaker_id"] or not source["reader_ids"]:
            raise ValueError("source_identity")
        datetime.fromisoformat(source["recorded_at"].replace("Z", "+00:00"))
    for family in families:
        if len({s["session_id"] for s in sources.values() if s["family_id"] == family}) < 2:
            raise ValueError("single_session_family")
    for event in events.values():
        if event["family_id"] not in families or event["project_id"] != families[event["family_id"]]["project_id"]:
            raise ValueError("event_family")
        if not event["actor_id"] or not event["status"] or not event["truth"]:
            raise ValueError("event_semantics")
        if (event["effective_at"] is None) != (event["time_precision"] == "unknown"):
            raise ValueError("invented_or_missing_time")
        if event["replaces_event_id"] and event["replaces_event_id"] not in events:
            raise ValueError("missing_predecessor")
        if not event["source_refs"] or not event["policy_dependencies"]:
            raise ValueError("missing_event_evidence")
        for ref in event["source_refs"]:
            source = sources[ref["source_id"]]
            if source["family_id"] != event["family_id"] or source["source_version"] != ref["version"]:
                raise ValueError("event_source_identity")
            if source["text"][ref["start"]:ref["end"]] != ref["quote"] or not ref["quote"]:
                raise ValueError("event_span")
            if event["speaker_id"] != source["speaker_id"] or event["recorded_at"] != source["recorded_at"]:
                raise ValueError("speaker_or_recorded_time")
            if ref["source_id"] not in event["policy_dependencies"]:
                raise ValueError("missing_policy_dependency")
        if any(s not in sources for s in event["policy_dependencies"]):
            raise ValueError("missing_policy_source")
    for family in families:
        ordering = [e["source_order"] for e in events.values() if e["family_id"] == family]
        if len(ordering) != len(set(ordering)) and family != "long_pages":
            raise ValueError("event_order_collision")
    fixed = bank["fixed_curated_records"]
    if len(fixed) != len(events) or {r["event_id"] for r in fixed} != set(events):
        raise ValueError("fixed_record_coverage")
    if any(r["source_refs"] != events[r["event_id"]]["source_refs"] or
           r["policy_dependencies"] != events[r["event_id"]]["policy_dependencies"] for r in fixed):
        raise ValueError("fixed_record_evidence")
    for q in questions.values():
        expected = q["expected"]
        if q["family_id"] not in families or q["project_id"] != families[q["family_id"]]["project_id"]:
            raise ValueError("question_family")
        if not isinstance(expected["answerable"], bool) or (expected["answer"] is None) == expected["answerable"]:
            raise ValueError("question_answerability")
        if expected["answerable"] and (not expected["event_ids"] or not expected["evidence_refs"]):
            raise ValueError("positive_without_evidence")
        if not expected["answerable"] and (expected["event_ids"] or expected["evidence_refs"] or not expected["abstention_reason"]):
            raise ValueError("negative_evidence")
        if any(eid not in events or events[eid]["family_id"] != q["family_id"] for eid in expected["event_ids"]):
            raise ValueError("question_event")
        if any(eid not in events or events[eid]["family_id"] != q["family_id"] for eid in expected["forbidden_event_ids"]):
            raise ValueError("forbidden_event")
        refs = [r for eid in expected["event_ids"] for r in events[eid]["source_refs"]]
        if expected["evidence_refs"] != refs:
            raise ValueError("question_evidence")
        if q["mode"] not in {"current", "history", "effective_at", "known_at", "original"}:
            raise ValueError("question_mode")
        if q["mode"] in {"effective_at", "known_at", "original"} and not q["as_of"]:
            raise ValueError("missing_query_time_or_version")
        for eid in expected["event_ids"]:
            if q["reader_id"] not in events[eid]["audience"]:
                raise ValueError("answer_leaks_audience")
    operations = {o["id"]: o for o in bank["runtime_operations"]}
    if len(operations) != len(bank["runtime_operations"]):
        raise ValueError("duplicate_operation")
    if not {"ordinary_ingest", "ordinary_update", "bounded_vector_refresh", "replace_native_version",
            "delete_source", "revoke_reader", "invalidate_summary", "concurrent_expected_revision"} <= {o["kind"] for o in operations.values()}:
        raise ValueError("missing_operation_kind")
    for operation in operations.values():
        for key in ("source_id", "old_source_id", "new_source_id", "evidence_source_id"):
            if key in operation and operation[key] not in sources:
                raise ValueError("operation_source")
    if len([s for s in sources.values() if s["family_id"] == "deep_history" and s["id"].split("-")[-1].startswith("noise")]) < 201:
        raise ValueError("short_deep_history")
    if len([e for e in events.values() if e["family_id"] == "long_pages" and e["status"] == "milestone"]) < 12:
        raise ValueError("short_timeline")
    return {"pass": True, "families": len(families), "sources": len(sources),
            "events": len(events), "questions": len(questions), "positive": 36,
            "abstention_or_denial": 12, "development_sha256": manifest["development_sha256"],
            "confirmation_sha256": manifest.get("confirmation_sha256"),
            "sealed_questions_read": False}


if __name__ == "__main__":
    print(json.dumps(validate_public(), sort_keys=True))
