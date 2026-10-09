"""Provider-free, source-grounded diagnostics for the retrieval experiment.

This module calls the canonical search once per question. Its test-only wrappers
observe work already performed by search; they never run a second candidate query.
Outputs contain synthetic/private evidence and belong in the private campaign path.
"""
from contextlib import contextmanager
import hashlib
import datetime
import decimal
import tempfile
import json
import math
import os
from pathlib import Path
import re
import time

GATES = {"recall_at_10": .95, "answerable_coverage": .85,
         "delivered_precision": .95, "abstention": .95}
ORACLE_PROTOCOL = "retrieval-source-span-actor-original-workflow-v4"
IMMUTABLE_KEYS = ("model_key", "build_ids", "corpus_sha256", "source_sha256", "labels_sha256")


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def _source_id(mapping, fixture_id):
    value = mapping[fixture_id]
    return value if isinstance(value, str) else value["source_id"]


def validate_suite(sources, questions, *, expected_split=None, principals=None,
                   require_shape=False, other_questions=()):
    """Validate labels against originals without evaluating or tuning a query."""
    sources = {s["id"]: s for s in sources} if isinstance(sources, list) else sources
    if len({q["id"] for q in questions}) != len(questions):
        raise ValueError("duplicate_question_id")
    scenarios = {q["scenario"] for q in questions}
    if scenarios & {q["scenario"] for q in other_questions}:
        raise ValueError("scenario_split_overlap")
    used_sources = set()
    for question in questions:
        expected = question["expected"]
        if expected_split is not None and question["split"] != expected_split:
            raise ValueError("question_split_mismatch")
        if principals is not None and (question["tenant"], question["principal"]) not in principals:
            raise ValueError("invalid_principal")
        if not isinstance(expected.get("answerable"), bool):
            raise ValueError("answerability_label_required")
        facets = expected.get("facets", [])
        evidence_sets = expected.get("evidence_sets", [])
        if expected["answerable"] and (not facets or not evidence_sets or any(not s for s in evidence_sets)):
            raise ValueError("answerable_without_complete_evidence")
        if len({f["id"] for f in facets}) != len(facets):
            raise ValueError("duplicate_facet_id")
        expected_ids = set(expected.get("source_ids", []))
        for group in evidence_sets:
            if not set(group) <= expected_ids:
                raise ValueError("evidence_set_outside_expected_sources")
        used_sources.update(expected_ids)
        for ident in expected_ids:
            if ident not in sources:
                raise ValueError("absent_expected_source")
            source = sources[ident]
            if source["tenant"] != question["tenant"] or source["scenario"] != question["scenario"]:
                raise ValueError("label_source_identity_mismatch")
            if source["split"] != question["split"]:
                raise ValueError("source_split_mismatch")
        for facet in facets:
            if not facet.get("value") or not facet.get("source_ids") or not facet.get("quote"):
                raise ValueError("facet_without_value_or_evidence")
            if not set(facet["source_ids"]) <= expected_ids:
                raise ValueError("facet_outside_expected_sources")
            for ident in facet["source_ids"]:
                text = sources[ident]["text"]
                start, end = facet["start"], facet["end"]
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                    raise ValueError("facet_span_bounds")
                if text[start:end] != facet["quote"]:
                    raise ValueError("facet_quote_not_source_evidence")
            for group in evidence_sets:
                if not set(group).intersection(facet["source_ids"]):
                    raise ValueError("evidence_set_cannot_support_facet")
    if used_sources & {s for q in other_questions for s in q["expected"].get("source_ids", [])}:
        raise ValueError("source_split_overlap")
    if require_shape:
        families = {q["family"] for q in questions}
        positives = sum(q["expected"]["answerable"] for q in questions)
        if len(questions) != 72 or positives != 55 or len(families) != 12:
            raise ValueError("suite_shape_mismatch")
        if any(sum(q["family"] == f for q in questions) != 6 for f in families):
            raise ValueError("suite_family_shape_mismatch")
    return {"questions": len(questions), "sources": len(used_sources),
            "scenarios": len(scenarios), "labels_sha256": content_hash(questions)}


