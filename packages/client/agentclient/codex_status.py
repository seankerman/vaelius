"""Narrow compatibility adapter for Codex 0.155 CommandExecution completion metadata.

Only the hook-provided selected-session file is read. No transcript text is copied.
Unknown/missing formats stay unknown; they never imply successful execution.
"""
import json
from pathlib import Path


def read_exit_code(path, session, turn, call, allowed_root=None):
    if not path or not session or not call:return None
    p=Path(path).resolve()
    roots=[Path(allowed_root).resolve()] if allowed_root else [(Path.home()/'.codex/sessions').resolve(),(Path.home()/'.codex/archived_sessions').resolve()]
    if not any(root in p.parents for root in roots) or session not in p.name or p.suffix!='.jsonl':return None
    try:
        with p.open('rb') as f:
            size=p.stat().st_size;start=max(0,size-1_048_576);f.seek(start)
            if start:f.readline()
            lines=f.read(1_048_576).splitlines()
        for line in reversed(lines):
            if b'"item_completed"' not in line:continue
            try:d=json.loads(line)
            except (ValueError,UnicodeDecodeError):continue
            payload=d.get('payload',{});item=payload.get('item',{})
            if d.get('type')=='event_msg' and payload.get('type')=='item_completed' and payload.get('thread_id')==session and payload.get('turn_id')==turn and item.get('id')==call and item.get('type')=='CommandExecution':
                code=item.get('exit_code')
                return code if isinstance(code,int) and not isinstance(code,bool) else None
    except OSError:pass
    return None
