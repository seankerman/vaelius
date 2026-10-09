"""Source packets and evidence-backed resolution for the continuous observer."""
from __future__ import annotations

import hashlib
import json
import re

from agenthub.processing.harness import object_schema


TEXT = {"type": "string"}
APPLICABILITY_FIELDS = (
    "project_scope", "platforms", "versions", "date_ranges", "units",
    "preconditions", "exclusions",
)





OPERATIONS = [
    "CREATE", "SUPPORT", "CORRECT", "SUPERSEDE", "CONTRADICT",
    "CONDITIONAL_VARIANT", "RELATED", "IGNORE",
]
RESOLUTION_SCHEMA = object_schema({
    "candidate_key": TEXT,
    "operation": {"type": "string", "enum": OPERATIONS},
    "target_artifact_id": TEXT,
    "reason": {"type": "string", "enum": [
        "new_claim", "compatible_support", "same_subject_newer_correction",
        "obsolete_claim_replaced", "same_conditions_incompatible_outcome",
        "different_explicit_conditions", "associated_but_distinct",
        "unsupported_or_redundant", "not_durable_or_not_atomic",
    ]},
})


RESOLUTION_PROMPT = """Resolve one validated learning candidate against the supplied bounded
set of active private knowledge artifacts. This is a fresh classification call, not
a continuation of the curator conversation. Similar wording never authorizes a
merge. Compare the atomic claim, evidence status, subject, and every applicability
condition.

CREATE when no supplied artifact represents the claim. SUPPORT adds compatible
independent evidence without changing meaning. CORRECT replaces a factual error with
newer supported evidence. SUPERSEDE replaces an older decision or procedure that is
no longer active. CONTRADICT preserves incompatible evidence under the same explicit
conditions. Opposing reports about the same actor, platform, time and scope are
CONTRADICT, even when neither report is independently verified. Do not invent a
different condition merely because the outcomes disagree. CONDITIONAL_VARIANT
requires an explicitly different actor, platform, time or applicability condition. RELATED links useful but distinct claims. IGNORE when unsupported,
truly redundant, a transient task-status snapshot, or a bundle of multiple independent
findings rather than one durable atomic claim. Use reason not_durable_or_not_atomic for
the latter two cases. Also IGNORE general advice, recommendations, capabilities,
market facts, or service descriptions supported only by the assistant's final answer;
they require source evidence or explicit user adoption. Never select a target outside the supplied artifacts. Use an empty
target_artifact_id for CREATE or IGNORE. Return only JSON."""

MEMORY_RESOLUTION_RULE = '''A candidate with memory_record has already passed original-evidence and durability validation.
Its who/what/where/why facets describe one memory and are not a non-atomic bundle.
A saved artifact, reported current location, personal decision, or completed activity
is durable organizational memory, even when it describes this session's work.
SUPPORT must preserve every structured facet, location, speaker, attribution and state;
adding a new location to a rationale is new knowledge, not support for that rationale.
IGNORE is available only for an exact structured duplicate among the supplied memories.
Use CREATE for distinct source-backed memories; preserve uncertainty and reported status.'''


def memory_equivalent(candidate, artifact):
    current=candidate.get('memory_record');prior=artifact.get('claim',{}).get('memory_context')
    if not current or not prior:return False
    fields=('subject','artifact_name','location','reason_actor','reason_quote','state')
    return (all(current.get(k)==prior.get(k) for k in fields)
        and set(current.get('facets',[]))==set(prior.get('facets',[]))
        and set(current.get('actors',[]))==set(prior.get('actors',[])))



class EpisodeError(ValueError):
    pass


def _span_id(source_id, start, end):
    raw = f"{source_id}:{start}:{end}".encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def _spans(source_id, body, width=480):
    result = []
    start = 0
    while start < len(body):
        end = min(len(body), start + width)
        if end < len(body):
            boundary = max(body.rfind("\n", start + 120, end),
                           body.rfind(". ", start + 120, end),
                           body.rfind(" ", start + 120, end))
            if boundary >= start + 120:
                end = boundary + 1
        text = body[start:end]
        if text.strip():
            result.append({"span_id": _span_id(source_id, start, end),
                           "start": start, "end": end, "text": text})
        start = end
    return result