class _Cursor:
    def __init__(self, cursor, event, trace):
        self._cursor, self._event, self._trace = cursor, event, trace

    def __getattr__(self, key):
        return getattr(self._cursor, key)

    def _observed(self, values):
        self._event["fetch_seconds"] += time.monotonic() - self._event.pop("fetch_start")
        if self._event.get("channel"):
            for rank, row in enumerate(values, 1):
                item = dict(row)
                if item.get("document_id"):
                    self._trace["channel_rows"].append({
                        "document_id": item["document_id"], "revision_id": item.get("revision_id"),
                        "channel": self._event["channel"], "rank": rank,
                        "score": item.get("score"), "candidate_call": self._event["candidate_call"]})
        self._event["rows_observed"] += len(values)
        return values

    def fetchall(self):
        self._event["fetch_start"] = time.monotonic()
        return self._observed(self._cursor.fetchall())

    def fetchone(self):
        self._event["fetch_start"] = time.monotonic()
        row = self._cursor.fetchone()
        self._observed([] if row is None else [row])
        return row

    def __iter__(self):
        return iter(self.fetchall())


class _Database:
    def __init__(self, db, trace):
        self._db, self._trace = db, trace

    def __getattr__(self, key):
        return getattr(self._db, key)

    def __enter__(self):
        self._db.__enter__()
        return self

    def __exit__(self, *args):
        return self._db.__exit__(*args)

    def execute(self, sql, *args, **kwargs):
        started = time.monotonic()
        cursor = self._db.execute(sql, *args, **kwargs)
        channel = ("vector" if "v.embedding <=>" in sql else
                   "lexical" if "ts_rank_cd" in sql else
                   "exact" if "SELECT d.document_id,r.revision_id,r.claim_json" in sql and
                   "AND d.document_id=ANY" in sql else None)
        event = {"sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                 "execute_seconds": time.monotonic() - started, "fetch_seconds": 0.,
                 "rows_observed": 0, "channel": channel,
                 "candidate_call": self._trace.get("active_candidate_call")}
        self._trace["sql"].append(event)
        return _Cursor(cursor, event, self._trace)


class _State:
    def __init__(self, state, trace):
        self._state, self.db = state, _Database(state.db, trace)

    def __getattr__(self, key):
        return getattr(self._state, key)


class _Embedder:
    def __init__(self, embedder, trace):
        self._embedder, self._trace = embedder, trace

    def __getattr__(self, key):
        return getattr(self._embedder, key)

    def embed_queries(self, values):
        started = time.monotonic()
        try:
            return self._embedder.embed_queries(values)
        finally:
            self._trace["embedding"].append({"seconds": time.monotonic() - started,
                                             "queries": len(values)})


@contextmanager
def observe_search(store, enabled=True):
    """Temporary instance wrappers; exclusive ownership required during evaluation."""
    trace = {"candidate_calls": [], "channel_rows": [], "sql": [], "embedding": []}
    if not enabled:
        yield trace
        return
    originals = {name: (name in vars(store), getattr(store, name))
                 for name in ("candidates", "open", "semantic_embedder")}
    original_candidates = originals["candidates"][1]
    original_open = originals["open"][1]

    def candidates(*args, **kwargs):
        call = {"index": len(trace["candidate_calls"]), "requested_limit": kwargs.get("limit", 10),
                "query_sha256": hashlib.sha256(str(args[1]).encode()).hexdigest() if len(args) > 1 else None}
        trace["candidate_calls"].append(call)
        trace["active_candidate_call"] = call["index"]
        started = time.monotonic()
        try:
            rows = original_candidates(*args, **kwargs)
            call["items"] = [dict(r) for r in rows]
            return rows
        finally:
            call["seconds_inclusive"] = time.monotonic() - started
            trace["active_candidate_call"] = None

    @contextmanager
    def opened(*args, **kwargs):
        with original_open(*args, **kwargs) as state:
            yield _State(state, trace)

    store.candidates, store.open = candidates, opened
    if originals["semantic_embedder"][1] is not None:
        store.semantic_embedder = _Embedder(originals["semantic_embedder"][1], trace)
    try:
        yield trace
    finally:
        for name, (was_instance, value) in originals.items():
            if was_instance:
                setattr(store, name, value)
            elif name in vars(store):
                delattr(store, name)


