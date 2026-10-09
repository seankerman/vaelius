"""Explicit, resumable historical Codex capture for an isolated enterprise profile.

The forward desktop cursor remains forward-only. This operator-only path reads an
allowlisted, immutable prefix of retained rollouts, feeds the same structured
normalizer as forward capture, and uses the existing redacted transport outbox.
No model runs in this module. Source text, paths, credentials and manifests stay
in the owner's private installation, never in a repository receipt.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from agentclient.capture_outbox import Outbox
from agentclient.enterprise_capture import normalize_capture
from agentclient.enterprise_desktop import convert
from agentclient.general_contract import split_event


VERSION = 1
MAX_LINE_BYTES = 32 * 1024 * 1024
BACKFILL_OUTBOX_MAX_AGE = 365 * 86400


@dataclass(frozen=True)
class Limits:
    max_lines: int = 500
    max_source_bytes: int = 256 * 1024 * 1024
    max_seconds: int = 300
    max_events: int = 500

    def validate(self):
        for name, value, ceiling in (
            ("max_lines", self.max_lines, 1_000_000),
            ("max_source_bytes", self.max_source_bytes, 10_000_000_000),
            ("max_seconds", self.max_seconds, 3600),
            ("max_events", self.max_events, 100_000),
        ):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid_" + name)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _binding(cwd, roots):
    if not isinstance(cwd, str):
        return None
    try:
        # A historical checkout may have been moved or deleted since the chat.
        path = Path(cwd).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None
    for item in sorted(roots, key=lambda row: -len(row["root"])):
        root = Path(item["root"])
        if path == root or root in path.parents:
            return item
    return None


def _roots(value):
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ValueError("explicit_project_roots_required")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"root", "project", "connection"}:
            raise ValueError("invalid_project_binding")
        if any(not isinstance(item[key], str) or not item[key] for key in item):
            raise ValueError("invalid_project_binding")
        root = Path(item["root"]).expanduser().resolve(strict=False)
        if not root.is_absolute() or (root.exists() and not root.is_dir()):
            raise ValueError("project_root_not_directory")
        result.append({**item, "root": str(root)})
    if len({item["root"] for item in result}) != len(result):
        raise ValueError("duplicate_project_root")
    return result


def _header(path):
    with path.open("rb") as stream:
        line = stream.readline(65_537)
    if not line.endswith(b"\n") or len(line) > 65_536:
        raise ValueError("invalid_session_header")
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("type") != "session_meta" or not isinstance(value.get("payload"), dict):
        raise ValueError("invalid_session_header")
    payload = value["payload"]
    session = payload.get("id") or payload.get("session_id")
    if not isinstance(session, str) or not session or not path.name.endswith(session + ".jsonl"):
        raise ValueError("invalid_session_identity")
    return session, payload, len(line), hashlib.sha256(line).hexdigest()


def discover(codex_home, project_roots, *, max_sessions=1000):
    """Read headers only; never infer source access from a chat's own content."""
    if type(max_sessions) is not int or not 1 <= max_sessions <= 1000:
        raise ValueError("invalid_max_sessions")
    home = Path(codex_home).expanduser().resolve(strict=True)
    roots = _roots(project_roots)
    selected = {}
    counts = Counter()
    for location in ("archived_sessions", "sessions"):
        source = home / location
        if not source.is_dir():
            continue
        for path in sorted(source.rglob("*.jsonl")):
            counts["files"] += 1
            try:
                target = path.resolve(strict=True)
                if source.resolve() not in target.parents or not target.is_file():
                    raise ValueError("session_path_escape")
                session, payload, header_bytes, header_sha256 = _header(target)
                if any(isinstance(payload.get(key), dict) and "subagent" in payload[key]
                       for key in ("source", "thread_source")):
                    counts["subagents"] += 1
                    continue
                binding = _binding(payload.get("cwd"), roots)
                if binding is None:
                    counts["unconfigured"] += 1
                    continue
                stat = target.stat()
                if session in selected:
                    counts["duplicates"] += 1
                selected[session] = {"session": session, "path": str(target),
                    "location": location, "project": binding["project"],
                    "connection": binding["connection"], "approved_root": binding["root"],
                    "device": stat.st_dev, "inode": stat.st_ino,
                    "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
                    "header_bytes": header_bytes, "header_sha256": header_sha256}
            except (OSError, ValueError, json.JSONDecodeError):
                counts["invalid"] += 1
    sessions = [selected[key] for key in sorted(selected)][:max_sessions]
    body = {"version": VERSION, "codex_home": str(home), "roots": roots, "sessions": sessions}
    return {**body, "manifest_sha256": _hash(body),
        "summary": {**counts, "selected_sessions": len(sessions),
                    "selected_source_bytes": sum(s["source_size"] for s in sessions)}}


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
        raise ValueError("invalid_backfill_manifest")
    body = {key: manifest[key] for key in ("version", "codex_home", "roots", "sessions")}
    if manifest.get("manifest_sha256") != _hash(body):
        raise ValueError("backfill_manifest_changed")
    home = Path(body["codex_home"]).resolve(strict=True)
    roots = _roots(body["roots"])
    if len(body["sessions"]) > 1000 or len({s["session"] for s in body["sessions"]}) != len(body["sessions"]):
        raise ValueError("invalid_backfill_sessions")
    for item in body["sessions"]:
        path = Path(item["path"]).resolve(strict=True)
        if not any((home / loc).resolve() in path.parents for loc in ("sessions", "archived_sessions")):
            raise ValueError("backfill_path_escape")
        session, payload, size, sha = _header(path)
        if (session != item["session"] or size != item["header_bytes"] or sha != item["header_sha256"]
                or item["device"] != path.stat().st_dev or item["inode"] != path.stat().st_ino
                or path.stat().st_size < item["source_size"]):
            raise ValueError("backfill_source_changed")
        binding = _binding(payload.get("cwd"), roots)
        if not binding or any(item[key] != binding[value] for key, value in
                              (("approved_root", "root"), ("project", "project"), ("connection", "connection"))):
            raise ValueError("backfill_binding_changed")
    return body


