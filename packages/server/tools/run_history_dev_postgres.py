"""Provider-free H DEV fixed-record projection into an isolated canonical PG schema.

This uses an explicitly labeled source-only pin patch while the source trees are
changing. It does not run extraction, install a release, or access confirmation.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests")]

from agentclient.enterprise_contract import VERSION
from agenthub.processing.episode_pipeline import _record_episode_revision
from agenthub.processing.temporal import record_assertion
from agenthub.cloud_runtime import CloudStore
from agenthub.document_ingest import DocumentStore
from agenthub.enterprise import Denied
from agenthub.project_history import HistoryDenied, project_episode_detail, project_overview
from agenthub.source_objects import FileSourceObjects
from test_cloud_postgres import PostgresFixture

from evaluate_history_dev import evaluate, load_public_bank


PRIVATE_ROOT = None  # Explicit synthetic evaluation workspace, never a developer default.
CONTROLS = Path(__file__).parent / "fixtures/history_retrieval_v1/projection_controls_v1.json"


def load_projection_controls(bank, development_sha):
    manifest = json.loads(CONTROLS.with_name("projection_controls_v1_manifest.json").read_text())
    raw = CONTROLS.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise ValueError("projection_controls_changed")
    controls = json.loads(raw)
    if controls["development_sha256"] != development_sha:
        raise ValueError("projection_development_mismatch")
    records = {row["event_id"]: row for row in bank["fixed_curated_records"]}
    sources = {row["id"]: row for row in bank["sources"]}
    by_id = {row["event_id"]: row for row in controls["records"]}
    if len(by_id) != len(controls["records"]) or set(by_id) != set(records):
        raise ValueError("projection_record_set_mismatch")
    for ident, control in by_id.items():
        record = records[ident]
        quote = record["source_refs"][0]["quote"]
        if hashlib.sha256(quote.encode()).hexdigest() != control["source_quote_sha256"]:
            raise ValueError("projection_quote_mismatch")
        typed = control.get("temporal")
        if typed:
            source_text = "\n".join(sources[ref["source_id"]]["text"]
                                    for ref in record["source_refs"])
            if typed["value"].casefold() not in source_text.casefold():
                raise ValueError("projection_value_not_source_grounded")
            if typed["time_basis"] != "authored_record_not_source_proven":
                raise ValueError("projection_time_basis_untrusted")
            change = typed.get("change")
            if change:
                previous = by_id.get(change["prior_event_id"], {}).get("temporal")
                if (not previous or previous["subject"] != typed["subject"] or
                    previous["predicate"] != typed["predicate"] or
                    change["relation"] not in {"corrects", "supersedes", "conflicts"} or
                    change["reviewed"] is not True):
                    raise ValueError("projection_relation_scope_mismatch")
    return by_id


def projection_decision(record, control):
    if record["event_id"] != control["event_id"]:
        raise ValueError("projection_event_mismatch")
    return {"episode": control["episode_kind"] == "occurrence",
            "reviewed_note": control["claim_kind"] in {
                "operative", "reported_unverified", "conflicted_report", "private_preference"},
            "claim_kind": control["claim_kind"]}


def _safe_paths(services, output):
    if PRIVATE_ROOT is None:
        raise ValueError('explicit_evaluation_workspace_required')
    services = Path(services).expanduser().resolve(strict=True)
    output = Path(output).expanduser().absolute()
    if services != PRIVATE_ROOT / "pg-h3-v1/services.json" or services.stat().st_mode & 0o077:
        raise ValueError("h_owned_private_services_required")
    if output.parent != PRIVATE_ROOT / "receipts" or output.exists():
        raise ValueError("new_h_private_receipt_required")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return services, output


def _principal(source):
    return "alex-42" if source["reader_ids"] == ["alex-42"] else "ada"


def _source_record(store, ctx, document_service, connections, source):
    owner = _principal(source)
    if source["kind"] == "native_document":
        connection = connections[source["project_id"]]
        external = source["original_id"] or source["id"]
        result = document_service.ingest(ctx[owner], connection, external,
            source["source_version"], external + ".md", io.BytesIO(source["text"].encode()),
            title=external, occurred_at=source["recorded_at"])
        return result["source_id"]
    visibility = "team" if len(source["reader_ids"]) > 1 else "private"
    result = store.ingest(ctx[owner], {"version": VERSION, "external_id": source["id"],
        "session": source["session_id"], "turn": source["id"],
        "project": source["project_id"], "kind": "Stop", "body": source["text"],
        "occurred_at": source["recorded_at"], "speaker": source["speaker_id"],
        "visibility": visibility})
    return result["source_id"]


def _fixed_projection(store, ctx, bank, sources, controls):
    """Project authored DEV roles without treating every historical event as a claim."""
    generation = "h1-dev-fixed-authored-v1"
    doc_to_event = {}
    occurrence_to_event = {}
    doc_to_occurrence = {}
    temporal_ids = {}
    projected = {"transport_receipts": 0, "historical_occurrences": 0,
                 "reviewed_notes": 0, "temporal_assertions": 0,
                 "reviewed_relations": 0}
    source_meta = {}
    with store.open() as state:
        for fixture_id, actual_id in sources.items():
            row = state.db.execute("""SELECT s.internal_project,m.session
                FROM enterprise_sources s JOIN memories m ON m.id=s.id WHERE s.id=?""",
                (actual_id,)).fetchone()
            if row is None:
                raise ValueError("fixture_source_not_in_canonical_authority")
            source_meta[fixture_id] = dict(row)
    with store.open() as state, state.db:
        state.db.execute("""INSERT INTO knowledge_generations
            (generation_id,curator_version,status,source_policy,config_hash,created)
            VALUES(?,?,?,?,?,?)""", (generation, "h1-authored-fixed", "active",
                                     "synthetic-authorized", "source-only", 1.0))
    for fixed in bank["fixed_curated_records"]:
        event = dict(fixed, id=fixed["event_id"])
        control = controls[event["id"]]
        decision = projection_decision(fixed, control)
        if not decision["episode"]:
            projected["transport_receipts"] += 1
            continue
        fixture_source = event["source_refs"][0]["source_id"]
        source_id = sources[fixture_source]
        owner = _principal(next(s for s in bank["sources"] if s["id"] == fixture_source))
        title = (event["source_refs"][0]["quote"][:140] or event["id"])
        lesson = (event["source_refs"][0]["quote"] + " — synthetic fixed curated record")[:1200]
        if decision["claim_kind"] == "reported_unverified":
            lesson = "Unverified report pending review. " + lesson
        elif decision["claim_kind"] == "conflicted_report":
            lesson = "Unresolved reported value; do not select as settled. " + lesson
        if len(lesson) < 20:
            lesson += " Synthetic source evidence."
        with store.open() as state:
            source_state = state.db.execute("SELECT active,owner FROM enterprise_sources WHERE id=?",
                (source_id,)).fetchone()
        note = None
        if (decision["reviewed_note"] and source_state and source_state["active"]
                and source_state["owner"] == ctx[owner]["actor"]):
            note = store.accept_reviewed_note(ctx[owner], source_id, title, lesson)
            doc_to_event[note["document_id"]] = event["id"]
            projected["reviewed_notes"] += 1
        typed = control.get("temporal")
        if typed and note is not None:
            effective = event["effective_at"]
            precision = event["time_precision"]
            qualification = ({"from": effective, "to_status": "ongoing",
                              "precision": precision, "timezone": "UTC",
                              "basis": "inferred"} if effective else None)
            source_ids = [sources[ref["source_id"]] for ref in event["source_refs"]]
            change = typed.get("change")
            prior = temporal_ids.get(change["prior_event_id"]) if change else None
            if change and not prior:
                raise ValueError("projection_relation_prior_missing")
            with store.open() as state, state.db:
                assertion = record_assertion(state.db, revision_id=note["revision_id"],
                    subject=typed["subject"], predicate=typed["predicate"],
                    value=typed["value"], actor=None,
                    validity=qualification, evidence_source_ids=source_ids,
                    recorded_at=datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00")).timestamp(),
                    change=({"relation": change["relation"], "assertion_id": prior,
                             "reviewed": True} if change else None))
            temporal_ids[event["id"]] = assertion["assertion_id"]
            projected["temporal_assertions"] += 1
            projected["reviewed_relations"] += int(change is not None)
        project = source_meta[fixture_source]["internal_project"]
        session = source_meta[fixture_source]["session"]
        turn = event["id"]
        job_id = "h1-job-" + hashlib.sha256(event["id"].encode()).hexdigest()[:32]
        actual_ids = [sources[r["source_id"]] for r in event["source_refs"]]
        ref_list = [dict(ref, source_id=sources[ref["source_id"]], segment_id=event["id"])
                    for ref in event["source_refs"]]
        record = {"title": title, "text": lesson, "subject": event["project_id"],
                  "facets": ["activity"] if event["status"] in {"failed_attempt", "rejected_proposal"}
                            else ["decision"] if event["status"] == "adopted" else ["observation"],
                  "actors": [event["actor_id"]], "state": event["status"],
                  "claim_status": decision["claim_kind"],
                  "attribution": event["truth"], "artifact_name": "", "location": "",
                  "reason_actor": event["actor_id"] if event["reason"] else "",
                  "reason_quote": event["reason"] or "", "occurred_date": event["effective_at"] or ""}
        with store.open() as state, state.db:
            db = state.db
            db.execute("""INSERT INTO curation_episode_jobs
                (id,generation_id,episode_id,project,session,turn,source_ids,source_hash,
                 created,updated,version,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'done')""",
                (job_id, generation, event["id"], project, session, turn,
                 json.dumps(actual_ids), event["id"], 1.0, 1.0, "h1-fixed"))
            row = {"id": job_id, "generation_id": generation, "project": project,
                   "session": session, "turn": turn, "source_hash": event["id"],
                   "source_ids": json.dumps(actual_ids)}
            atom = {"atom_key": event["id"], "record": record,
                    "evidence": ref_list, "operation": "AUTHORED_FIXED"}
            links = ([{"atom_key": event["id"], "candidate_id": event["id"],
                       "document_id": note["document_id"],
                       "claim_revision_id": note["revision_id"], "operation": "CREATE"}]
                     if note is not None else [])
            revision_id = _record_episode_revision(db, row,
                {"episode_disposition": "has_learning" if note is not None else "no_learning"},
                [atom], links, {}, time.time())
            occurrence = db.execute("SELECT occurrence_id FROM episode_revisions WHERE revision_id=?",
                                    (revision_id,)).fetchone()[0]
            occurrence_to_event[occurrence] = event["id"]
            projected["historical_occurrences"] += 1
            if note is not None:
                doc_to_occurrence[note["document_id"]] = occurrence
    return generation, doc_to_event, occurrence_to_event, doc_to_occurrence, projected


def _normalize_refs(refs, reverse_sources):
    return [{key: (reverse_sources.get(ref[key], ref[key]) if key == "source_id" else ref[key])
             for key in ("source_id", "version", "start", "end", "quote") if key in ref}
            for ref in refs]


def _query(store, ctx, service, request, doc_to_event, occurrence_to_event,
           doc_to_occurrence, reverse_sources, originals):
    project = request["project_id"]
    mode = request["mode"]
    result = {"question_id": request["id"], "mode": mode, "as_of": request["as_of"],
              "answerable": False, "event_ids": [], "evidence_refs": [],
              "abstention_reason": None, "coverage_gaps": [], "route": None}
    try:
        if hasattr(store, "_project_member"):
            with store.open() as state:
                reader = ctx[request["reader_id"]]
                if hasattr(store, "current_identity"):
                    store.current_identity(state.db, reader)
                member = store._project_member(state.db, reader, project)
                own_or_org = state.db.execute("""SELECT 1 FROM enterprise_sources
                    WHERE tenant=? AND external_project=? AND active=1
                      AND (owner=? OR visibility='organization') LIMIT 1""",
                    (reader["tenant"], project, reader["actor"])).fetchone()
                if not member and not own_or_org:
                    result.update(abstention_reason="current_policy_denial",
                                  coverage_gaps=["project_scope_denied"], route="canonical_scope_check")
                    return result
        if mode == "original":
            result["route"] = "canonical_original_list_fetch"
            version = str(request["as_of"])
            choices = service.list(ctx[request["reader_id"]], title="oak-design", limit=20)
            item = next((x for x in choices if x.get("version") == version and x.get("source_id") in originals), None)
            if item is not None:
                stream, headers = service.fetch(ctx[request["reader_id"]], item["source_id"], version=version)
                with stream:
                    raw = stream.read()
                if hashlib.sha256(raw).hexdigest() == headers["X-Source-SHA256"]:
                    original = originals[item["source_id"]]
                    ref = original["ref"]
                    try:
                        exact = raw.decode("utf-8")[ref["start"]:ref["end"]] == ref["quote"]
                    except UnicodeDecodeError:
                        exact = False
                    if exact:
                        result.update(answerable=True, event_ids=[original["event_id"]],
                                      evidence_refs=_normalize_refs([dict(ref, source_id=item["source_id"])],
                                                                    reverse_sources))
                    else:
                        result["coverage_gaps"] = ["original_exact_span_unverified"]
            else:
                result["coverage_gaps"] = ["original_version_not_discoverable_by_list"]
            return result
        broad = mode == "history" and bool(re.search(
            r'\b(?:history|evolv\w*|sequence|all phases|full authorized)\b', request["query"], re.I))
        if broad:
            result["route"] = "canonical_project_overview"
            cursor = None
            pages = []
            for _ in range(100):
                page = project_overview(store, ctx[request["reader_id"]], project,
                    cursor=cursor, page_limit=20,
                    max_bytes=320 if request["id"] == "dev-long_pages-q1" else 4000)
                result["coverage_gaps"].extend(page.get("coverage_gaps", []))
                ids = [occurrence_to_event[item["episode_id"]] for item in page["episodes"]
                       if item["episode_id"] in occurrence_to_event]
                result["event_ids"].extend(ids)
                for item in page["episodes"]:
                    try:
                        detail = project_episode_detail(store, ctx[request["reader_id"]],
                            project, item["episode_id"], limit=8)
                        refs = [ref for assertion in detail.get("assertions", [])
                                for ref in assertion.get("evidence", [])]
                        result["evidence_refs"].extend(_normalize_refs(refs, reverse_sources))
                    except Exception:
                        pass
                pages.append({"event_ids": ids, "serialized_bytes": len(json.dumps(page, ensure_ascii=True,
                    separators=(",", ":")).encode()), "has_more": page["has_more"],
                    "next_cursor": page["next_cursor"]})
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]
            result["answerable"] = bool(result["event_ids"])
            if request["id"] == "dev-long_pages-q1":
                result["pages"] = pages
            return result
        result["route"] = "canonical_search"
        value = {"version": VERSION, "query": request["query"], "project": project,
                 "mode": "explicit", "limit": 8}
        if mode in {"effective_at", "known_at"}:
            value["time_mode"] = mode
            value["as_of"] = request["as_of"]
        response = store.search(ctx[request["reader_id"]], value)
        result["answerable"] = bool(response.get("answerable"))
        result["coverage_gaps"] = list(response.get("coverage_gaps") or [])
        if not result["answerable"] and len(result["coverage_gaps"]) == 1:
            result["abstention_reason"] = result["coverage_gaps"][0]
        result["event_ids"] = [doc_to_event[card["id"]] for card in response.get("results", [])
                               if card.get("id") in doc_to_event]
        # Exercise the canonical progressive detail path. Backing dependency
        # rows alone are never upgraded into delivered citations.
        for card in response.get("results", []):
            occurrence = doc_to_occurrence.get(card.get("id"))
            if occurrence is None:
                continue
            try:
                detail = project_episode_detail(store, ctx[request["reader_id"]],
                    project, occurrence, limit=8)
                refs = [ref for assertion in detail.get("assertions", [])
                        for ref in assertion.get("evidence", [])]
                result["evidence_refs"].extend(_normalize_refs(refs, reverse_sources))
            except Exception:
                pass
        return result
    except (Denied, HistoryDenied):
        result["abstention_reason"] = "current_policy_denial"
        result["coverage_gaps"].append("project_scope_denied")
        return result
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["coverage_gaps"].append("query_error")
        return result


def run(services_path, output_path, *, question_id=None):
    services_path, output_path = _safe_paths(services_path, output_path)
    bank, fixture_sha = load_public_bank()
    controls = load_projection_controls(bank, fixture_sha)
    os.environ["AGENTNETWORK_PG_SERVICES"] = str(services_path)
    import test_cloud_postgres
    test_cloud_postgres.SERVICES = str(services_path)
    fixture = PostgresFixture()
    with patch("agenthub.pipeline_pin.verify", return_value={"source_only_pin_patch": True}):
        fixture.postgres_setup(tenant_id="synthetic-lab", bootstrap=False)
        try:
            store = CloudStore(Path(fixture.temp.name) / "history-store", fixture.dsn, "synthetic-lab")
            store.hybrid_enabled = False
            store.semantic_embedder = None
            store.answerability_judge = None
            store.retrieval_selection_policy = "facets_v2"
            store.create_organization("synthetic-lab")
            for principal in ("ada", "ben", "alex-42"):
                store.create_principal("synthetic-lab", principal)
            for family in bank["families"]:
                project = family["project_id"]
                store.create_project("synthetic-lab", project)
                for principal in ("ada", "ben"):
                    store.set_membership("synthetic-lab", project, principal, True)
            store.set_membership("synthetic-lab", "larch-team", "alex-42", True)
            tokens = {principal: store.enroll("synthetic-lab", principal, "h1-" + principal,
                ["ingest", "read", "source_read", "correct", "withdraw", "policy"])
                for principal in ("ada", "ben", "alex-42")}
            ctx = {principal: store.authenticate(token) for principal, token in tokens.items()}
            connections = {}
            for source in (s for s in bank["sources"] if s["kind"] == "native_document"):
                project = source["project_id"]
                if project not in connections:
                    ident = "h1-doc-" + project
                    store.enroll_connection(ctx["ada"], ident, "synthetic", project,
                        ["document"], visibility="team", reader_ids=["ada", "ben"])
                    connections[project] = ident
            document_service = DocumentStore(store, FileSourceObjects(Path(fixture.temp.name) / "objects"))
            source_ids = {}
            for source in bank["sources"]:
                source_ids[source["id"]] = _source_record(store, ctx, document_service, connections, source)
            generation, doc_to_event, occurrence_to_event, doc_to_occurrence, projected = _fixed_projection(
                store, ctx, bank, source_ids, controls)
            with store.open() as state:
                temporal_assertions = state.db.execute(
                    "SELECT count(*) FROM knowledge_temporal_assertions WHERE document_id IN (" +
                    ",".join("?" for _ in doc_to_event) + ")",
                    list(doc_to_event)).fetchone()[0]
            # Current-policy transitions are fixture operations, never inferred
            # from historical timestamps or applied retroactively by a query.
            store.set_membership("synthetic-lab", "larch-team", "alex-42", False)
            store.set_membership("synthetic-lab", "fir-incident", "ben", False)
            with store.open() as state, state.db:
                state.db.execute("UPDATE enterprise_sources SET active=0 WHERE id=?",
                                 (source_ids["dev-concurrency_lifecycle-s2"],))
            original_records = {record["source_refs"][0]["source_id"]: record
                for record in bank["fixed_curated_records"]
                if record["status"] == "document_version"}
            originals = {source_ids[s["id"]]: {"event_id": original_records[s["id"]]["event_id"],
                "ref": original_records[s["id"]]["source_refs"][0]}
                for s in bank["sources"] if s["id"] in original_records}
            reverse_sources = {actual: fixture_id for fixture_id, actual in source_ids.items()}
            requests = [{k: v for k, v in question.items() if k != "expected"}
                        for question in bank["questions"]]
            if question_id is not None:
                requests = [request for request in requests if request["id"] == question_id]
                if len(requests) != 1:
                    raise ValueError("unknown_development_question")
            rows = [_query(store, ctx, document_service, request, doc_to_event,
                           occurrence_to_event, doc_to_occurrence, reverse_sources, originals)
                    for request in requests]
            observations = {"development_sha256": fixture_sha,
                "fixed_curated_retrieval": {"input_layer": "fixed_curated_records", "questions": rows}}
            grade = evaluate(observations) if question_id is None else None
            receipt = {"classification": ("targeted consumed-DEV diagnostic; not scored or held-out"
                if question_id is not None else
                "source-only canonical PostgreSQL fixed-curated DEV projection; no extraction or installed claim"),
                "development_sha256": fixture_sha, "generation_id": generation,
                "source_only_pin_patch": True, "provider_calls": 0, "embedding_calls": 0,
                "sources_ingested": len(source_ids), "authored_fixed_records": len(bank["fixed_curated_records"]),
                "fixed_record_count": len(bank["fixed_curated_records"]),
                "projection": projected,
                "temporal_assertions_projected": temporal_assertions,
                "temporal_projection_status": ("typed_but_authored_dates_not_source_proven" if temporal_assertions
                                               else "unavailable"),
                "observations": observations, "grade": grade,
                "targeted_question_id": question_id,
                "end_to_end": "unavailable_no_model_curation", "schema_isolated": True,
                "services_manifest": "H-owned pg-h3-v1; DSN omitted"}
            fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream, indent=2, sort_keys=True)
                stream.write("\n")
            return {"receipt": str(output_path), "sources": len(source_ids),
                    "fixed_records": len(doc_to_event),
                    "structural_positive": (grade["fixed_curated_retrieval"]["positive_structural_complete"]
                                            if grade is not None else None),
                    "abstention": (grade["fixed_curated_retrieval"]["appropriate_abstention_or_denial"]
                                   if grade is not None else None),
                    "targeted_question_id": question_id,
                    "provider_calls": 0, "end_to_end": "unavailable"}
        finally:
            fixture.postgres_teardown()


def main():
    global PRIVATE_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="explicit isolated evaluation workspace")
    parser.add_argument("--services", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--question-id", help="one consumed DEV case for unscored diagnosis")
    args = parser.parse_args()
    PRIVATE_ROOT = Path(args.workspace).expanduser().resolve()
    print(json.dumps(run(args.services, args.output,question_id=args.question_id), sort_keys=True))


if __name__ == "__main__":
    main()