def canonical_evidence(store, ctx, card):
    """Current canonical authorization plus native/curated/temporal provenance.

    This inspection runs after the measured request, and is not search work. A
    canonical source dependency alone does not establish passage-level support.
    """
    ident = card.get("id", card.get("document_id"))
    revision = card.get("revision", card.get("revision_id"))
    with store.open() as state:
        db = state.db
        doc = store._document_allowed(db, ctx, ident)
        temporal = None
        if not doc:
            temporal = store._temporal_allowed(db, ctx, ident)
            if not temporal:
                return {"allowed": False, "source_ids": [], "spans": [], "claim": {}}
            ident = temporal["document_id"]
        row = db.execute("SELECT claim_json FROM knowledge_revisions WHERE document_id=? AND revision_id=?",
                         (ident, revision)).fetchone()
        if not row:
            return {"allowed": False, "source_ids": [], "spans": [], "claim": {}}
        if not temporal and doc["active_revision_id"] != revision:
            return {"allowed": False, "source_ids": [], "spans": [], "claim": {}}
        claim = json.loads(row[0])
        dependencies = [r[0] for r in db.execute(
            "SELECT source_id FROM enterprise_dependencies WHERE document_id=?", (ident,))]
        if temporal:
            dependencies = [r[0] for r in db.execute(
                "SELECT source_memory_id FROM knowledge_temporal_evidence WHERE assertion_id=?",
                (temporal["assertion_id"],))]
        refs = [dict(r) for r in db.execute(
            "SELECT source_memory_id,source_segment_id FROM knowledge_support WHERE revision_id=?", (revision,))]
        spans = [dict(r) for r in db.execute(
            'SELECT source_id,start,"end" FROM backend_native_spans WHERE document_id=?', (ident,))]
        native_document = bool(spans)
        revisions, owners = {}, {}
        from agenthub.processing.episode_curator import _spans
        for source_id in dependencies:
            owner = db.execute("SELECT owner FROM enterprise_sources WHERE id=?", (source_id,)).fetchone()
            if owner:
                owners[source_id] = owner[0]
            source = db.execute("SELECT revision FROM backend_source_revisions WHERE source_id=?", (source_id,)).fetchone()
            revisions[source_id] = source[0] if source else "1"
            body = db.execute("SELECT body FROM memories WHERE id=?", (source_id,)).fetchone()
            if body:
                cited = {r["source_segment_id"] for r in refs if r["source_memory_id"] == source_id}
                for span in _spans(source_id, body[0]):
                    if span["span_id"] in cited:
                        spans.append({"source_id": source_id, "start": span["start"], "end": span["end"]})
        return {"allowed": True, "source_ids": dependencies, "spans": spans,
                "source_revisions": revisions, "source_owners": owners, "claim": claim,
                "native_document": native_document,
                "temporal": dict(temporal) if temporal else None}


def authorized_evidence_oracle(store, ctx, expected, source_map):
    """Read labeled canonical dependencies, with current authorization per document.

    This deliberately small exact inspection is outside measured search. It does
    not create an index or obtain candidates using a broader identity. Denied
    dependencies are exposed only to the private evaluation receipt.
    """
    ids = [_source_id(source_map, s) for s in expected.get("source_ids", [])]
    if not ids:
        return {"documents": [], "source_ids_present": [], "denied_document_count": 0}
    with store.open() as state:
        present = [r[0] for r in state.db.execute(
            "SELECT id FROM enterprise_sources WHERE id=ANY(?::text[])", (ids,))]
        documents = [dict(r) for r in state.db.execute("""SELECT DISTINCT d.document_id,d.active_revision_id
            FROM knowledge_documents d JOIN enterprise_dependencies e ON e.document_id=d.document_id
            WHERE e.source_id=ANY(?::text[]) ORDER BY d.document_id""", (ids,))]
        permitted = [row for row in documents if store._document_allowed(state.db, ctx, row["document_id"])]
        assertions = [dict(r) for r in state.db.execute("""SELECT DISTINCT a.assertion_id,a.revision_id
            FROM knowledge_temporal_assertions a JOIN knowledge_temporal_evidence e ON e.assertion_id=a.assertion_id
            WHERE e.source_memory_id=ANY(?::text[]) ORDER BY a.assertion_id""", (ids,))]
        allowed_assertions = [row for row in assertions if store._temporal_allowed(state.db, ctx, row["assertion_id"])]
    cards = [{"id": row["document_id"], "revision": row["active_revision_id"]} for row in permitted]
    cards += [{"id": row["assertion_id"], "revision": row["revision_id"]} for row in allowed_assertions]
    return {"documents": cards, "source_ids_present": present,
            "denied_document_count": len(documents) - len(permitted)}


def _normalized(value):
    return " ".join(str(value).casefold().split())


def _value_present(text, facet):
    values = [facet["value"], *facet.get("aliases", [])]
    if facet.get("type") in {"path", "id", "version", "date", "sha256"}:
        return any(re.search(r"(?<![\w./-])" + re.escape(str(value)) + r"(?![\w/-]|\.[\w])", text)
                   for value in values)
    text = _normalized(text)
    return any(re.search(r"(?<!\w)" + re.escape(_normalized(value)) + r"(?!\w)", text)
               for value in values)


