"""Authorized project chronology over canonical episode revisions.

This is an on-demand derivative, not another knowledge corpus. The caller's
CloudStore supplies current project/source authorization; every continuation
rebuilds the authorized projection and rejects stale cursors.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from agenthub.enterprise import Denied


class HistoryDenied(PermissionError, Denied):
    """A denied history read, compatible with CloudStore's Denied boundary."""


_GAPS = {"capture_gap", "unprocessed_range", "index_refresh_pending"}
_MAX_EPISODES = 5000


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def _default_view(db, generation, project, session, turn, source_authorizer):
    from agenthub.processing.episode_pipeline import get_episode_view
    return get_episode_view(db, generation, project, session, turn, limit=20,
                            mode="history", source_authorizer=source_authorizer)


def _active_generation(db):
    row = db.execute("SELECT generation_id FROM knowledge_generations WHERE status='active' "
                     "ORDER BY created DESC,generation_id DESC LIMIT 1").fetchone()
    return row["generation_id"] if row else None


def _time(view):
    values = [a.get("occurred_date") for a in view.get("assertions", [])
              if isinstance(a.get("occurred_date"), str) and a["occurred_date"]]
    if not values:
        return None, "unknown", (1, 0)
    parsed = []
    for value in values:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            parsed.append((moment.astimezone(timezone.utc).timestamp(), value))
        except ValueError:
            continue
    if not parsed:
        return None, "unknown", (1, 0)
    number, value = min(parsed)
    return value, "explicit_effective", (0, number)


def _source_authorizer(store, db, ctx, internal_project, external_project):
    fingerprints = {}

    def allowed(source_id):
        if source_id in fingerprints:
            return fingerprints[source_id] is not None
        source = db.execute("SELECT * FROM enterprise_sources WHERE id=?", (source_id,)).fetchone()
        if (not source or source["internal_project"] != internal_project or
                ("external_project" in source.keys() and source["external_project"] != external_project)):
            fingerprints[source_id] = None
            return False
        head_fingerprint = None
        if not store._visible_source(db, ctx, source):
            if getattr(db, "dialect", None) != "postgres":
                fingerprints[source_id] = None
                return False
            # A retired document version is still an authorized historical
            # original when its current head and its own retained policy both
            # permit this reader. The document service also checks lifecycle
            # denial and accepted upload state. Ordinary inactive trace sources
            # never get this exception.
            from agenthub.document_ingest import DocumentStore
            from agenthub.source_objects import ObjectMissing
            try:
                DocumentStore(store, None)._authorize(db, ctx, source_id)
            except (Denied, ObjectMissing):
                fingerprints[source_id] = None
                return False
            head = db.execute('''SELECT h.source_id,s.policy_version,s.payload_hash,
                    s.visibility,s.active FROM backend_document_versions v
                JOIN backend_source_heads h ON h.connection=v.connection
                    AND h.external_id=v.external_id
                JOIN enterprise_sources s ON s.id=h.source_id
                WHERE v.source_id=?''',(source_id,)).fetchone()
            if not head:
                fingerprints[source_id] = None
                return False
            head_fingerprint = tuple(head[key] for key in
                ("source_id", "policy_version", "payload_hash", "visibility", "active"))
        fingerprints[source_id] = (source_id, source["payload_hash"], source["active"],
                                   source["visibility"] if "visibility" in source.keys() else None,
                                   source["policy_version"] if "policy_version" in source.keys() else None,
                                   head_fingerprint)
        return True

    return allowed, fingerprints