def _applicability_options(events):
    """Offer only deterministic exact phrases; free-text claim wording stays separate."""
    text = "\n".join(span["text"] for event in events for span in event["spans"])
    patterns = {
        "platforms": r"\b(?:macOS(?:\s+\d+(?:\.\d+)*)?|Linux|Windows(?:\s+\d+)?|iOS(?:\s+\d+(?:\.\d+)*)?|Android(?:\s+\d+(?:\.\d+)*)?|Apple Silicon)\b",
        "versions": r"\b(?:version\s+|v)(?:\d+)(?:\.\d+){0,3}\b",
        "date_ranges": r"\b\d{4}-\d{2}-\d{2}(?:\s+(?:through|to)\s+\d{4}-\d{2}-\d{2})?\b",
        "units": r"\b\d+(?:\.\d+)?\s+(?:milliseconds?|seconds?|minutes?|hours?|days?|bytes?|KB|MB|GB|cases?|runs?|restarts?|records?|workers?|calls?)\b",
    }
    result = {key: [] for key in APPLICABILITY_FIELDS}
    for field, pattern in patterns.items():
        result[field] = list(dict.fromkeys(match.group(0) for match in re.finditer(pattern, text, re.I)))[:16]
    return result


def prepare_episode(sources, related_artifacts=(), *, max_events=80, max_chars=48_000,
                    media_policy=None):
    """Create one bounded episode view or fail without treating a fragment as complete."""
    from agenthub.processing.media_projection import validate_policy, project_media
    validate_policy(media_policy)
    if not isinstance(sources, list) or not sources:
        raise EpisodeError("empty_episode")
    ordered = sorted(sources, key=lambda row: (row.get('source_order') if type(row.get('source_order')) is int else row.get("created", 0), row.get("id", "")))
    identity = {(row.get("project"), row.get("session"), row.get("turn")) for row in ordered}
    if len(identity) != 1:
        raise EpisodeError("mixed_episode_identity")
    if any(not isinstance(row.get("id"), str) or not isinstance(row.get("body", ""), str)
           for row in ordered):
        raise EpisodeError("invalid_episode_source")
    project, session, turn = next(iter(identity))
    native_identity = isinstance(project, str) and project.startswith('enterprise:')
    seen_bodies = {}
    events = []
    deterministic_receipts = []
    total_chars = 0
    for position, row in enumerate(ordered):
        body = row.get("body", "")
        role = row.get("source_role") or "episode_evidence"
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        if not native_identity and body.strip() and body_hash in seen_bodies:
            deterministic_receipts.append({"event_id": row["id"], "reason": "duplicate_transport"})
            continue
        if body.strip():
            seen_bodies[body_hash] = row["id"]
        if role in {"context_transfer", "retrieved_memory"}:
            deterministic_receipts.append({"event_id": row["id"], "reason": "context_transfer"})
            continue
        media=[]
        if media_policy:
            ranges,media=project_media(body)
            parts=[]
            for start,end in ranges:
                for part in _spans(row['id'],body[start:end]):
                    part['start']+=start;part['end']+=start
                    part['span_id']=_span_id(row['id'],part['start'],part['end'])
                    parts.append(part)
        else:
            parts = _spans(row["id"], body)
        total_chars += sum(len(part["text"]) for part in parts)
        events.append({
            "event_id": row["id"], "position": position, "kind": row.get("kind", ""),
            "tool_name": row.get("tool_name", ""), "exit_code": row.get("exit_code"),
            **{key: row[key] for key in ('role','channel','actor','call_id','parent_id','native_kind') if key in row},
            **({'source_order':row['source_order']} if 'source_order' in row else {}),
            **({'execution_success':row['execution_success']} if 'execution_success' in row else {}),
            **({"occurred_at":row["occurred_at"],
                "occurred_precision":row.get("occurred_precision","unknown"),
                "occurred_timezone":row.get("occurred_timezone")}
               if "occurred_at" in row else {}),
            "spans": parts,
            **({'media_references':media} if media_policy else {}),
        })
    if len(events) > max_events or total_chars > max_chars:
        raise EpisodeError("episode_requires_staged_curation")
    if not events:
        raise EpisodeError("episode_has_no_curatable_events")
    artifacts = []
    for item in related_artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str):
            raise EpisodeError("invalid_related_artifact")
        artifacts.append({key: item.get(key) for key in
                          ("artifact_id", "claim", "evidence_status", "applicability",
                           "lifecycle", "revision")})
    return {
        "episode": {
            "episode_id": hashlib.sha256(json.dumps([project, session, turn,
                [row["id"] for row in ordered]], separators=(",", ":")).encode()).hexdigest(),
            "project": project, "session": session, "turn": turn,
            "objective_event_ids": [event["event_id"] for event in events
                                    if event["kind"] == "UserPromptSubmit"],
            "final_event_ids": [event["event_id"] for event in events if event["kind"] == "Stop"],
            "events": events,
        },
        "related_artifacts": artifacts,
        "applicability_options": _applicability_options(events),
        "deterministic_skip_receipts": deterministic_receipts,
    }