def _facet_provenance(facet, evidence, source_map):
    if not evidence.get("allowed"):
        return False
    for fixture_id in facet["source_ids"]:
        canonical_id = _source_id(source_map, fixture_id)
        if canonical_id not in evidence.get("source_ids", []):
            continue
        expected_revision = str(facet.get("revision", "1"))
        if str(evidence.get("source_revisions", {}).get(canonical_id, "")) != expected_revision:
            continue
        if any(s["source_id"] == canonical_id and s["start"] <= facet["start"] and
               s["end"] >= facet["end"] for s in evidence.get("spans", [])):
            return True
    return False


def _facet_supported(facet, card, evidence, source_map, *, delivered=True):
    if not _facet_provenance(facet, evidence, source_map):
        return False
    claim = evidence.get("claim", {})
    # Ranking can locate a passage whose card does not carry the needed fact.
    text = card.get("lesson", "") if delivered else claim.get("lesson", "")
    if not _value_present(text, facet):
        return False
    if facet.get("actor"):
        actor = _normalized(facet["actor"])
        actors = [_normalized(a) for a in claim.get("memory_context", {}).get("actors", [])]
        # Canonical observer vocabulary names the source speaker "user". Resolve
        # only through verified source ownership, never through query wording.
        facet_sources = {_source_id(source_map, s) for s in facet["source_ids"]}
        facet_owners = {_normalized(owner) for source, owner in evidence.get("source_owners", {}).items()
                        if source in facet_sources}
        if facet.get("actor_role") == "source_author":
            return bool(evidence.get("native_document") and facet_owners == {actor})
        if "user" in actors and facet_owners == {actor}:
            actors.append(actor)
        actor_present = actor in actors or bool(re.search(r"(?<!\w)" + re.escape(actor) + r"(?!\w)", _normalized(text)))
        if not actor_present:
            return False
    return True


SOURCE_AUTHOR_FAMILIES = {"exact_discovery", "semantic_paraphrase", "procedures",
                         "values_numbers", "multiple_sources", "time_changes", "long_documents_originals"}


def grading_expected(question, sources):
    """Runtime oracle annotation; never mutate frozen authored files.

    Fixture authors declared actor as source author for these native information
    families. Person/ownership/preference questions and all curated memories keep
    claim-actor semantics. This is an oracle correction, not a retrieval variant.
    """
    import copy
    expected = copy.deepcopy(question["expected"])
    for facet in expected.get("facets", []):
        kinds = {sources[s].get("kind", "unknown") for s in facet["source_ids"] if s in sources}
        if question["family"] in SOURCE_AUTHOR_FAMILIES and kinds == {"native_document"}:
            facet["actor_role"] = "source_author"
        else:
            facet["actor_role"] = "claim_actor"
    return expected


def _covered(expected, cards, evidence, source_map, *, delivered=True):
    facets = expected.get("facets", [])
    covered = [f["id"] for f in facets if any(_facet_supported(f, c, e, source_map, delivered=delivered)
                                               for c, e in zip(cards, evidence))]
    allowed_sources = {s for e in evidence if e.get("allowed") for s in e.get("source_ids", [])}
    complete_sources = any({_source_id(source_map, s) for s in group} <= allowed_sources
                           for group in expected.get("evidence_sets", []))
    provenance_complete = bool(facets) and complete_sources and all(
        any(_facet_provenance(facet, e, source_map) for e in evidence) for facet in facets)
    return {"facets": covered, "missing_facets": [f["id"] for f in facets if f["id"] not in covered],
            "complete": bool(facets) and len(covered) == len(facets) and complete_sources,
            "complete_sources": complete_sources, "provenance_complete": provenance_complete}


