"""Meaningful evaluator failures: provenance, facets, limits and consumption."""
import copy
import hashlib
from contextlib import contextmanager
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vaelius_test_support.hub.retrieval_evaluation import (IMMUTABLE_KEYS, evaluate_questions,
    canonical_evidence, grade_observed_question, observe_search, reserve_confirmation, validate_suite)


TEXT = "Alice saved the dataset at /synthetic/a.csv because headers remain stable."
SOURCES = {"source-a": {"id": "source-a", "tenant": "acme", "scenario": "dataset-a",
                       "split": "development", "text": TEXT}}
SOURCE_MAP = {"source-a": {"source_id": "canonical-a", "revision": "1"}}
CARD = {"id": "document-a", "revision": "revision-a", "title": "Dataset location",
        "lesson": TEXT, "evidence_status": "source_linked_unverified"}
EVIDENCE = {"allowed": True, "source_ids": ["canonical-a"],
            "source_revisions": {"canonical-a": "1"},
            "spans": [{"source_id": "canonical-a", "start": 0, "end": len(TEXT)}],
            "claim": {"lesson": TEXT, "memory_context": {"actors": ["Alice"]}}}


def facet(ident="location", value="/synthetic/a.csv", kind="path"):
    start = TEXT.index(value)
    return {"id": ident, "value": value, "type": kind, "actor": "Alice",
            "source_ids": ["source-a"], "quote": value, "start": start,
            "end": start + len(value), "revision": "1", "aliases": []}


def question(negative=False):
    return {"id": "question-a", "split": "development", "family": "artifact",
            "scenario": "dataset-a", "tenant": "acme", "principal": "alice",
            "query": "Where is my dataset saved?", "project": "maple", "filters": {},
            "expected": {"answerable": not negative, "facets": [] if negative else [facet()],
                         "evidence_sets": [] if negative else [["source-a"]],
                         "source_ids": [] if negative else ["source-a"], "response_type": "evidence"}}


class Cursor:
    def __init__(self, rows):
        self.rows = rows
    def fetchall(self):
        return self.rows
    def fetchone(self):
        return self.rows[0] if self.rows else None


class Database:
    def execute(self, sql, params=()):
        return Cursor([{"document_id": "document-a", "revision_id": "revision-a", "score": .9}])


class State:
    def __init__(self):
        self.db = Database()


class Embedder:
    def embed_queries(self, values):
        return [[1., 0.] for _ in values]


class Store:
    answerability_judge = None
    semantic_embedder = Embedder()
    def __init__(self, result=None, candidates=None, error=None):
        self.result = result if result is not None else {"answerable": True, "results": [copy.deepcopy(CARD)]}
        self.items = candidates if candidates is not None else [{"document_id": "document-a",
            "revision_id": "revision-a", "claim_json": json.dumps(EVIDENCE["claim"]), "channels": ["lexical", "vector"]}]
        self.error = error
        self.search_calls = self.candidate_calls = 0
    @contextmanager
    def open(self):
        yield State()
    def candidates(self, ctx, query, **kwargs):
        self.candidate_calls += 1
        with self.open() as state:
            state.db.execute("SELECT d.document_id,r.revision_id,ts_rank_cd(f.body,q) score", ()).fetchall()
            self.semantic_embedder.embed_queries([query])
            state.db.execute("SELECT d.document_id,r.revision_id,1-(v.embedding <=> ?) score", ()).fetchall()
        return self.items
    def search(self, ctx, value):
        self.search_calls += 1
        self.candidates(ctx, value["query"], limit=20)
        if self.error:
            raise self.error
        return self.result