def prepare_episode_stages(sources, related_artifacts=(), *, max_events=80, max_chars=48_000,
                           split_oversized_events=False, media_policy=None):
    """Return one full packet or bounded transport stages retaining one episode ID."""
    if type(split_oversized_events) is not bool:
        raise ValueError('invalid_event_fragment_policy')
    try:
        return [prepare_episode(sources, related_artifacts,
                                max_events=max_events, max_chars=max_chars,
                                media_policy=media_policy)], False
    except EpisodeError as exc:
        if str(exc) != "episode_requires_staged_curation":
            raise
    if split_oversized_events or media_policy:
        return _fragment_episode_stages(sources, related_artifacts,
                                        max_events=max_events, max_chars=max_chars,
                                        media_policy=media_policy), True
    ordered = sorted(sources, key=lambda row: (row.get('source_order') if type(row.get('source_order')) is int else row.get("created", 0), row.get("id", "")))
    identity = {(row.get("project"), row.get("session"), row.get("turn")) for row in ordered}
    if len(identity) != 1:
        raise EpisodeError("mixed_episode_identity")
    native_identity = ordered[0].get('project', '').startswith('enterprise:')
    seen = {}
    retained = []
    deterministic = []
    for row in ordered:
        body = row.get("body", "")
        digest = hashlib.sha256(body.encode()).hexdigest()
        if not native_identity and body.strip() and digest in seen:
            deterministic.append({"event_id": row["id"], "reason": "duplicate_transport"})
            continue
        if body.strip():
            seen[digest] = row["id"]
        if (row.get("source_role") or "episode_evidence") in {"context_transfer", "retrieved_memory"}:
            deterministic.append({"event_id": row["id"], "reason": "context_transfer"})
            continue
        retained.append(row)
    anchors = [row for row in retained if row.get("kind") in {"UserPromptSubmit", "Stop"}]
    evidence = [row for row in retained if row.get("kind") not in {"UserPromptSubmit", "Stop"}]
    anchor_chars = sum(len(row.get("body", "")) for row in anchors)
    if len(anchors) >= max_events or anchor_chars >= max_chars:
        raise EpisodeError("episode_anchors_exceed_stage_limit")
    chunks = []
    current = []
    current_chars = anchor_chars
    for row in evidence:
        size = len(row.get("body", ""))
        if current and (len(current) + len(anchors) >= max_events
                        or current_chars + size > max_chars):
            chunks.append(current);current=[];current_chars=anchor_chars
        if size + anchor_chars > max_chars:
            raise EpisodeError("episode_event_exceeds_stage_limit")
        current.append(row);current_chars += size
    if current or not chunks:
        chunks.append(current)
    project, session, turn = next(iter(identity))
    episode_id = hashlib.sha256(json.dumps([project, session, turn,
        [row["id"] for row in retained]], separators=(",", ":")).encode()).hexdigest()
    packets = []
    for index, chunk in enumerate(chunks):
        packet = prepare_episode(anchors + chunk, related_artifacts,
                                 max_events=max_events, max_chars=max_chars)
        packet["episode"]["episode_id"] = episode_id
        packet["episode"]["stage_index"] = index
        packet["episode"]["stage_count"] = len(chunks)
        packet["deterministic_skip_receipts"] = deterministic if index == 0 else []
        packets.append(packet)
    return packets, True


