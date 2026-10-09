"""Strict, versioned envelopes for the private enterprise-local service.

Authority is never accepted from these envelopes. The service derives tenant,
principal, enrollment, delegation and readable scopes from the credential.
"""
from __future__ import annotations

import json

VERSION = "enterprise-local-1"
MAX_BODY_BYTES = 262_144
MAX_BATCH_EVENTS = 32
MAX_QUERY_CHARS = 2_000
MAX_AUTOMATIC_CHARS = 1_500
MAX_RESPONSE_CHARS = 4_000
EVENT_KINDS = {"UserPromptSubmit", "Stop", "PostToolUse", "SessionStart", "gap"}
VISIBILITIES = {"private", "team", "organization"}


def _object(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - (set(required) | set(optional)) or not set(required) <= set(value):
        raise ValueError("invalid_envelope")
    if value.get("version") != VERSION:
        raise ValueError("unsupported_enterprise_version")
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        raise ValueError("enterprise_payload_too_large")
    return value


def _text(value, maximum, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise ValueError("invalid_enterprise_text")
    return value


def validate_source(value):
    """Validate a source event without granting its claimed actor any authority."""
    _object(value, {"version", "external_id", "session", "turn", "project", "kind", "body", "occurred_at"},
            {"speaker", "tool_name", "source_role", "capture_id", "event_fields", "response_shape", "visibility", "raw_visibility", "outcome", "exit_code", "precision", "timezone", "excluded_reason"})
    for field, size in (("external_id", 128), ("session", 256), ("turn", 128), ("project", 256)):
        _text(value[field], size)
    _text(value["body"], 180_000, allow_empty=value["kind"] == "gap")
    if value["kind"] not in EVENT_KINDS or value.get("visibility", "private") not in VISIBILITIES:
        raise ValueError("invalid_source_kind_or_visibility")
    if value.get("raw_visibility", "private") != "private":
        raise ValueError("raw_release_requires_review")
    if not isinstance(value["occurred_at"], (int, float, str)) or isinstance(value["occurred_at"], bool):
        raise ValueError("invalid_source_time")
    if "exit_code" in value and (type(value["exit_code"]) is not int or abs(value["exit_code"])>65535):
        raise ValueError("invalid_exit_code")
    if "event_fields" in value and (not isinstance(value["event_fields"],list)
            or len(value["event_fields"])>128):
        raise ValueError('invalid_event_fields')
    for field in ("speaker", "tool_name", "source_role", "capture_id", "precision", "timezone", "excluded_reason"):
        if field in value:
            _text(value[field], 256, allow_empty=True)
    return value


def validate_search(value):
    _object(value, {"version", "query"}, {"project", "limit", "mode", "as_of", "time_mode",
                                           "session", "boundary_id"})
    _text(value["query"], MAX_QUERY_CHARS)
    if "project" in value: _text(value["project"], 256)
    if "session" in value: _text(value["session"], 256)
    if "as_of" in value:_text(value["as_of"],40)
    if "time_mode" in value:
        if value["time_mode"] not in {"current", "effective_at", "known_at"}:
            raise ValueError("invalid_temporal_mode")
        if value["time_mode"] == "known_at" and not value.get("as_of"):
            raise ValueError("known_at_requires_as_of")
        if value["time_mode"] == "current" and value.get("as_of"):
            raise ValueError("current_conflicts_with_as_of")
    if "boundary_id" in value:_text(value["boundary_id"],128)
    if "limit" in value and (type(value["limit"]) is not int or not 1 <= value["limit"] <= 20):
        raise ValueError("invalid_search_limit")
    if value.get("mode", "explicit") not in {"explicit", "automatic"}:
        raise ValueError("invalid_search_mode")
    return value


def validate_lifecycle(value):
    _object(value, {"version", "target_id", "expected_revision", "idempotency_key", "reason", "operation"},
            {"replacement", "visibility"})
    for field in ("target_id", "expected_revision", "idempotency_key", "reason"):
        _text(value[field], 512)
    if value["operation"] not in {"correct", "withdraw", "delete", "policy"}:
        raise ValueError("invalid_lifecycle_operation")
    if "visibility" in value and value["visibility"] not in VISIBILITIES:
        raise ValueError("invalid_visibility")
    if value["operation"]=="correct":
        replacement=value.get("replacement")
        if not isinstance(replacement,dict) or set(replacement)!={"source","title","lesson"}:
            raise ValueError("invalid_correction")
        validate_source(replacement["source"])
        _text(replacement["title"],180)
        _text(replacement["lesson"],1200)
    return value


def validate_receipt(value):
    _object(value,{"version","session","cards","serialized_chars"},{"request_id"})
    _text(value["session"],256)
    if "request_id" in value:_text(value["request_id"],128)
    if (type(value["serialized_chars"]) is not int or
        not 0<=value["serialized_chars"]<=MAX_AUTOMATIC_CHARS):
        raise ValueError("invalid_receipt_size")
    if not isinstance(value["cards"],list) or len(value["cards"])>3:
        raise ValueError("invalid_receipt_cards")
    for card in value["cards"]:
        if not isinstance(card,dict) or set(card)!={"id","revision"}:
            raise ValueError("invalid_receipt_card")
        _text(card["id"],128);_text(card["revision"],128)
    return value


def validate_boundary(value):
    _object(value,{"version","event"})
    event=value["event"]
    if not isinstance(event,dict) or set(event)!={"hook_event_name","trigger","session_id","turn_id"}:
        raise ValueError("invalid_boundary")
    if event["hook_event_name"]!="PostCompact" or event["trigger"] not in {"auto","manual"}:
        raise ValueError("invalid_boundary")
    _text(event["session_id"],256);_text(event["turn_id"],128)
    return value


def validate_response(path, value):
    """Reject malformed or expanded service envelopes before client delivery."""
    if not isinstance(path,str) or not path.startswith('/enterprise/v1/'):
        raise ValueError('invalid_enterprise_response_path')
    if not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=True))>524288:
        raise ValueError('invalid_enterprise_response')
    route=path.removeprefix('/enterprise/v1/').split('/',1)[0]
    spec={
        'status':({'service','version','tenant','principal','acting_for','enrollment','actions','schema'},set()),
        'processing-status':({'tenant','principal','visible_active_sources','jobs','scope_count'},set()),
        'search':({'results','answerable'},{'coverage_gaps'}),
        'sources':(({'id','body','kind','occurred_at','precision','timezone','visibility','source_version'}
                    if '/' in path.removeprefix('/enterprise/v1/sources') else
                    {'source_id','disposition'}),set()),
        'documents':({'id','revision','claim'},set()),
        'timeline':({'id','revision','episodes','coverage_gaps','has_more'},set()),
        'lifecycle':({'target_id','operation','source_version','dependent_documents_blocked'},
                     {'replacement_source_id'}),
        'boundary':({'applied','epoch'},set()),
        'receipts':({'accepted','status','host_confirmed','observed_used','epoch'},set()),
        'reviewed-notes':({'action','document_id','revision_id'},
                          {'related_candidates','temporal','previous_revision_id'}),
        'explain':({'kind','id','active','policy_version','eligible_principals'},
                   {'raw_readers','visibility','dependency_count'}),
    }.get(route)
    if spec is None or not spec[0]<=set(value) or set(value)-spec[0]-spec[1]:
        raise ValueError('invalid_enterprise_response_fields')
    if route=='status':
        if value['version']!=VERSION or not isinstance(value['actions'],list) or type(value['schema']) is not int:
            raise ValueError('invalid_enterprise_status')
    elif route=='search':
        if type(value['answerable']) is not bool or not isinstance(value['results'],list) or len(value['results'])>20:
            raise ValueError('invalid_enterprise_search_response')
        for card in value['results']:
            if (not isinstance(card,dict) or set(card)!={'id','revision','title','lesson','evidence_status'}
                    or any(not isinstance(card[key],str) or not card[key] for key in ('id','revision'))
                    or len(json.dumps(card,ensure_ascii=True))>1300):
                raise ValueError('invalid_enterprise_card')
        if bool(value['results'])!=value['answerable']:
            raise ValueError('invalid_enterprise_answerability')
    elif route=='documents' and not isinstance(value['claim'],dict):
        raise ValueError('invalid_enterprise_document')
    elif route=='timeline' and (not isinstance(value['episodes'],list) or
                                not isinstance(value['coverage_gaps'],list)):
        raise ValueError('invalid_enterprise_timeline')
    return value
