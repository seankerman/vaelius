"""Versioned, private metadata for curated observations and selection decisions."""
from __future__ import annotations


import json
import time


SCHEMA_VERSION = 1
DOMAINS = {"software_debugging", "research_synthesis", "writing_documents",
           "data_analysis", "design_product", "planning_decisions", "unknown"}
DECISION_REASONS = {
    "reusable_finding", "supported_negative_result", "durable_decision_or_constraint",
    "new_applicability", "new_independent_support", "preserved_disagreement",
    "routine_status_without_new_evidence", "task_management_chatter", "same_lineage_repetition",
    "unsupported_claim_or_promise", "insufficient_context", "explicit_exclusion",
    "recursion_from_injected_memory", "no_durable_learning",
}


def initialize(db):
    db.require_schema()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence_status(observation):
    return {"observed_success": "reviewed_observed_execution",
            "reported_success": "reported_only", "failed": "observed_failure",
            "unknown": "unknown"}.get(observation.get("outcome"), "unknown")


def metadata_row(memory_id, observation, decision_reason="reusable_finding", now=None):
    domain=observation.get("domain", "unknown")
    if domain not in DOMAINS:domain="unknown"
    evidence=observation.get("evidence", [])
    return (memory_id,SCHEMA_VERSION,domain,observation.get("knowledge_type", "unknown"),
        _json(observation.get("subjects", [])),_json(observation.get("tags", [])),
        observation.get("applicability", ""),_json(observation.get("applicability_constraints", {})),
        evidence_status(observation),decision_reason,
        _json(sorted({ref.get("segment_id", "") for ref in evidence if ref.get("segment_id")})),
        now or time.time())


def store_metadata(db, memory_id, observation, decision_reason="reusable_finding"):
    db.execute("INSERT INTO memory_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET schema_version=excluded.schema_version,domain=excluded.domain,knowledge_type=excluded.knowledge_type,subjects=excluded.subjects,tags=excluded.tags,applicability_text=excluded.applicability_text,applicability_constraints=excluded.applicability_constraints,evidence_status=excluded.evidence_status,decision_reason=excluded.decision_reason,source_segment_ids=excluded.source_segment_ids,updated=excluded.updated",
               metadata_row(memory_id,observation,decision_reason))