def _summarize(rows, requested, *, expected_positive=None, expected_negative=None, expected_original=None):
    positive = [r for r in rows if r["expected_answerable"]]
    negative = [r for r in rows if not r["expected_answerable"]]
    positive_count = len(positive) if expected_positive is None else expected_positive
    negative_count = len(negative) if expected_negative is None else expected_negative
    counts = {"requested_queries": requested, "completed_queries": sum(not r.get("error") for r in rows),
              "positive_queries": positive_count, "negative_queries": negative_count,
              "evaluated_positive_queries": len(positive), "evaluated_negative_queries": len(negative),
              "recalled_at_10": sum(r.get("delivered_recall_at_10", False) for r in positive),
              "supported_answers": sum(r.get("complete_supported_answer", False) for r in positive),
              "correct_abstentions": sum(r.get("correct_abstention", False) for r in negative),
              "delivered_cards": sum(r.get("delivered_cards", 0) for r in rows),
              "precise_cards": sum(r.get("precise_cards", 0) for r in rows),
              "unauthorized_cards": sum(r.get("unauthorized_cards", 0) for r in rows),
              "unauthorized_candidates": sum(r.get("unauthorized_candidates", 0) for r in rows),
              "unauthorized_originals": sum(r.get("unauthorized_originals", 0) for r in rows),
              "delivery_limit_failures": sum(r.get("delivery_limit_failure", False) for r in rows),
              "query_errors": sum(bool(r.get("error")) for r in rows)}
    counts["original_requests"] = (sum(r.get("original_requested", False) for r in rows)
                                    if expected_original is None else expected_original)
    counts["original_delivered"] = sum(r.get("original_delivery", {}).get("pass", False) for r in rows)
    counts["workflow_delivered_items"] = sum(r.get("workflow_delivered_items",r.get("delivered_cards",0)) for r in rows)
    counts["workflow_precise_items"] = sum(r.get("workflow_precise_items",r.get("precise_cards",0)) for r in rows)
    rates = {"recall_at_10": counts["recalled_at_10"] / positive_count if positive_count else 0.,
             "answerable_coverage": counts["supported_answers"] / positive_count if positive_count else 0.,
             "delivered_precision": counts["precise_cards"] / counts["delivered_cards"] if counts["delivered_cards"] else 0.,
             "abstention": counts["correct_abstentions"] / negative_count if negative_count else 0.}
    rates["selected_workflow_precision"] = (counts["workflow_precise_items"] / counts["workflow_delivered_items"]
                                                if counts["workflow_delivered_items"] else 0.)
    gate_checks = {key: rates[key] >= target for key, target in GATES.items()}
    gate_checks.update({"execution_complete": counts["completed_queries"] == requested,
                        "authorization": not counts["unauthorized_cards"] and not counts["unauthorized_candidates"] and not counts["unauthorized_originals"],
                        "delivery_bounds": not counts["delivery_limit_failures"],
                        "original_delivery": counts["original_requests"] == counts["original_delivered"]})
    return {"counts": counts, "rates": rates, "gates": gate_checks, "pass": all(gate_checks.values()),
            "precision_gate_contract": "all memory-search diagnostic cards, including original requests; selected workflow item precision is separate",
            "workflow_item_contract": "original streamed object counts as one item; memory cards count individually; clarification delivers no asserted fact item"}