class EvaluationTests(unittest.TestCase):
    def evaluate(self, store=None, case=None, evidence=None, **kwargs):
        store = store or Store()
        evidence = evidence if evidence is not None else EVIDENCE
        return evaluate_questions({"acme": store}, [case or question()],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map=SOURCE_MAP, sources=SOURCES,
            evidence_resolver=lambda *args: copy.deepcopy(evidence), **kwargs)

    def test_one_search_and_one_candidate_invocation_instrumentation_neutral(self):
        store = Store()
        plain = self.evaluate(Store(), diagnostic=False)
        report = self.evaluate(store)
        self.assertEqual(store.search_calls, 1)
        self.assertEqual(store.candidate_calls, 1)
        self.assertEqual(report["rows"][0]["result"], plain["rows"][0]["result"])
        self.assertEqual(report["counts"]["supported_answers"], 1)
        trace = report["rows"][0]["trace"]
        self.assertEqual({r["channel"] for r in trace["channel_rows"]}, {"lexical", "vector"})
        self.assertEqual(len(trace["embedding"]), 1)
        self.assertNotIn("open", vars(store))
        self.assertNotIn("candidates", vars(store))
        self.assertNotIn("semantic_embedder", vars(store))

    def test_wrong_source_with_matching_literal_fails(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["source_ids"] = ["wrong-source"]
        report = self.evaluate(evidence=evidence)
        self.assertEqual(report["counts"]["supported_answers"], 0)
        self.assertEqual(report["counts"]["precise_cards"], 0)

    def test_wrong_passage_same_original_fails(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["spans"][0]["end"] = TEXT.index("/synthetic")
        report = self.evaluate(evidence=evidence)
        self.assertFalse(report["rows"][0]["candidate_recall_at_20"])
        self.assertFalse(report["rows"][0]["delivered_recall_at_10"])

    def test_wrong_revision_fails(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["source_revisions"]["canonical-a"] = "2"
        self.assertEqual(self.evaluate(evidence=evidence)["counts"]["supported_answers"], 0)

    def test_wrong_actor_value_and_source_are_insufficient(self):
        card = dict(CARD, lesson="Bob saved the dataset at /synthetic/a.csv")
        evidence = copy.deepcopy(EVIDENCE)
        evidence["claim"]["memory_context"]["actors"] = ["Bob"]
        report = self.evaluate(Store({"answerable": True, "results": [card]}), evidence=evidence)
        self.assertFalse(report["rows"][0]["complete_supported_answer"])

    def test_missing_reason_does_not_count_as_complete(self):
        case = question()
        case["expected"]["facets"].append(facet("reason", "headers remain stable", "text"))
        card = dict(CARD, lesson="Alice saved the dataset at /synthetic/a.csv")
        report = self.evaluate(Store({"answerable": True, "results": [card]}), case)
        self.assertTrue(report["rows"][0]["delivered_recall_at_10"])
        self.assertFalse(report["rows"][0]["complete_supported_answer"])
        self.assertEqual(report["rows"][0]["delivered_facet_coverage"]["missing_facets"], ["reason"])

    def test_path_prefix_does_not_count_as_typed_location(self):
        card = dict(CARD, lesson="Alice saved the dataset at /synthetic/a.csv.bak")
        self.assertFalse(self.evaluate(Store({"answerable": True, "results": [card]}))["rows"][0]["complete_supported_answer"])

    def test_required_abstention_is_empty_supported_response(self):
        result = {"answerable": False, "results": []}
        report = self.evaluate(Store(result), question(True))
        self.assertEqual(report["counts"]["correct_abstentions"], 1)
        result["results"] = [CARD]
        self.assertEqual(self.evaluate(Store(result), question(True))["counts"]["correct_abstentions"], 0)

    def test_unauthorized_output_and_candidates_fail_independent_guard(self):
        evidence = dict(EVIDENCE, allowed=False)
        report = self.evaluate(evidence=evidence)
        self.assertEqual(report["counts"]["unauthorized_cards"], 1)
        self.assertEqual(report["counts"]["unauthorized_candidates"], 1)
        self.assertFalse(report["gates"]["authorization"])

    def test_delivery_limit_fails_even_if_content_is_correct(self):
        result = {"answerable": True, "results": [dict(CARD, lesson=TEXT + "x" * 5000)]}
        self.assertFalse(self.evaluate(Store(result))["gates"]["delivery_bounds"])

    def test_request_failure_restores_wrappers_and_is_not_success(self):
        store = Store(error=RuntimeError("injected_failure"))
        report = self.evaluate(store)
        self.assertEqual(report["counts"]["query_errors"], 1)
        self.assertEqual(report["counts"]["completed_queries"], 0)
        self.assertFalse(report["gates"]["execution_complete"])
        self.assertNotIn("open", vars(store))
        self.assertNotIn("candidates", vars(store))

    def test_diagnostic_trace_does_not_run_additional_queries(self):
        store = Store()
        with observe_search(store) as trace:
            result = store.search({"actor": "alice", "tenant": "acme"}, {"query": "dataset"})
        self.assertTrue(result["answerable"])
        self.assertEqual(len(trace["candidate_calls"]), 1)
        self.assertEqual(store.candidate_calls, 1)

    def test_model_judge_denied(self):
        store = Store()
        store.answerability_judge = lambda *args: "NONE"
        with self.assertRaisesRegex(ValueError, "no_judge"):
            self.evaluate(store)

    def test_one_part_of_multisource_answer_does_not_count_as_recalled(self):
        case = question()
        second_facet = facet("reason", "headers remain stable", "text")
        second_facet["source_ids"] = ["source-b"]
        case["expected"]["facets"].append(second_facet)
        case["expected"]["source_ids"].append("source-b")
        case["expected"]["evidence_sets"] = [["source-a", "source-b"]]
        mapping = {**SOURCE_MAP, "source-b": "canonical-b"}
        report = evaluate_questions({"acme": Store()}, [case],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map=mapping, sources=SOURCES, evidence_resolver=lambda *a: EVIDENCE)
        self.assertFalse(report["rows"][0]["candidate_recall_at_20"])
        self.assertFalse(report["rows"][0]["complete_supported_answer"])

    def test_alternative_valid_provenance_is_accepted(self):
        case = question()
        case["expected"]["source_ids"].append("source-b")
        case["expected"]["facets"][0]["source_ids"].append("source-b")
        case["expected"]["evidence_sets"] = [["source-a"], ["source-b"]]
        evidence = copy.deepcopy(EVIDENCE)
        evidence["source_ids"] = ["canonical-b"]
        evidence["source_revisions"] = {"canonical-b": "1"}
        evidence["spans"][0]["source_id"] = "canonical-b"
        report = evaluate_questions({"acme": Store()}, [case],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map={**SOURCE_MAP, "source-b": "canonical-b"}, sources=SOURCES,
            evidence_resolver=lambda *a: evidence)
        self.assertTrue(report["rows"][0]["complete_supported_answer"])

    def test_deadline_overshoot_fails_without_hidden_partial_success(self):
        ticks = iter(i * .1 for i in range(200))
        with patch("vaelius_test_support.hub.retrieval_evaluation.time.monotonic", side_effect=lambda: next(ticks)):
            report = self.evaluate(max_seconds=1)
        self.assertEqual(report["rows"][0]["error"], "evaluation_deadline_overshoot")
        self.assertFalse(report["gates"]["execution_complete"])

    def test_partial_execution_preserves_full_suite_denominators(self):
        first = question()
        second = question(True)
        second["id"] = "question-b"
        report = evaluate_questions({"acme": Store(error=RuntimeError("injected"))}, [first, second],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map=SOURCE_MAP, sources=SOURCES, evidence_resolver=lambda *a: EVIDENCE)
        self.assertEqual(report["counts"]["positive_queries"], 1)
        self.assertEqual(report["counts"]["negative_queries"], 1)
        self.assertEqual(report["counts"]["evaluated_negative_queries"], 0)
        self.assertFalse(report["pass"])

    def test_sentence_punctuation_after_path_is_not_part_of_location(self):
        card = dict(CARD, lesson="Alice saved the dataset at /synthetic/a.csv.")
        self.assertTrue(self.evaluate(Store({"answerable": True, "results": [card]}))["rows"][0]["complete_supported_answer"])

    def test_curated_user_actor_requires_verified_source_owner(self):
        card = dict(CARD, lesson="The dataset is saved at /synthetic/a.csv")
        evidence = copy.deepcopy(EVIDENCE)
        evidence["claim"]["memory_context"]["actors"] = ["user"]
        store = Store({"answerable": True, "results": [card]})
        self.assertFalse(self.evaluate(store, evidence=evidence)["rows"][0]["complete_supported_answer"])
        evidence["source_owners"] = {"canonical-a": "alice"}
        self.assertTrue(self.evaluate(store, evidence=evidence)["rows"][0]["complete_supported_answer"])
        evidence["source_owners"] = {"canonical-a": "bob"}
        self.assertFalse(self.evaluate(store, evidence=evidence)["rows"][0]["complete_supported_answer"])

    def test_native_source_author_does_not_require_author_name_in_fact(self):
        case = question()
        case["family"] = "procedures"
        evidence = copy.deepcopy(EVIDENCE)
        evidence["native_document"] = True
        evidence["source_owners"] = {"canonical-a": "alice"}
        card = dict(CARD, lesson="The dataset is saved at /synthetic/a.csv")
        sources = {"source-a": dict(SOURCES["source-a"], kind="native_document")}
        store = Store({"answerable": True, "results": [card]})
        report = evaluate_questions({"acme": store}, [case],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map=SOURCE_MAP, sources=sources, evidence_resolver=lambda *a: evidence)
        self.assertTrue(report["rows"][0]["complete_supported_answer"])
        self.assertNotIn("actor_role", case["expected"]["facets"][0])

    def test_explicit_coworker_actor_cannot_be_inferred_from_source_author(self):
        case = question()
        case["family"] = "speaker_ownership"
        evidence = copy.deepcopy(EVIDENCE)
        evidence["native_document"] = True
        evidence["source_owners"] = {"canonical-a": "alice"}
        evidence["claim"]["memory_context"]["actors"] = ["Bob"]
        sources = {"source-a": dict(SOURCES["source-a"], kind="native_document")}
        store = Store({"answerable": True, "results": [dict(CARD, lesson="Bob saved the dataset at /synthetic/a.csv")]})
        report = evaluate_questions({"acme": store}, [case],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map=SOURCE_MAP, sources=sources, evidence_resolver=lambda *a: evidence)
        self.assertFalse(report["rows"][0]["complete_supported_answer"])
        self.assertEqual(report["rows"][0]["first_supported_cause"], "evidence_or_answer_selection_unknown")

    def test_grading_observed_request_never_searches_or_embeds(self):
        store = Store()
        trace = {"candidate_calls": [{"items": store.items}], "channel_rows": [], "sql": [], "embedding": []}
        row = grade_observed_question(store, question(), store.result, trace,
            ctx={"tenant": "acme", "actor": "alice"}, source_map=SOURCE_MAP, sources=SOURCES,
            evidence_resolver=lambda *a: EVIDENCE, seconds=.1)
        self.assertTrue(row["complete_supported_answer"])
        self.assertEqual(store.search_calls, 0)
        self.assertEqual(store.candidate_calls, 0)
        self.assertEqual(row["seconds"], .1)

    def original_case(self):
        case = question()
        case["expected"].update(response_type="original", original_source_id="source-a", original_version="1",
                                original_sha256=hashlib.sha256(TEXT.encode()).hexdigest())
        return case

    def test_original_bytes_require_actual_unique_discovery_and_fetch(self):
        evidence = dict(EVIDENCE, native_document=True)
        calls = []
        def fetched(store, ctx, ident):
            calls.append(ident)
            return {"source_id": ident, "version": "1", "sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                    "bytes": len(TEXT.encode()), "authorized": True}
        report = self.evaluate(case=self.original_case(), evidence=evidence, original_resolver=fetched)
        self.assertEqual(calls, ["canonical-a"])
        self.assertEqual(report["counts"]["original_requests"], 1)
        self.assertEqual(report["counts"]["original_delivered"], 1)
        self.assertTrue(report["gates"]["original_delivery"])

    def test_original_pending_does_not_change_search_coverage_or_fake_delivery(self):
        report = self.evaluate(case=self.original_case(), evidence=dict(EVIDENCE, native_document=True))
        self.assertTrue(report["rows"][0]["complete_supported_answer"])
        self.assertEqual(report["rows"][0]["original_delivery"]["status"], "fetch_evidence_pending")
        self.assertFalse(report["gates"]["original_delivery"])

    def test_original_gold_source_cannot_choose_among_unrelated_delivered_sources(self):
        store = Store({"answerable": True, "results": [CARD, dict(CARD, id="document-b")]})
        evidence_a = dict(EVIDENCE, native_document=True)
        evidence_b = copy.deepcopy(evidence_a)
        evidence_b["source_ids"] = ["canonical-b"]
        calls = []
        report = evaluate_questions({"acme": store}, [self.original_case()],
            contexts={("acme", "alice"): {"tenant": "acme", "actor": "alice"}},
            source_map=SOURCE_MAP, sources=SOURCES,
            evidence_resolver=lambda store, ctx, card: evidence_b if card.get("id") == "document-b" else evidence_a,
            original_resolver=lambda *args: calls.append(args))
        self.assertFalse(calls)
        self.assertEqual(report["rows"][0]["original_delivery"]["status"], "ambiguous_discovery")

    def test_original_version_or_permission_mismatch_fails(self):
        evidence = dict(EVIDENCE, native_document=True)
        def wrong_version(*args):
            return {"source_id": "canonical-a", "version": "2", "sha256": hashlib.sha256(TEXT.encode()).hexdigest(), "authorized": True}
        report = self.evaluate(case=self.original_case(), evidence=evidence, original_resolver=wrong_version)
        self.assertFalse(report["gates"]["original_delivery"])
        def denied(*args):
            raise PermissionError("synthetic current policy revoked")
        report = self.evaluate(case=self.original_case(), evidence=evidence, original_resolver=denied)
        self.assertEqual(report["rows"][0]["original_delivery"]["status"], "fetch_rejected")
        self.assertFalse(report["gates"]["original_delivery"])


class SuiteValidationTests(unittest.TestCase):
    def test_valid_source_labels(self):
        self.assertEqual(validate_suite(SOURCES, [question()])["questions"], 1)

    def test_absent_source_and_unsupported_quote_rejected(self):
        case = question()
        case["expected"]["source_ids"] = ["missing"]
        with self.assertRaises(ValueError):
            validate_suite(SOURCES, [case])
        case = question()
        case["expected"]["facets"][0]["quote"] = "invented answer"
        with self.assertRaisesRegex(ValueError, "quote_not_source"):
            validate_suite(SOURCES, [case])

    def test_split_source_and_scenario_overlap_rejected(self):
        with self.assertRaisesRegex(ValueError, "scenario_split_overlap"):
            validate_suite(SOURCES, [question()], other_questions=[question()])
        case = question()
        case["split"] = "confirmation"
        with self.assertRaisesRegex(ValueError, "source_split_mismatch"):
            validate_suite(SOURCES, [case])

    def test_positive_without_facet_or_principal_rejected(self):
        case = question()
        case["expected"]["facets"] = []
        with self.assertRaisesRegex(ValueError, "complete_evidence"):
            validate_suite(SOURCES, [case])
        with self.assertRaisesRegex(ValueError, "invalid_principal"):
            validate_suite(SOURCES, [question()], principals={("acme", "bob")})


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.expected = {key: key + "-frozen" for key in IMMUTABLE_KEYS}
        self.sha = "a" * 64
    def tearDown(self):
        self.tmp.cleanup()
    def reserve(self, **kwargs):
        return reserve_confirmation(self.path, kwargs.pop("suite", "retrieval-v1"), self.sha,
                                    kwargs.pop("expected", self.expected),
                                    kwargs.pop("actual", self.expected))

    def test_stale_metadata_rejected_before_consumption(self):
        actual = dict(self.expected, model_key="changed")
        with self.assertRaisesRegex(ValueError, "preflight_mismatch"):
            self.reserve(actual=actual)
        self.assertFalse((self.path / "confirmation-consumed").exists())
        self.reserve().finish("completed", pass_gates=False)

    def test_completed_or_failed_reservation_consumed_across_profiles_and_suite_names(self):
        self.reserve().finish("failed", completed_queries=1)
        with self.assertRaisesRegex(ValueError, "already_consumed"):
            self.reserve(suite="renamed-v2")

    def test_interrupted_context_consumes_and_preserves_terminal_status(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.reserve():
                raise KeyboardInterrupt()
        marker = next((self.path / "confirmation-consumed").glob("*.json"))
        self.assertEqual(json.loads(marker.read_text())["status"], "interrupted")
        with self.assertRaisesRegex(ValueError, "already_consumed"):
            self.reserve()

    def test_unfinalized_context_consumes_and_gate_failure_not_replayable(self):
        with self.reserve():
            pass
        with self.assertRaisesRegex(ValueError, "already_consumed"):
            self.reserve()

    def test_abandoned_reservation_rejects_concurrent_reservation(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "already_consumed"):
            self.reserve()

    def test_permissive_marker_directory_rejected(self):
        directory = self.path / "confirmation-consumed"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory_permissions"):
            self.reserve()


from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, "explicit private PostgreSQL services manifest required")
class CanonicalEvaluationPostgres(PostgresFixture, unittest.TestCase):
    def setUp(self):
        from agenthub.cloud_runtime import CloudStore
        from agenthub.document_ingest import DocumentStore
        from agenthub.source_objects import FileSourceObjects
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, "acme")
        self.store.hybrid_enabled = False
        self.store.enroll_connection(self.ctx, "evaluator-native", "synthetic", "maple", ["document"])
        self.documents = DocumentStore(self.store, FileSourceObjects(Path(self.temp.name) / "objects"))
        self.source = self.documents.ingest(self.ctx, "evaluator-native", "a", "1", "a.md",
                                           io.BytesIO(TEXT.encode()), title="Dataset location")["source_id"]

    def tearDown(self):
        self.postgres_teardown()

    def test_real_native_spans_authorization_and_single_search(self):
        case = question()
        case["query"] = "Where is Alice dataset saved?"
        report = evaluate_questions({"acme": self.store}, [case],
            contexts={("acme", "alice"): self.ctx}, source_map={"source-a": self.source}, sources=SOURCES)
        self.assertNotIn("error", report["rows"][0], report["rows"][0])
        self.assertTrue(report["rows"][0]["complete_supported_answer"], report["rows"][0])
        self.assertEqual(len(report["rows"][0]["trace"]["candidate_calls"]), 1)
        self.assertTrue(report["rows"][0]["exact_authorized_oracle"]["documents"])
        card = report["rows"][0]["result"]["results"][0]
        denied = canonical_evidence(self.store, self.store.authenticate(self.tokens["bob"]), card)
        self.assertFalse(denied["allowed"])

    def test_current_withdrawal_rejects_earlier_delivered_card(self):
        from agentclient.enterprise_contract import VERSION
        result = self.store.search(self.ctx, {"version": VERSION, "query": "Where is Alice dataset saved?", "project": "maple"})
        self.assertTrue(result["results"])
        card = result["results"][0]
        self.store.lifecycle(self.ctx, {"version": VERSION, "target_id": self.source, "operation": "withdraw",
                                       "expected_revision": "1", "idempotency_key": "evaluation-withdraw",
                                       "reason": "synthetic owner withdrawal"})
        self.assertFalse(canonical_evidence(self.store, self.ctx, card)["allowed"])

    def test_native_author_provenance_without_author_in_quantity(self):
        text = "The operating allocation is 185000 credits."
        source = self.documents.ingest(self.ctx, "evaluator-native", "budget", "1", "budget.md",
                                       io.BytesIO(text.encode()), title="Operating allocation")["source_id"]
        case = question()
        case["family"] = "values_numbers"
        case["query"] = "What is the operating allocation?"
        value = "185000 credits"
        start = text.index(value)
        case["expected"]["facets"] = [{"id": "amount", "value": value, "type": "quantity", "actor": "alice",
            "source_ids": ["source-a"], "quote": value, "start": start, "end": start + len(value), "revision": "1"}]
        sources = {"source-a": {**SOURCES["source-a"], "text": text, "kind": "native_document"}}
        report = evaluate_questions({"acme": self.store}, [case],
            contexts={("acme", "alice"): self.ctx}, source_map={"source-a": source}, sources=sources)
        self.assertNotIn("error", report["rows"][0], report["rows"][0])
        self.assertTrue(report["rows"][0]["complete_supported_answer"], report["rows"][0])

    def test_original_delivery_fetches_actual_discovered_individual_object(self):
        case = question()
        case["query"] = "Where is Alice dataset saved?"
        case["expected"].update(response_type="original", original_source_id="source-a",
                                original_version="1", original_sha256=hashlib.sha256(TEXT.encode()).hexdigest())
        fetched = []
        def original(store, ctx, source):
            fetched.append(source)
            stream, headers = self.documents.fetch(ctx, source)
            with stream:
                raw = stream.read()
            metadata = self.documents.describe(ctx, source)
            return {"source_id": metadata["source_id"], "version": metadata["version"],
                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "authorized": True}
        report = evaluate_questions({"acme": self.store}, [case],
            contexts={("acme", "alice"): self.ctx}, source_map={"source-a": self.source}, sources=SOURCES,
            original_resolver=original)
        self.assertEqual(fetched, [self.source])
        self.assertEqual(report["counts"]["original_delivered"], 1)
        self.assertTrue(report["gates"]["original_delivery"])

class OriginalWorkflowContract(unittest.TestCase):
    evaluate = EvaluationTests.evaluate
    def original_case(self):
        case=question();case['query']='Give me the signed original Dataset location.'
        case['expected'].update(response_type='original',original_source_id='source-a',original_version='1',
                               original_sha256=hashlib.sha256(TEXT.encode()).hexdigest())
        return case

    def test_discovery_receives_only_actual_request_and_fetch_fulfills_without_memory_fact(self):
        received=[]
        def workflow(store,ctx,request):
            received.append(request)
            self.assertEqual(set(request),{'version','query','project','mode','limit'})
            self.assertNotIn('expected',request)
            return {'applicable':True,'source_id':'canonical-a','version':'1','sha256':hashlib.sha256(TEXT.encode()).hexdigest(),
                    'bytes':len(TEXT.encode()),'authorized':True,'status':'fetched'}
        case=self.original_case();store=Store(result={'answerable':False,'results':[]})
        report=self.evaluate(store,case=case,original_workflow_resolver=workflow)
        self.assertEqual(len(received),1);self.assertEqual(store.search_calls,1)
        self.assertTrue(report['rows'][0]['complete_supported_answer'])
        self.assertFalse(report['rows'][0]['memory_complete_supported_answer'])
        self.assertFalse(report['rows'][0]['delivered_recall_at_10'])
        self.assertEqual(report['counts']['original_delivered'],1)

    def test_workflow_does_not_select_gold_among_multiple_unrelated_originals(self):
        def ambiguous(*args):return {'applicable':True,'clarification':True,'status':'ambiguous','discovered_source_ids':['canonical-a','other']}
        report=self.evaluate(case=self.original_case(),original_workflow_resolver=ambiguous)
        self.assertFalse(report['rows'][0]['original_delivery']['pass'])
        self.assertFalse(report['rows'][0]['complete_supported_answer'])
        self.assertTrue(report['rows'][0]['memory_complete_supported_answer'])

    def test_wrong_version_permission_or_sha_cannot_fulfill_original(self):
        actual={'applicable':True,'source_id':'canonical-a','version':'1','sha256':hashlib.sha256(TEXT.encode()).hexdigest(),
                'bytes':len(TEXT),'authorized':True}
        for field,value in [('version','2'),('sha256','0'*64),('authorized',False),('source_id','other')]:
            with self.subTest(field=field):
                report=self.evaluate(case=self.original_case(),original_workflow_resolver=lambda *args:{**actual,field:value})
                self.assertFalse(report['rows'][0]['complete_supported_answer'])
                self.assertFalse(report['gates']['original_delivery'])
                if field=='authorized':self.assertFalse(report['gates']['authorization'])

    def test_unavailable_original_negative_uses_explicit_clarification_keeps_noisy_cards(self):
        case=question(negative=True);case['query']='Give me the never-signed 2028 Dataset original.'
        case['expected']['response_type']='clarification'
        report=self.evaluate(case=case,original_workflow_resolver=lambda *args:{'applicable':True,'clarification':True,'status':'requested_version_unavailable'})
        row=report['rows'][0]
        self.assertTrue(row['correct_abstention']);self.assertFalse(row['memory_correct_abstention'])
        self.assertEqual(report['counts']['delivered_cards'],1);self.assertEqual(report['counts']['precise_cards'],0)

    def test_semantic_or_location_request_is_not_dropped_based_on_frozen_labels(self):
        received=[]
        def no_original(store,ctx,request):received.append(request['query']);return {'applicable':False}
        report=self.evaluate(original_workflow_resolver=no_original)
        self.assertEqual(received,['Where is my dataset saved?'])
        self.assertTrue(report['rows'][0]['complete_supported_answer'])

    def test_workflow_failure_does_not_erase_memory_diagnostics_or_falsely_pass(self):
        def broken(*args):raise RuntimeError('simulated document transport failure')
        report=self.evaluate(case=self.original_case(),original_workflow_resolver=broken)
        self.assertFalse(report['rows'][0]['complete_supported_answer'])
        self.assertTrue(report['rows'][0]['memory_complete_supported_answer'])
        self.assertEqual(report['rows'][0]['original_workflow']['status'],'workflow_error')

    def test_progress_callback_retains_completed_rows_without_additional_search(self):
        rows=[];store=Store()
        report=self.evaluate(store,progress_callback=rows.append)
        self.assertEqual(store.search_calls,1)
        self.assertEqual(rows[0]['row']['result'],report['rows'][0]['result'])
        self.assertEqual(rows[0]['completed_queries'],1)


if __name__ == "__main__":
    unittest.main()