def _fragment_episode_stages(sources, related_artifacts, *, max_events, max_chars, media_policy=None):
    """Partition original spans, never truncate an event or invent fragment IDs.

    The full packet is used only in local memory. Each provider stage retains the
    goal/final anchors and a bounded subset of exact spans with original offsets,
    order and event metadata. Installation must merge the spans from all stages.
    """
    full = prepare_episode(sources, related_artifacts, max_events=len(sources),
                           max_chars=sum(len(row.get('body', '')) for row in sources),
                           media_policy=media_policy)
    events = full['episode']['events']
    anchors = [event for event in events if event['kind'] in {'UserPromptSubmit', 'Stop'}]
    anchor_chars = sum(len(span['text']) for event in anchors for span in event['spans'])
    if len(anchors) >= max_events or anchor_chars >= max_chars:
        raise EpisodeError('episode_anchors_exceed_stage_limit')
    chunks = []; current = {}; size = anchor_chars
    for event in events:
        if event['kind'] in {'UserPromptSubmit', 'Stop'}:
            continue
        for span in event['spans'] or [None]:
            span_size = len(span['text']) if span else 0
            new_event = event['event_id'] not in current
            if current and (size + span_size > max_chars or
                    (new_event and len(current) + len(anchors) >= max_events)):
                chunks.append(list(current.values())); current = {}; size = anchor_chars
            if span_size + anchor_chars > max_chars:
                raise EpisodeError('episode_span_exceeds_stage_limit')
            fragment = current.setdefault(event['event_id'], {**event, 'spans': []})
            if span is not None:
                fragment['spans'].append(span)
            size += span_size
    if current or not chunks:
        chunks.append(list(current.values()))
    packets = []
    for index, chunk in enumerate(chunks):
        stage_events = sorted(anchors + chunk, key=lambda event: event['position'])
        packets.append({**full, 'episode': {**full['episode'], 'events': stage_events,
            'stage_index': index, 'stage_count': len(chunks)},
            'applicability_options': _applicability_options(stage_events),
            'deterministic_skip_receipts': full['deterministic_skip_receipts'] if index == 0 else []})
    return packets








def resolution_payload(candidate, payload):
    return {"candidate": candidate, "active_artifacts": payload.get("related_artifacts", [])}


def resolution_schema(candidate, payload):
    import copy
    schema = copy.deepcopy(RESOLUTION_SCHEMA)
    targets = [item["artifact_id"] for item in payload.get("related_artifacts", [])]
    schema["properties"]["candidate_key"] = {"type": "string", "enum": [candidate["candidate_key"]]}
    schema["properties"]["target_artifact_id"] = {"type": "string", "enum": [""] + targets}
    if candidate.get('memory_record') and not any(memory_equivalent(candidate,item) for item in payload.get('related_artifacts',[])):
        schema['properties']['operation']['enum']=[op for op in OPERATIONS if op not in {'IGNORE','SUPPORT'}]
    return schema


def validate_resolution(value, candidate, payload):
    if not isinstance(value, dict) or set(value) != set(RESOLUTION_SCHEMA["properties"]):
        raise EpisodeError("resolution_shape")
    if value["candidate_key"] != candidate["candidate_key"] or value["operation"] not in OPERATIONS:
        raise EpisodeError("resolution_reference")
    targets = {item["artifact_id"] for item in payload.get("related_artifacts", [])}
    target = value["target_artifact_id"]
    if value["operation"] in {"CREATE", "IGNORE"}:
        if target:
            raise EpisodeError("resolution_target")
    elif target not in targets:
        raise EpisodeError("resolution_target")
    expected_reasons = RESOLUTION_SCHEMA["properties"]["reason"]["enum"]
    if value["reason"] not in expected_reasons:
        raise EpisodeError("resolution_reason")
    if candidate.get('memory_record'):
        equivalent=[item['artifact_id'] for item in payload.get('related_artifacts',[]) if memory_equivalent(candidate,item)]
        if value['operation']=='SUPPORT' and target not in equivalent:raise EpisodeError('memory_support_would_erase_facets')
        if value['operation']=='IGNORE' and not equivalent:raise EpisodeError('memory_ignore_would_discard_validated_fact')
    return value