def grade_observed_question(store, question, result, trace, *, ctx, source_map, sources,
                            evidence_resolver=None, oracle_resolver=None, original_resolver=None, original_workflow_resolver=None,
                            seconds=0., diagnostic=True):
    """Grade an already observed request without search, candidate or model calls."""
    resolver = evidence_resolver or canonical_evidence
    oracle_resolver = oracle_resolver or (authorized_evidence_oracle if evidence_resolver is None else None)
    expected = grading_expected(question, sources)
    row = {"query_id": question["id"], "family": question["family"],
           "expected_answerable": expected["answerable"], "oracle_protocol": ORACLE_PROTOCOL}
    # Ground-truth inspection is outside request timing and diagnostic wrappers.
    cards = result.get("results", [])
    evidence = [resolver(store, ctx, card) for card in cards]
    candidates = []
    seen = set()
    for call in trace["candidate_calls"]:
        for candidate in call.get("items", []):
            key = (candidate["document_id"], candidate["revision_id"])
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    candidate_evidence = [resolver(store, ctx, candidate) for candidate in candidates]
    support = _covered(expected, cards, evidence, source_map)
    candidate_support = {str(limit): _covered(expected, candidates[:limit], candidate_evidence[:limit],
                                               source_map, delivered=False)
                         for limit in (10, 20, 50)}
    oracle = oracle_resolver(store, ctx, expected, source_map) if oracle_resolver else None
    oracle_support = None
    if oracle:
        oracle_evidence = [resolver(store, ctx, c) for c in oracle["documents"]]
        oracle_support = _covered(expected, oracle["documents"], oracle_evidence, source_map, delivered=False)
    expected_sources = {_source_id(source_map, s) for s in expected.get("source_ids", [])}
    precise = []
    for card, provenance in zip(cards, evidence):
        sources_for_card = set(provenance.get("source_ids", []))
        covers_facet = any(_facet_supported(f, card, provenance, source_map) for f in expected.get("facets", []))
        precise.append(bool(provenance.get("allowed") and sources_for_card and
                            sources_for_card <= expected_sources and covers_facet))
    serialized = json.dumps(result, ensure_ascii=True)
    abstention = not result.get("answerable") and not cards
    complete = expected["answerable"] and bool(result.get("answerable")) and support["complete"]
    if not expected["answerable"]:
        cause = "legitimate_abstention" if abstention else "unsupported_selection"
    elif complete:
        cause = None
    elif oracle is not None and set(oracle["source_ids_present"]) != expected_sources:
        cause = "missing_or_incorrect_source"
    elif oracle is not None and not oracle["documents"]:
        cause = "wrong_scope" if oracle["denied_document_count"] else "missing_or_incorrect_source"
    elif oracle_support is not None and not oracle_support["provenance_complete"]:
        cause = "passage_boundary_or_missing_evidence"
    elif not diagnostic or not candidates:
        cause = "unknown" if question.get("as_of") or not diagnostic else "candidate_omission"
    elif not candidate_support["50"]["provenance_complete"]:
        raw_ids = {r["document_id"] for r in trace["channel_rows"]}
        cause = "fusion_cutoff" if raw_ids - {c["document_id"] for c in candidates} else "candidate_omission"
        # Raw omitted IDs alone cannot prove they carry the missing evidence.
        if cause == "fusion_cutoff":
            raw = [{"document_id": r["document_id"], "revision_id": r["revision_id"]}
                   for r in trace["channel_rows"] if r["document_id"] in raw_ids]
            raw_evidence = [resolver(store, ctx, c) for c in raw]
            if not _covered(expected, raw, raw_evidence, source_map, delivered=False)["provenance_complete"]:
                cause = "candidate_omission"
    else:
        cause = "evidence_or_answer_selection_unknown"
    row.update(seconds=seconds, result=result, trace=trace,
               answerable=result.get("answerable", False),
               delivered_recall_at_10=_covered(expected, cards[:10], evidence[:10], source_map)["provenance_complete"],
               candidate_recall_at_10=candidate_support["10"]["provenance_complete"],
               candidate_recall_at_20=candidate_support["20"]["provenance_complete"],
               candidate_recall_at_50=candidate_support["50"]["provenance_complete"],
               candidate_facet_coverage=candidate_support, delivered_facet_coverage=support,
               exact_authorized_oracle=oracle, oracle_facet_coverage=oracle_support,
               complete_supported_answer=complete, correct_abstention=not expected["answerable"] and abstention,
               delivered_cards=len(cards), precise_cards=sum(precise),
               unauthorized_cards=sum(not e.get("allowed") for e in evidence),
               unauthorized_candidates=sum(not e.get("allowed") for e in candidate_evidence),
               serialized_chars=len(serialized),
               delivery_limit_failure=len(serialized) > (1500 if question.get("mode", "explicit") == "automatic" else 4000),
               first_supported_cause=cause,
               timing_semantics="search inclusive; candidate inclusive includes SQL and embedding; oracle inspection excluded")
    row["original_requested"] = bool(expected["answerable"] and expected.get("response_type") == "original")
    if row["original_requested"]:
        # Discovery consumes actual delivered native provenance only. Gold IDs are
        # deliberately not used to pick one original out of unrelated results.
        discovered = {source for e in evidence if e.get("allowed") and e.get("native_document")
                      for source in e.get("source_ids", [])}
        delivery = {"pass": False, "status": "ambiguous_discovery" if len(discovered) > 1 else "source_not_discovered",
                    "discovered_source_count": len(discovered), "gold_used_for_selection": False}
        if len(discovered) == 1:
            if original_resolver is None or original_workflow_resolver is not None:
                delivery["status"] = "memory_discovery_only" if original_workflow_resolver is not None else "fetch_evidence_pending"
            else:
                try:
                    actual = original_resolver(store, ctx, next(iter(discovered)))
                    allowed_keys = {"source_id", "version", "sha256", "bytes", "authorized"}
                    observed = {key: actual.get(key) for key in allowed_keys}
                    delivery.update(observed, status="fetched")
                    delivery["pass"] = bool(actual.get("authorized") is True and
                        actual.get("source_id") == _source_id(source_map, expected["original_source_id"]) and
                        actual.get("sha256") == expected["original_sha256"] and
                        str(actual.get("version")) == str(expected["original_version"]))
                    if not delivery["pass"]:
                        delivery["status"] = "source_sha_version_or_permission_mismatch"
                except Exception as error:
                    delivery.update(status="fetch_rejected", error_type=type(error).__name__)
        row["original_delivery"] = delivery
    row["workflow_route"] = "memory"
    row["workflow_delivered_items"] = row["delivered_cards"]
    row["workflow_precise_items"] = row["precise_cards"]
    row["memory_complete_supported_answer"] = row["complete_supported_answer"]
    row["memory_correct_abstention"] = row["correct_abstention"]
    if original_workflow_resolver is not None:
        # The adapter decides intent from the user request alone. It never receives
        # labels, fixture source IDs, expected spans or the canonical source map.
        request = {"version": "enterprise-local-1", "query": question["query"],
                   "project": question["project"], "mode": question.get("mode", "explicit"),
                   "limit": 8, **question.get("filters", {})}
        if question.get("as_of"):
            request["as_of"] = question["as_of"]
        first = time.monotonic()
        try:
            actual = original_workflow_resolver(store, ctx, request)
            if not isinstance(actual, dict) or type(actual.get("applicable")) is not bool:
                raise ValueError("original_workflow_intent_required")
        except Exception as error:
            actual = {"applicable": False, "status": "workflow_error", "error_type": type(error).__name__}
        row["original_workflow"] = actual
        row["original_workflow_seconds"] = time.monotonic() - first
        row["unauthorized_originals"] = int(bool(actual.get("bytes", 0) and actual.get("authorized") is not True))
        if actual.get("applicable"):
            row["workflow_route"] = "original"
            row["workflow_delivered_items"] = int(bool(actual.get("bytes", 0)))
            row["workflow_precise_items"] = 0
        if row["original_requested"]:
            row["memory_original_delivery"] = row.get("original_delivery")
            good = bool(actual.get("applicable") and actual.get("authorized") is True
                        and actual.get("source_id", actual.get("selected_source_id")) == _source_id(source_map, expected["original_source_id"])
                        and actual.get("sha256") == expected["original_sha256"]
                        and str(actual.get("version")) == str(expected["original_version"])
                        and actual.get("bytes", 0) > 0 and not actual.get("clarification"))
            row["original_delivery"] = {**actual, "pass": good, "gold_used_for_selection": False,
                                        "workflow": "actual request intent -> discovery -> current-policy fetch"}
            # Full original bytes fulfill the original request. Internal anchor
            # facets still diagnose memory search but are not requested content.
            row["complete_supported_answer"] = good
            row["workflow_precise_items"] = int(good)
            row["first_supported_cause"] = None if good else "original_workflow_not_fulfilled"
        elif actual.get("applicable"):
            if not expected["answerable"]:
                row["correct_abstention"] = bool(actual.get("clarification") and not actual.get("bytes", 0)
                                                  and not actual.get("sha256"))
                row["first_supported_cause"] = "legitimate_original_clarification" if row["correct_abstention"] else "unsupported_original_selection"
            else:
                row["complete_supported_answer"] = False
                row["first_supported_cause"] = "wrong_original_intent"
    return row