def _gap(item, offset, turn, reason, timestamp="unknown"):
    event = normalize_capture({"hook_event_name": "gap", "session_id": item["session"],
        "turn_id": turn, "event_id": "historical-gap:" + str(offset), "timestamp": timestamp,
        "source_order": offset}, item["project"], item["connection"])
    event["external_id"] = _historical_id(item["session"], event["external_id"])
    event["blocks"] = [{"type": "record_fields", "value": {"capture_gap": reason}}]
    return event


def _historical_id(session, native_id):
    # The host only guarantees item IDs inside its own chat. Source revisions
    # are keyed by connection, so two archived chats may reuse one native ID.
    return "historical:" + hashlib.sha256(_canonical([session, native_id])).hexdigest()


def _event(item, line, offset, turn):
    if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
        return _gap(item, offset, turn, "oversize_or_partial_line"), turn, "gap"
    try:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("invalid_source_record")
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("invalid_payload")
        if isinstance(payload.get("turn_id"), str) and payload["turn_id"]:
            turn = payload["turn_id"]
        value = convert(record, item["session"], item["project"], item["connection"],
                        turn=turn, order=offset)
        if value is not None:
            value["external_id"] = _historical_id(item["session"], value["external_id"])
            value["event"]["expected_events"] = [
                _historical_id(item["session"], native_id)
                for native_id in value["event"]["expected_events"]]
            try:
                split_event(value)
            except (TypeError, ValueError):
                value = _gap(item, offset, turn, "logical_event_too_large", record.get("timestamp", "unknown"))
                return value, turn, "gap"
        return value, turn, "complete" if (record.get("type") == "event_msg" and
            payload.get("type") == "task_complete") else "event"
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return _gap(item, offset, turn, "unparseable_source_line"), turn, "gap"