def _authorized_entries(store, db, ctx, project, generation, view_loader):
    scopes = store._readable_scopes(db, ctx, project)
    if not scopes:
        raise HistoryDenied()
    entries = []
    for scope in scopes:
        rows = db.execute("""SELECT o.occurrence_id,o.project,o.session,o.turn,o.created,
                r.revision_id,r.revision_number,r.source_ids,r.recorded_at
            FROM episode_occurrences o JOIN episode_revisions r
              ON r.occurrence_id=o.occurrence_id
            WHERE o.project=? AND r.generation_id=?
              AND r.revision_number=(SELECT MAX(x.revision_number) FROM episode_revisions x
                  WHERE x.occurrence_id=r.occurrence_id AND x.generation_id=r.generation_id)
            ORDER BY o.created,o.occurrence_id LIMIT ?""",
            (scope, generation, _MAX_EPISODES + 1)).fetchall()
        if len(rows) > _MAX_EPISODES:
            # A partial chronology is never described as complete or paginated
            # past a hidden/raw scan boundary.
            raise ValueError("project_history_scan_bound")
        allowed, source_fingerprints = _source_authorizer(store, db, ctx, scope, project)
        for row in rows:
            try:
                source_ids = json.loads(row["source_ids"])
            except (TypeError, ValueError):
                continue
            if not isinstance(source_ids, list) or not source_ids or not all(
                    isinstance(source_id, str) and allowed(source_id) for source_id in source_ids):
                continue
            view = view_loader(db, generation, scope, row["session"], row["turn"], allowed)
            if not view or view.get("occurrence_id") != row["occurrence_id"]:
                continue
            assertions = view.get("assertions") or []
            if not assertions and not view.get("open_work"):
                continue
            evidence = []
            valid = True
            for assertion in assertions:
                refs = assertion.get("evidence") or []
                if not refs:
                    valid = False
                    break
                for ref in refs:
                    source_id = ref.get("source_id")
                    if not source_id or not allowed(source_id):
                        valid = False
                        break
                    evidence.append({key: ref[key] for key in ("source_id", "version", "start", "end", "segment_id")
                                     if key in ref})
                if not valid:
                    break
            if not valid:
                continue
            event_time, basis, sort_time = _time(view)
            reasons = [a["rationale"] for a in assertions if a.get("rationale")]
            statuses = list(dict.fromkeys(a.get("state") for a in assertions if a.get("state")))
            entries.append({"episode_id": row["occurrence_id"], "handle": view.get("handle") or row["occurrence_id"],
                            "revision_id": row["revision_id"], "summary_revision": view.get("summary_revision"),
                            "summary": str(view.get("summary") or ""), "event_time": event_time,
                            "time_basis": basis, "evidence_refs": evidence,
                            "rationale": reasons, "statuses": statuses,
                            "coverage_gaps": list(view.get("coverage_gaps") or []),
                            "sort_key": (sort_time, row["created"], row["occurrence_id"]),
                            "dependency_fingerprint": sorted(source_fingerprints[x] for x in source_ids),
                            "scope": scope})
    entries.sort(key=lambda e: e["sort_key"])
    return entries