def evaluate_questions(stores, questions, *, contexts, source_map, sources,
                       diagnostic=True, evidence_resolver=None, oracle_resolver=None, original_resolver=None, original_workflow_resolver=None,
                       max_seconds=900, progress_callback=None):
    """Evaluate authorized evidence sets; report failures instead of hiding them.

    Caller owns subprocess hard termination for blocking native/SQL work. This
    inner deadline checks question boundaries and catches overshooting requests.
    All returned texts/traces must be written privately by the calling runner.
    """
    if not 1 <= max_seconds <= 900:
        raise ValueError("evaluation_deadline_bound")
    resolver = evidence_resolver or canonical_evidence
    oracle_resolver = oracle_resolver or (authorized_evidence_oracle if evidence_resolver is None else None)
    rows, started = [], time.monotonic()
    for question in questions:
        expected = question["expected"]
        row = {"query_id": question["id"], "family": question["family"],
               "expected_answerable": expected["answerable"]}
        if time.monotonic() - started >= max_seconds:
            row["error"] = "evaluation_deadline_before_query"
            rows.append(row)
            break
        store = stores[question["tenant"]]
        ctx = contexts[(question["tenant"], question["principal"])]
        if ctx["tenant"] != question["tenant"] or ctx["actor"] != question["principal"]:
            raise ValueError("evaluation_principal_mismatch")
        if store.answerability_judge is not None:
            raise ValueError("provider_free_evaluation_requires_no_judge")
        request = {"version": "enterprise-local-1", "query": question["query"],
                   "project": question["project"], "mode": question.get("mode", "explicit"), "limit": 8,
                   **question.get("filters", {})}
        if question.get("as_of"):
            request["as_of"] = question["as_of"]
        if question.get("session"):
            request["session"] = question["session"]
        first = time.monotonic()
        try:
            with observe_search(store, diagnostic) as trace:
                result = store.search(ctx, request)
            seconds = time.monotonic() - first
            row = grade_observed_question(store, question, result, trace, ctx=ctx,
                                          source_map=source_map, sources=sources,
                                          evidence_resolver=evidence_resolver, oracle_resolver=oracle_resolver,
                                          original_resolver=original_resolver, original_workflow_resolver=original_workflow_resolver,
                                          seconds=seconds, diagnostic=diagnostic)
            if time.monotonic() - started > max_seconds:
                row["error"] = "evaluation_deadline_overshoot"
        except Exception as error:
            row.update(error=type(error).__name__, error_code=str(error)[:180],
                       seconds=time.monotonic() - first, first_supported_cause="request_error")
        rows.append(row)
        if progress_callback is not None:
            progress_callback({"query_id": question["id"], "completed_queries": len(rows),
                               "requested_queries": len(questions), "seconds": time.monotonic() - started,
                               "row": row})
        if row.get("error"):
            break
    report = _summarize(rows, len(questions),
                        expected_positive=sum(q["expected"]["answerable"] for q in questions),
                        expected_negative=sum(not q["expected"]["answerable"] for q in questions),
                        expected_original=sum(q["expected"]["answerable"] and q["expected"].get("response_type") == "original" for q in questions))
    report.update(rows=rows, seconds=time.monotonic() - started, provider_calls=0,
                  oracle_protocol=ORACLE_PROTOCOL,
                  families={family: _summarize([r for r in rows if r["family"] == family],
                                               sum(q["family"] == family for q in questions),
                                               expected_positive=sum(q["family"] == family and q["expected"]["answerable"] for q in questions),
                                               expected_negative=sum(q["family"] == family and not q["expected"]["answerable"] for q in questions),
                                               expected_original=sum(q["family"] == family and q["expected"]["answerable"] and q["expected"].get("response_type") == "original" for q in questions))
                            for family in sorted({q["family"] for q in questions})},
                  grading="required facet values and actors AND canonical revision/span provenance AND complete acceptable source set",
                  diagnostic=diagnostic)
    return report