def _scan(manifest, limits, *, callback=None, cursors=None):
    body = validate_manifest(manifest)
    limits.validate()
    start = time.monotonic()
    counts = Counter()
    positions = {}
    stop = False
    for item in body["sessions"]:
        if stop:
            break
        begin = cursors[item["session"]] if cursors is not None else item["header_bytes"]
        turn = ""
        with Path(item["path"]).open("rb") as stream:
            stream.seek(begin)
            while stream.tell() < item["source_size"]:
                if (counts["source_lines_advanced"] >= limits.max_lines or
                        counts["source_bytes_read"] >= limits.max_source_bytes or
                        counts["deliverable_events"] >= limits.max_events or
                        time.monotonic() - start >= limits.max_seconds):
                    stop = True
                    break
                offset = stream.tell()
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line or stream.tell() > item["source_size"]:
                    stop = True
                    break
                oversized = len(line) > MAX_LINE_BYTES or not line.endswith(b"\n")
                if oversized:
                    while line and not line.endswith(b"\n") and stream.tell() < item["source_size"]:
                        line = stream.readline(MAX_LINE_BYTES + 1)
                end = stream.tell()
                if counts["source_bytes_read"] + end - offset > limits.max_source_bytes:
                    stop = True
                    break
                event, turn, kind = (_gap(item, offset, turn, "oversize_or_partial_line"), turn, "gap") \
                    if oversized else _event(item, line, offset, turn)
                if callback:
                    if callback(item, event, end, turn) is False:
                        stop = True
                        break
                counts["source_lines_advanced"] += 1
                counts["source_bytes_read"] += end - offset
                if kind == "complete":
                    counts["completed_turns"] += 1
                if event is None:
                    counts["ignored_lines"] += 1
                else:
                    counts["deliverable_events"] += 1
                    if event["disposition"] in ("unsupported", "incomplete") or kind == "gap":
                        counts["gap_or_unsupported_events"] += 1
                positions[item["session"]] = end
    return body, counts, positions, stop


def preflight(manifest, limits=Limits(max_lines=1_000_000, max_source_bytes=10_000_000_000,
                                      max_seconds=3600, max_events=100_000)):
    """Count converted events without storage, transport, provider or source text output."""
    turns = {}
    def measure(item, event, _end, turn):
        if event is not None and turn:
            key = (item["session"], turn)
            row = turns.setdefault(key, {"project":item["project"], "events":0,
                "bytes":0, "gap":False, "complete":False})
            row["events"] += 1
            row["bytes"] += len(_canonical(event))
            row["gap"] |= event["disposition"] in ("unsupported", "incomplete")
            row["complete"] |= bool(event["event"].get("complete"))
    body, counts, positions, stopped = _scan(manifest, limits, callback=measure)
    completed = [row for row in turns.values() if row["complete"]]
    by_project = {}
    for row in turns.values():
        group = by_project.setdefault(row["project"], Counter())
        group["events"] += row["events"]
        group["redacted_event_bytes"] += row["bytes"]
        if row["complete"]:
            group["completed_turns"] += 1
            group["held_turns_with_gaps"] += int(row["gap"])
        else:
            group["open_turns"] += 1
    return {**counts, "sessions_scanned":len(positions), "selected_sessions":len(body["sessions"]),
        "eligible_complete_turns":sum(not row["gap"] for row in completed),
        "held_complete_turns":sum(row["gap"] for row in completed),
        "redacted_event_bytes":sum(row["bytes"] for row in turns.values()),
        "by_project":{project:dict(counters) for project,counters in sorted(by_project.items())},
        "complete":not stopped and all(positions.get(s["session"], s["header_bytes"]) >= s["source_size"]
                                  for s in body["sessions"]), "provider_calls":0,
        "private_source_text_in_report":False}


def status_manifest(manifest, home):
    """Read-only, cumulative byte and outbox progress for one frozen manifest."""
    body = validate_manifest(manifest)
    target = Path(home).expanduser().resolve()
    if target.exists() and target.stat().st_mode & 0o077:
        raise ValueError("backfill_home_permissions")
    positions = {}
    cursor_file = target / "historical-backfill.sqlite"
    if cursor_file.is_file():
        with sqlite3.connect(cursor_file.resolve().as_uri() + "?mode=ro", uri=True) as db:
            positions = {session: offset for session, offset in db.execute(
                "SELECT session,offset FROM progress WHERE manifest=?",
                (manifest["manifest_sha256"],))}
    total = committed = completed = 0
    for item in body["sessions"]:
        size = item["source_size"] - item["header_bytes"]
        offset = positions.get(item["session"], item["header_bytes"])
        if not item["header_bytes"] <= offset <= item["source_size"]:
            raise ValueError("historical_backfill_cursor_invalid")
        total += size
        committed += offset - item["header_bytes"]
        completed += offset == item["source_size"]
    pending = acknowledged = 0
    outbox_file = target / "capture-outbox.sqlite"
    if outbox_file.is_file():
        with sqlite3.connect(outbox_file.resolve().as_uri() + "?mode=ro", uri=True) as db:
            pending = db.execute("SELECT count(*) FROM pending").fetchone()[0]
            acknowledged = db.execute("SELECT count(*) FROM receipts").fetchone()[0]
    return {"status": "complete" if completed == len(body["sessions"]) and not pending else "partial",
            "manifest_sha256": manifest["manifest_sha256"],
            "sessions_total": len(body["sessions"]), "sessions_complete": completed,
            "bytes_total": total, "bytes_committed": committed,
            "bytes_remaining": total - committed, "pending_events": pending,
            "acknowledged_receipts": acknowledged, "provider_calls": 0,
            "private_source_text_in_report": False}