def _anchors(entries):
    if not entries:
        return []
    positions = (0, (len(entries) - 1) // 2, len(entries) - 1)
    return [{"phase": phase, "episode_id": entries[index]["episode_id"]}
            for phase, index in zip(("early", "middle", "recent"), positions)]


def _recheck_snapshot(store, db, ctx, project, generation, entries):
    if _active_generation(db) != generation:
        raise HistoryDenied()
    authorizers = {}
    for entry in entries:
        scope = entry["scope"]
        if scope not in authorizers:
            authorizers[scope] = _source_authorizer(store, db, ctx, scope, project)
        allowed, fingerprints = authorizers[scope]
        source_ids = [dependency[0] for dependency in entry["dependency_fingerprint"]]
        if not all(allowed(source_id) for source_id in source_ids):
            raise HistoryDenied()
        if sorted(fingerprints[source_id] for source_id in source_ids) != entry["dependency_fingerprint"]:
            raise HistoryDenied()


def _card(entry, *, compact=False):
    card = {key: entry[key] for key in ("episode_id", "handle", "revision_id", "event_time", "time_basis")}
    if compact:
        card["detail_available"] = True
        card["coverage_gaps"] = ["episode_detail_required"]
    else:
        card.update(summary=entry["summary"], evidence_refs=entry["evidence_refs"],
                    rationale=entry["rationale"], statuses=entry["statuses"],
                    coverage_gaps=entry["coverage_gaps"], detail_available=True)
    return card


def _minimal_overview(entries, project, revision, offset, page_limit, max_bytes, gaps):
    """Keep a complete continuation path when full evidence cards cannot fit.

    The cursor carries a 96-bit prefix of the authorization-bound revision;
    each read recomputes the full revision before accepting it. Detailed evidence
    remains available through the episode endpoint after its own policy check.
    """
    cursor_revision = revision[:24]
    def pack(lean):
        # A very small page still carries every known coverage gap and an
        # authorization-bound continuation. The response omits its redundant
        # summary fingerprint only when the full set cannot fit; the cursor
        # continues to bind the full server-side revision on the next read.
        result = {"project": project,
                  "summary_revision": None if lean else revision[:32],
                  "phase_anchors": [], "episodes": [],
                  "coverage_gaps": (list(dict.fromkeys(gaps)) if lean else
                                    list(dict.fromkeys([*gaps, "phase_anchors_deferred"]))),
                  "has_more": False, "next_cursor": None}
        for entry in entries[offset:offset + page_limit]:
            next_offset = offset + len(result["episodes"]) + 1
            card = {"episode_id": entry["episode_id"]}
            if not lean:card["detail_available"] = True
            trial = {**result, "episodes": [*result["episodes"], card],
                     "has_more": next_offset < len(entries),
                     "next_cursor": (f"ph2_{next_offset}_{cursor_revision}"
                                     if next_offset < len(entries) else None)}
            if _size(trial) > max_bytes:
                break
            result = trial
        return result
    result = pack(False)
    if entries[offset:] and not result["episodes"]:
        result = pack(True)
    if entries[offset:] and not result["episodes"]:
        raise ValueError("project_history_response_bound")
    return result


def project_overview(store, ctx, project, *, cursor=None, page_limit=8,
                     max_bytes=4000, authorized_gaps=(), view_loader=None):
    """Return a bounded chronology page and early/middle/recent navigation.

    `project` is the external project ID. `authorized_gaps` contains only
    caller-verified, non-identifying labels; absent capture metadata is reported
    as unverified rather than silently claiming complete project history.
    """
    if not isinstance(project, str) or not project or type(page_limit) is not int or not 1 <= page_limit <= 20:
        raise ValueError("project_history_request")
    if type(max_bytes) is not int or not 320 <= max_bytes <= 4000:
        raise ValueError("project_history_byte_bound")
    if not isinstance(authorized_gaps, (list, tuple)) or set(authorized_gaps) - _GAPS:
        raise ValueError("project_history_gap_label")
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(
            r"(?:ph1_\d{1,5}_[0-9a-f]{64}|ph2_\d{1,5}_[0-9a-f]{24})", cursor)):
        raise ValueError("project_history_cursor")
    store._need(ctx, "read")
    with store.open() as state:
        db = state.db
        if not store._readable_scopes(db, ctx, project):
            raise HistoryDenied()
        generation = _active_generation(db)
        if generation is None:
            if cursor is not None:
                raise ValueError("project_history_cursor_stale")
            return {"project": project, "generation_id": None, "summary_revision": None,
                    "phase_anchors": [], "episodes": [], "coverage": "available_authorized_history",
                    "coverage_gaps": ["generation_unavailable", "capture_coverage_unverified"],
                    "has_more": False, "next_cursor": None, "derived_context": True}
        entries = _authorized_entries(store, db, ctx, project, generation, view_loader or _default_view)
        identity = [ctx.get(k) for k in ("tenant", "actor", "principal", "enrollment", "acting_for")]
        identity.append(sorted(ctx.get("delegated_projects") or []))
        projection = [[e["episode_id"], e["revision_id"], e["summary_revision"], e["summary"],
                       e["event_time"], e["dependency_fingerprint"]] for e in entries]
        summary_revision = _hash(["project-history-v1", identity, project, generation,
                                  projection, sorted(authorized_gaps)])
        offset = 0
        if cursor is not None:
            offset = int(cursor.split("_")[1])
            expected = (f"ph1_{offset}_{summary_revision}" if cursor.startswith("ph1_")
                        else f"ph2_{offset}_{summary_revision[:24]}")
            if offset > len(entries) or cursor != expected:
                raise ValueError("project_history_cursor_stale")
        gaps = ["capture_coverage_unverified", *dict.fromkeys(authorized_gaps)]
        if any(e["time_basis"] == "unknown" for e in entries):
            gaps.append("unknown_event_time")
        if any(e["coverage_gaps"] for e in entries):
            gaps.append("curation_partial")
        if max_bytes == 320 or (cursor is not None and cursor.startswith("ph2_")):
            result = _minimal_overview(entries, project, summary_revision, offset,
                                       page_limit, max_bytes, gaps)
            _recheck_snapshot(store, db, ctx, project, generation, entries)
            return result
        result = {"project": project, "generation_id": generation,
                  "summary_revision": summary_revision,
                  "phase_anchors": _anchors(entries) if offset == 0 and len(entries) > 3 else [],
                  "episodes": [], "coverage": "available_authorized_history",
                  "coverage_gaps": gaps, "has_more": bool(entries[offset:]),
                  "next_cursor": f"ph1_{offset}_{summary_revision}" if entries[offset:] else None,
                  "derived_context": True}
        for entry in entries[offset:offset + page_limit]:
            # Keep the early/middle/recent navigation when a compact drill-down
            # card fits; drop anchors only after both card shapes fail.
            variants = [(False, False), (True, False)]
            if result["phase_anchors"] and not result["episodes"]:
                variants += [(False, True), (True, True)]
            for compact, drop_anchors in variants:
                card = _card(entry, compact=compact)
                next_offset = offset + len(result["episodes"]) + 1
                trial = {**result, "episodes": result["episodes"] + [card],
                         "has_more": next_offset < len(entries),
                         "next_cursor": f"ph1_{next_offset}_{summary_revision}" if next_offset < len(entries) else None}
                if drop_anchors:
                    trial["phase_anchors"] = []
                    if "phase_anchors_deferred" not in trial["coverage_gaps"]:
                        trial["coverage_gaps"] = [*trial["coverage_gaps"], "phase_anchors_deferred"]
                if _size(trial) <= max_bytes:
                    result = trial
                    break
            else:
                if result["episodes"]:
                    break
                result = _minimal_overview(entries, project, summary_revision, offset,
                                           page_limit, max_bytes, gaps)
                break
        if entries[offset:] and not result["episodes"]:
            raise ValueError("project_history_response_bound")
        if _size(result) > max_bytes:
            raise ValueError("project_history_response_bound")
        _recheck_snapshot(store, db, ctx, project, generation, entries)
        return result