def percentile(values, quantile):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)]


def _json_default(value):
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError("private_output_nonfinite_numeric")
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError("private_output_unserializable:" + type(value).__name__)


def private_write(path, value, *, exclusive=False):
    """Serialize first, then publish a complete private file atomically.

    Immutable receipts use an exclusive hard-link publication; mutable progress
    inventories/terminal reservation updates use replacement. A failed encoding
    leaves no destination and cannot damage prior evidence.
    """
    path = Path(path)
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default,
                         allow_nan=False).encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("private_output_directory_permissions")
    if path.is_symlink():
        raise ValueError("private_output_symlink_denied")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConfirmationReservation:
    """An irreversible reservation shared by profiles/builds in one campaign."""
    def __init__(self, marker, metadata):
        self.marker, self.metadata = marker, metadata

    def finish(self, status, **details):
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("confirmation_terminal_status_required")
        existing = json.loads(self.marker.read_text())
        if existing["status"] != "reserved":
            raise ValueError("confirmation_already_terminal")
        private_write(self.marker, {**self.metadata, "status": status, **details})

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        if kind is not None:
            self.finish("interrupted" if issubclass(kind, (KeyboardInterrupt, SystemExit)) else "failed",
                        error_type=kind.__name__)
        elif json.loads(self.marker.read_text())["status"] == "reserved":
            self.finish("failed", error_type="confirmation_not_finalized")


def reserve_confirmation(campaign_root, suite_id, fixture_sha256, expected, actual):
    """Validate immutable metadata first, then reserve once using O_EXCL.

    The caller verifies installed/API identity and corpus originals before calling
    this function. No query may run before its successful return. Any reservation,
    including one abandoned by a killed process, permanently consumes the fixture.
    """
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", suite_id) or not re.fullmatch(r"[0-9a-f]{64}", fixture_sha256):
        raise ValueError("confirmation_key_invalid")
    if any(key not in expected or key not in actual or not expected[key] or expected[key] != actual[key]
           for key in IMMUTABLE_KEYS):
        raise ValueError("confirmation_immutable_preflight_mismatch")
    directory = Path(campaign_root).resolve() / "confirmation-consumed"
    if directory.is_symlink():
        raise ValueError("confirmation_marker_symlink_denied")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise ValueError("confirmation_marker_directory_permissions")
    if list(directory.glob("*--" + fixture_sha256 + ".json")):
        raise ValueError("confirmation_fixture_already_consumed")
    # A fixed SHA-only lock also prevents concurrent reservations using renamed suites.
    marker = directory / ("fixture--" + fixture_sha256 + ".json")
    metadata = {"status": "reserved", "suite_id": suite_id, "fixture_sha256": fixture_sha256,
                "created": time.time(), "immutable": actual, "authored": True,
                "independent_held_out": False}
    try:
        private_write(marker, metadata, exclusive=True)
    except FileExistsError as error:
        raise ValueError("confirmation_fixture_already_consumed") from error
    return ConfirmationReservation(marker, metadata)
