"""Finite, resumable local operator import through the canonical part intake.

This is an explicit backfill operation, not a service daemon or plugin hook. It
uses an existing private credential and the same store method as the HTTP API.
No model runs here. The client importer owns redaction, outbox and byte cursors.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import stat
import time

from agentclient.enterprise_backfill import Limits, apply_manifest, write_private
from agentclient.general_contract import EVENT_BYTES, validate_event, validate_part, validate_response
from agenthub.cloud_local import runtime


class LocalPartBackend:
    def __init__(self, profile, credential_file):
        path = Path(credential_file).expanduser().resolve(strict=True)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("private_credential_required")
        self.token = path.read_text().strip()
        _, self.registry = runtime(Path(profile).expanduser().resolve(strict=True))
        self.last_error_type = None
        self._parts = None

    def request(self, path, data):
        if path != "/enterprise/v2/parts":
            raise ValueError("historical_part_route_only")
        try:
            validate_part(data)
            key = (data["connection"], data["external_id"], data["revision"],
                   data["digest"], data["part_count"])
            if data["part_index"] == 0:
                self._parts = {"key": key, "data": []}
            if self._parts is None or self._parts["key"] != key or data["part_index"] != len(self._parts["data"]):
                raise ValueError("historical_part_sequence")
            self._parts["data"].append(base64.b64decode(data["data"], validate=True))
            if len(self._parts["data"]) < data["part_count"]:
                return validate_response(path, {"disposition": "incomplete", "digest": data["digest"],
                    "received_parts": len(self._parts["data"]), "complete": False, "source_id": ""})
            raw = b"".join(self._parts["data"])
            self._parts = None
            if len(raw) > EVENT_BYTES or hashlib.sha256(raw).hexdigest() != data["digest"]:
                raise ValueError("historical_part_digest")
            event = validate_event(json.loads(raw))
            if (event["connection"], event["external_id"], event["revision"]) != key[:3]:
                raise ValueError("historical_part_identity")
            store = self.registry.store_for_token(self.token)
            ctx = store.authenticate(self.token)
            store.require_ready()
            result = store.ingest_general(ctx, event)
            return validate_response(path, {"disposition": result["disposition"],
                "digest": data["digest"], "received_parts": data["part_count"],
                "complete": True, "source_id": result["source_id"]})
        except Exception as exc:
            self.last_error_type = type(exc).__name__
            raise


def run(profile, credential_file, manifest_file, home, *, limits=Limits(),
        max_slices=1, max_total_seconds=3600):
    if type(max_slices) is not int or not 1 <= max_slices <= 1000:
        raise ValueError("invalid_slice_bound")
    if type(max_total_seconds) is not int or not 1 <= max_total_seconds <= 3600:
        raise ValueError("invalid_total_time_bound")
    source = Path(manifest_file).expanduser().resolve(strict=True)
    if stat.S_IMODE(source.stat().st_mode) & 0o077:
        raise ValueError("private_manifest_required")
    manifest = json.loads(source.read_text())
    target = Path(home).expanduser().resolve()
    backend = LocalPartBackend(profile, credential_file)
    start = time.monotonic()
    slices = []
    for _ in range(max_slices):
        if time.monotonic() - start >= max_total_seconds:
            break
        remaining = max(1, int(max_total_seconds - (time.monotonic() - start)))
        bounded = Limits(limits.max_lines, limits.max_source_bytes,
                         min(limits.max_seconds, remaining), limits.max_events)
        result = apply_manifest(manifest, target, backend, bounded)
        slices.append({key: result.get(key, 0) for key in
                       ("status", "source_lines_advanced", "source_bytes_read",
                        "deliverable_events", "acknowledged_events")}
                      | {"remaining_sessions": result.get("remaining_sessions")})
        write_private(target / "backfill-progress.json", {
            "status": result["status"], "slices": len(slices),
            "lines_advanced": sum(s["source_lines_advanced"] for s in slices),
            "events_acknowledged": sum(s["acknowledged_events"] for s in slices),
            "remaining_sessions": result.get("remaining_sessions"), "provider_calls": 0})
        if result["status"] in ("complete", "transport_blocked"):
            break
    summary = {"status": slices[-1]["status"] if slices else "time_bound",
               "slices": len(slices), "lines_advanced": sum(s["source_lines_advanced"] for s in slices),
               "events_queued": sum(s["deliverable_events"] for s in slices),
               "events_acknowledged": sum(s["acknowledged_events"] for s in slices),
               "remaining_sessions": slices[-1]["remaining_sessions"] if slices else None,
               "last_error_type": backend.last_error_type,
               "provider_calls": 0, "private_source_text_in_report": False}
    write_private(target / "backfill-run-latest.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--max-slices", type=int, default=1)
    parser.add_argument("--max-total-seconds", type=int, default=3600)
    parser.add_argument("--max-lines", type=int, default=500)
    parser.add_argument("--max-source-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--max-events", type=int, default=500)
    args = parser.parse_args(argv)
    result = run(args.profile, args.credential_file, args.manifest, args.home,
                 limits=Limits(args.max_lines, args.max_source_bytes,
                               args.max_seconds, args.max_events),
                 max_slices=args.max_slices, max_total_seconds=args.max_total_seconds)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