def _detail_card(assertion):
    keys = ("candidate_id", "atom_key", "document_id", "revision_id", "episode_revision_id",
            "title", "text", "subject", "facets", "actors", "state", "attribution",
            "rationale", "occurred_date", "claim_status", "operation", "evidence")
    return {key: assertion[key] for key in keys if key in assertion}


def _pack_detail(view, offset, limit, text_offset, max_bytes):
    summary = str(view.get("summary") or "")
    result = {key: view.get(key) for key in ("episode_id", "occurrence_id",
              "curated_revision_id", "summary_revision", "generation_id", "project",
              "session", "source_turn") if key in view}
    result.update(summary=summary[:500], assertions=[], offset=offset,
                  next_offset=None, next_text_offset=None, has_more=False,
                  coverage_gaps=list(view.get("coverage_gaps") or []),
                  derived_context=True)
    if len(summary) > 500:
        result["coverage_gaps"].append("episode_summary_truncated")
    assertions = list(view.get("assertions") or [])
    if text_offset and not assertions:
        raise ValueError("project_episode_page")
    for index, assertion in enumerate(assertions[:limit]):
        card = _detail_card(assertion)
        full_text = str(card.get("text") or "")
        start = text_offset if index == 0 else 0
        if start > len(full_text):
            raise ValueError("project_episode_page")
        remaining = full_text[start:]
        lo, hi = 0, len(remaining)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            candidate = dict(card, text=remaining[:middle], text_truncated=middle < len(remaining))
            continuation = start + middle if middle < len(remaining) else None
            trial = {**result, "assertions": result["assertions"] + [candidate],
                     "next_text_offset": continuation,
                     "next_offset": offset + index + 1 if continuation is None and
                         (index + 1 < len(assertions) or view.get("has_more")) else None,
                     "has_more": bool(continuation is not None or index + 1 < len(assertions) or view.get("has_more"))}
            if continuation is not None and "assertion_text_continuation" not in trial["coverage_gaps"]:
                trial["coverage_gaps"] = [*trial["coverage_gaps"], "assertion_text_continuation"]
            if _size(trial) <= max_bytes:
                lo = middle
            else:
                hi = middle - 1
        if not remaining:
            lo = 0
        if lo == 0 and remaining:
            if result["assertions"]:
                break
            raise ValueError("project_episode_response_bound")
        candidate = dict(card, text=remaining[:lo], text_truncated=lo < len(remaining))
        continuation = start + lo if lo < len(remaining) else None
        trial = {**result, "assertions": result["assertions"] + [candidate],
                 "next_text_offset": continuation,
                 "next_offset": offset + index + 1 if continuation is None and
                     (index + 1 < len(assertions) or view.get("has_more")) else None,
                 "has_more": bool(continuation is not None or index + 1 < len(assertions) or view.get("has_more"))}
        if continuation is not None and "assertion_text_continuation" not in trial["coverage_gaps"]:
            trial["coverage_gaps"] = [*trial["coverage_gaps"], "assertion_text_continuation"]
        if _size(trial) > max_bytes:
            if result["assertions"]:
                break
            raise ValueError("project_episode_response_bound")
        result = trial
        if continuation is not None:
            break
    if len(result["assertions"]) < len(assertions) and result["next_text_offset"] is None:
        result["has_more"] = True
        result["next_offset"] = offset + len(result["assertions"])
    if _size(result) > max_bytes:
        raise ValueError("project_episode_response_bound")
    return result