def _cursor(home, manifest):
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.stat().st_mode & 0o077:
        raise ValueError("backfill_home_permissions")
    path = home / "historical-backfill.sqlite"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("CREATE TABLE IF NOT EXISTS progress(manifest TEXT NOT NULL, session TEXT NOT NULL, offset INTEGER NOT NULL, turn TEXT NOT NULL DEFAULT '', PRIMARY KEY(manifest,session))")
    with db:
        for item in manifest["sessions"]:
            db.execute("INSERT OR IGNORE INTO progress(manifest,session,offset) VALUES(?,?,?)",
                       (manifest["manifest_sha256"], item["session"], item["header_bytes"]))
    return db


def apply_manifest(manifest, home, backend, limits=Limits()):
    """Deliver one bounded slice. A cursor advances only after a durable outbox put."""
    target = Path(home).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.stat().st_mode & 0o077:
        raise ValueError("backfill_home_permissions")
    fd = os.open(target / "historical-backfill.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("historical_backfill_already_running") from None
        return _apply_manifest_locked(manifest, target, backend, limits)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _apply_manifest_locked(manifest, home, backend, limits):
    body = validate_manifest(manifest)
    limits.validate()
    db = _cursor(home, manifest)
    # A deliberate historical import may be paused for days. Its durable spool
    # must retain the redacted source until ACK instead of inheriting the
    # forward hook's one-day expiration and silently replacing it with a gap.
    outbox = Outbox(home, max_age=BACKFILL_OUTBOX_MAX_AGE)
    counts = Counter()
    start = time.monotonic()
    try:
        def drain():
            result = outbox.drain(backend, max_events=32, max_seconds=min(30, limits.max_seconds))
            counts["acknowledged_events"] += result["acknowledged_events"]
            return result
        transport = drain()
        if transport["blocked"] or transport["pending_events"]:
            return {"status":"transport_blocked", **counts, "outbox":transport,
                    "provider_calls":0,"private_source_text_in_report":False}
        for item in body["sessions"]:
            row = db.execute("SELECT offset,turn FROM progress WHERE manifest=? AND session=?",
                             (manifest["manifest_sha256"], item["session"])).fetchone()
            offset, turn = row
            if offset >= item["source_size"]:
                continue
            with Path(item["path"]).open("rb") as stream:
                stream.seek(offset)
                while stream.tell() < item["source_size"]:
                    if (counts["source_lines_advanced"] >= limits.max_lines or
                            counts["source_bytes_read"] >= limits.max_source_bytes or
                            counts["deliverable_events"] >= limits.max_events or
                            time.monotonic() - start >= limits.max_seconds):
                        break
                    before = stream.tell()
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line or stream.tell() > item["source_size"]:
                        break
                    oversized = len(line) > MAX_LINE_BYTES or not line.endswith(b"\n")
                    if oversized:
                        while line and not line.endswith(b"\n") and stream.tell() < item["source_size"]:
                            line = stream.readline(MAX_LINE_BYTES + 1)
                    end = stream.tell()
                    if counts["source_bytes_read"] + end - before > limits.max_source_bytes:
                        break
                    event, turn, kind = (_gap(item, before, turn, "oversize_or_partial_line"), turn, "gap") \
                        if oversized else _event(item, line, before, turn)
                    if event is not None:
                        try:
                            outbox.put(event)
                        except ValueError as exc:
                            if str(exc) != "capture_outbox_full":
                                raise
                            transport = drain()
                            if transport["blocked"] or transport["pending_events"]:
                                return {"status":"transport_blocked", **counts, "outbox":transport,
                                        "provider_calls":0,"private_source_text_in_report":False}
                            outbox.put(event)
                    with db:
                        db.execute("UPDATE progress SET offset=?,turn=? WHERE manifest=? AND session=?",
                                   (end,turn,manifest["manifest_sha256"],item["session"]))
                    counts["source_lines_advanced"] += 1
                    counts["source_bytes_read"] += end - before
                    if kind == "complete":counts["completed_turns"] += 1
                    if event is not None:
                        counts["deliverable_events"] += 1
                        if event["disposition"] in ("unsupported", "incomplete") or kind == "gap":
                            counts["gap_or_unsupported_events"] += 1
                        if counts["deliverable_events"] % 16 == 0:
                            transport = drain()
                            if transport["blocked"] or transport["pending_events"]:
                                return {"status":"transport_blocked", **counts, "outbox":transport,
                                        "provider_calls":0,"private_source_text_in_report":False}
                if (counts["source_lines_advanced"] >= limits.max_lines or
                        counts["source_bytes_read"] >= limits.max_source_bytes or
                        counts["deliverable_events"] >= limits.max_events or
                        time.monotonic() - start >= limits.max_seconds):
                    break
        transport = drain()
        remaining = sum(
            db.execute("SELECT offset FROM progress WHERE manifest=? AND session=?",
                       (manifest["manifest_sha256"],s["session"])).fetchone()[0] < s["source_size"]
            for s in body["sessions"])
        status = "complete" if not remaining and not transport["pending_events"] else (
            "transport_blocked" if transport["blocked"] or transport["pending_events"] else "partial")
        return {"status":status, **counts, "remaining_sessions":remaining,
            "outbox":transport,"provider_calls":0,"private_source_text_in_report":False}
    finally:
        outbox.close()
        db.close()


def write_private(path, value):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("private_report_directory_permissions")
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name + ".",
                                     suffix=".tmp", delete=False) as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def main(argv=None):
    import argparse
    from agentclient.transport import EnterpriseLocal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "preflight", "apply", "status"))
    parser.add_argument("--home", required=True)
    parser.add_argument("--codex-home", default=str(Path.home()/".codex"))
    parser.add_argument("--roots-file")
    parser.add_argument("--manifest")
    parser.add_argument("--url")
    parser.add_argument("--credential-file")
    parser.add_argument("--max-sessions", type=int, default=1000)
    parser.add_argument("--max-lines", type=int, default=500)
    parser.add_argument("--max-source-bytes", type=int, default=256*1024*1024)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--max-events", type=int, default=500)
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.stat().st_mode & 0o077:
        raise ValueError("backfill_home_permissions")
    private = home / "backfills"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "plan":
        if not args.roots_file:
            parser.error("--roots-file required")
        roots_path = Path(args.roots_file).expanduser().resolve(strict=True)
        if roots_path.stat().st_mode & 0o077:
            raise ValueError("project_roots_file_permissions")
        manifest = discover(args.codex_home, json.loads(roots_path.read_text()),
                            max_sessions=args.max_sessions)
        path = write_private(private/"manifest.json", manifest)
        result = {"status":"planned","manifest":str(path),"manifest_sha256":manifest["manifest_sha256"],
                  "summary":manifest["summary"],"provider_calls":0}
    else:
        path = Path(args.manifest or private/"manifest.json").expanduser().resolve(strict=True)
        if (path.parent not in (home, private.resolve()) or path.stat().st_mode & 0o077):
            raise ValueError("private_manifest_required")
        manifest = json.loads(path.read_text())
        limits = Limits(args.max_lines,args.max_source_bytes,args.max_seconds,args.max_events)
        if args.command == "preflight":
            result = preflight(manifest, limits)
        elif args.command == "status":
            result = status_manifest(manifest,home)
        else:
            if not args.url or not args.credential_file:
                parser.error("apply requires --url and --credential-file")
            backend = EnterpriseLocal(args.url,args.credential_file,timeout=5)
            result = apply_manifest(manifest,home,backend,limits)
        write_private(private/(args.command+"-latest.json"),result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
