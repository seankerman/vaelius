"""Short, scoped, deterministic transport handles; storage keeps original IDs."""
from copy import deepcopy
import hashlib
import json


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _handle(kind, scope, ident):
    return kind + _digest([scope, ident])[:8]


class EvidenceReferences:
    def __init__(self, packet, *, previous=None):
        episode = packet["episode"]
        self.scope = [episode["project"], episode["session"]]
        self.events = {}; self.spans = {}; self.entries = {}
        for event in episode["events"]:
            self._add("e", event["event_id"], event, self.events)
            for span in event["spans"]:
                self._add("s", span["span_id"], [event["event_id"], span], self.spans)
        self.manifest = {"version": 1, "scope": self.scope, "entries": self.entries}
        if previous is not None:
            if (not isinstance(previous, dict) or previous.get("version") != 1 or previous.get("scope") != self.scope or
                    not isinstance(previous.get("entries"), dict) or
                    any(self.entries.get(key) != value for key, value in previous["entries"].items())):
                raise ValueError("reference_manifest_changed")
            # Replaying a return may use only references available to that call.
            # Extra original context is allowed, but cannot bless an unknown handle
            # in an old result. Every original input (including uncited ones) must
            # still match, rather than just the handles a record happened to cite.
            self.events = {k: v for k, v in self.events.items() if k in previous['entries']}
            self.spans = {k: v for k, v in self.spans.items() if k in previous['entries']}
            self.manifest = deepcopy(previous)

    def _add(self, kind, ident, source, target):
        handle = _handle(kind, self.scope, ident)
        entry = {"kind": kind, "original_id": ident, "source_sha256": _digest(source)}
        if handle in self.entries and self.entries[handle] != entry:
            raise ValueError("reference_collision")
        self.entries[handle] = entry
        target[handle] = ident

    def packet(self, packet):
        from agenthub.processing.durable_memory import _successful_execution
        if [packet["episode"]["project"], packet["episode"]["session"]] != self.scope:
            raise ValueError("reference_scope")
        result = deepcopy(packet)
        for field in ("objective_event_ids", "final_event_ids"):
            result["episode"][field] = [_handle("e", self.scope, ident)
                                         for ident in result["episode"].get(field, [])]
        for event in result["episode"]["events"]:
            event["event_id"] = _handle("e", self.scope, event["event_id"])
            if event["event_id"] not in self.events:
                raise ValueError("reference_event_unknown")
            event["observed_allowed"] = _successful_execution(event)
            event["verification_gap"] = ("tool_identity_missing" if event["kind"] == "PostToolUse"
                and not event.get("tool_name") else "" if event["observed_allowed"] else "no_verified_execution")
            for span in event["spans"]:
                span["span_id"] = _handle("s", self.scope, span["span_id"])
                if span["span_id"] not in self.spans:
                    raise ValueError("reference_span_unknown")
        return result

    def resolve(self, record):
        if not isinstance(record, dict):
            raise ValueError("reference_record_shape")
        result = dict(record)
        try:
            if not isinstance(result["evidence_span_ids"], list):
                raise ValueError("reference_span_shape")
            result["event_id"] = self.events[result["event_id"]]
            result["evidence_span_ids"] = [self.spans[s] for s in result["evidence_span_ids"]]
        except (KeyError, TypeError) as exc:
            raise ValueError("reference_unknown") from exc
        return result
