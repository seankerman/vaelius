"""Canonical continuing-context helpers; observer execution belongs to AgentHub."""
import hashlib
import json
from agenthub.processing.episode_curator import EpisodeError, APPLICABILITY_FIELDS
def durable_continuation(current, previous, references, *, reconstruct=False,
                         max_context_chars=128_000):
    """Shared continuing-context transport for the canonical durable writer.

    Originals remain validation authority. Prior model output is only a checkpoint.
    All handles are stable within project/session and all previous spans are eligible.
    A finite evidence index replaces resending the complete chat on normal resumes.
    """
    packet=references.packet(current)
    index=[]
    if previous:
        prior=references.packet(previous)
        for event in prior['episode']['events']:
            for span in event['spans']:
                index.append({'event_id':event['event_id'],'kind':event['kind'],
                    'span_id':span['span_id'],'text':span['text'],
                    **{key:event[key] for key in ('role','actor','channel','source_order',
                        'occurred_at','occurred_precision','occurred_timezone','source_created',
                        'tool_name','exit_code','call_id','parent_id') if key in event},
                    'observed_allowed':event['observed_allowed']})
    if len(json.dumps(index,ensure_ascii=True))>max_context_chars:
        raise EpisodeError('observer_reconstruction_context_budget')
    packet['previous_evidence_index']=index
    packet['observer_reconstruction']=reconstruct
    return packet


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def durable_stage_context(packets, index, *, max_chars):
    """Bounded original earlier-stage context for a reconstructed observer.

    This does not expand the current stage's citable evidence manifest. Saved
    generated records are never substituted for the original readable spans.
    """
    if index <= 0:
        return {'spans': [], 'omitted_spans': 0, 'context_only': True}
    original = _whole_turn(packets[:index])
    entries = []
    for event in original['episode']['events']:
        for span in event['spans']:
            entries.append({'text': span['text'], 'kind': event['kind'],
                **{k: event[k] for k in ('role', 'actor', 'occurred_at', 'tool_name') if k in event}})
    selected = []
    used = 80
    for entry in reversed(entries):
        size = len(json.dumps(entry, ensure_ascii=True)) + 2
        if used + size <= max_chars:
            selected.append(entry)
            used += size
    return {'spans': list(reversed(selected)), 'omitted_spans': len(entries)-len(selected),
            'context_only': True}


def _whole_turn(packets):
    first = packets[0]
    episode = dict(first["episode"])
    seen = {}
    for packet in packets:
        for event in packet["episode"]["events"]:
            entry = seen.setdefault(event["event_id"], {**event, "spans": []})
            known = {span["span_id"] for span in entry["spans"]}
            entry["spans"].extend(span for span in event["spans"]
                                  if span["span_id"] not in known)
    episode["events"] = sorted(seen.values(), key=lambda event: event["position"])
    options = {field: [] for field in APPLICABILITY_FIELDS}
    for packet in packets:
        for field, values in packet["applicability_options"].items():
            options[field].extend(values)
    return {"episode": episode,
            "applicability_options": {key: list(dict.fromkeys(values))
                                      for key, values in options.items()},
            "deterministic_skip_receipts": list({(item["event_id"], item["reason"]): item
                for packet in packets for item in packet["deterministic_skip_receipts"]}.values())}


def _validation_packet(current, previous=None):
    if previous is None:
        return current
    packet = {**current, "episode": {**current["episode"]}}
    packet["episode"]["events"] = previous["episode"]["events"] + current["episode"]["events"]
    packet["applicability_options"] = {key: list(dict.fromkeys(
        previous["applicability_options"][key] + current["applicability_options"][key]))
        for key in APPLICABILITY_FIELDS}
    packet["deterministic_skip_receipts"] = (
        previous["deterministic_skip_receipts"] + current["deterministic_skip_receipts"])
    return packet