def project_episode_detail(store, ctx, project, episode_id, *, limit=8, offset=0,
                           text_offset=0, max_bytes=4000, view_loader=None):
    """Expand an overview episode through the canonical cited history view."""
    if not isinstance(project, str) or not project or not isinstance(episode_id, str) or not episode_id:
        raise ValueError("project_episode_request")
    if (type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= 1000
            or type(text_offset) is not int or not 0 <= text_offset <= 1000000
            or (text_offset and limit != 1)):
        raise ValueError("project_episode_page")
    if type(max_bytes) is not int or not 320 <= max_bytes <= 4000:
        raise ValueError("project_episode_byte_bound")
    store._need(ctx, "read")
    with store.open() as state:
        db = state.db
        generation = _active_generation(db)
        if generation is None or not store._readable_scopes(db, ctx, project):
            raise HistoryDenied()
        row = db.execute("""SELECT o.occurrence_id,o.project,o.session,o.turn,r.revision_id,r.source_ids
            FROM episode_occurrences o JOIN episode_revisions r ON r.occurrence_id=o.occurrence_id
            WHERE o.occurrence_id=? AND r.generation_id=?
              AND r.revision_number=(SELECT MAX(x.revision_number) FROM episode_revisions x
                  WHERE x.occurrence_id=r.occurrence_id AND x.generation_id=r.generation_id)""",
            (episode_id, generation)).fetchone()
        if row is None or row["project"] not in store._readable_scopes(db, ctx, project):
            raise HistoryDenied()
        allowed, _ = _source_authorizer(store, db, ctx, row["project"], project)
        try:
            source_ids = json.loads(row["source_ids"])
        except (TypeError, ValueError):
            raise HistoryDenied() from None
        if not isinstance(source_ids, list) or not source_ids or not all(
                isinstance(source_id, str) and allowed(source_id) for source_id in source_ids):
            raise HistoryDenied()
        loader = view_loader or _default_view
        if loader is _default_view:
            from agenthub.processing.episode_pipeline import get_episode_view
            view = get_episode_view(db, generation, row["project"], row["session"], row["turn"],
                                    limit=limit, offset=offset, mode="history", source_authorizer=allowed)
        else:
            view = loader(db, generation, row["project"], row["session"], row["turn"], allowed)
        if not view or view.get("occurrence_id") != episode_id:
            raise HistoryDenied()
        for assertion in view.get("assertions") or []:
            refs = assertion.get("evidence") or []
            if not refs or not all(ref.get("source_id") and allowed(ref["source_id"]) for ref in refs):
                raise HistoryDenied()
        fresh, _ = _source_authorizer(store, db, ctx, row["project"], project)
        if _active_generation(db) != generation or not all(fresh(source_id) for source_id in source_ids):
            raise HistoryDenied()
        return _pack_detail(view, offset, limit, text_offset, max_bytes)
