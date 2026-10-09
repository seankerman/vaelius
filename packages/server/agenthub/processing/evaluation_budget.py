"""Private, persistent attempt accounting for the local evaluation campaign."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time


# October 1, 2026: the owner removed artificial authorization ceilings.
# Keep optional limits for explicitly configured diagnostic tests; the working
# campaign is accounting-only. Existing attempts and failed receipts are retained.
TOTAL_ATTEMPT_CAP = None
RETRY_ATTEMPT_CAP = None
PHASE_CAPS = dict.fromkeys(("live_baseline", "live_curation", "consolidation", "task_retrieval"))


def _remaining(cap, count):
    return None if cap is None else max(0, cap - count)


def _connect(path):
    path=Path(path).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.parent.chmod(0o700)
    db=sqlite3.connect(path,timeout=2)
    os.chmod(path,0o600)
    db.execute("PRAGMA busy_timeout=2000")
    db.execute("""CREATE TABLE IF NOT EXISTS evaluation_attempts(
        attempt_id TEXT PRIMARY KEY,phase TEXT NOT NULL,status TEXT NOT NULL,
        created REAL NOT NULL,updated REAL NOT NULL,input_tokens INTEGER,
        output_tokens INTEGER,reasoning_tokens INTEGER,is_retry INTEGER NOT NULL DEFAULT 0)""")
    if "is_retry" not in {row[1] for row in db.execute("PRAGMA table_info(evaluation_attempts)")}:
        db.execute("ALTER TABLE evaluation_attempts ADD COLUMN is_retry INTEGER NOT NULL DEFAULT 0")
    columns = {row[1] for row in db.execute("PRAGMA table_info(evaluation_attempts)")}
    for column in ("cached_input_tokens", "cache_write_input_tokens"):
        if column not in columns:
            try: db.execute("ALTER TABLE evaluation_attempts ADD COLUMN " + column + " INTEGER")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc): raise
    db.execute("CREATE INDEX IF NOT EXISTS evaluation_attempt_phase ON evaluation_attempts(phase)")
    db.commit()
    return db


def snapshot(path):
    db=_connect(path)
    try:
        total=db.execute("SELECT count(*) FROM evaluation_attempts").fetchone()[0]
        retries=db.execute("SELECT count(*) FROM evaluation_attempts WHERE is_retry=1").fetchone()[0]
        phases={phase:db.execute("SELECT count(*) FROM evaluation_attempts WHERE phase=? AND is_retry=0",(phase,)).fetchone()[0]
                for phase in PHASE_CAPS}
        phase_retries={phase:db.execute('SELECT count(*) FROM evaluation_attempts WHERE phase=? AND is_retry=1',(phase,)).fetchone()[0]
                       for phase in PHASE_CAPS}
        counts={phase:count+(phase_retries[phase] if phase=='task_retrieval' else 0) for phase,count in phases.items()}
        return {"attempts":total,"total_cap":TOTAL_ATTEMPT_CAP,
            "remaining":_remaining(TOTAL_ATTEMPT_CAP,total),
            "retry_attempts":retries,"retry_cap":RETRY_ATTEMPT_CAP,
            "retry_remaining":_remaining(RETRY_ATTEMPT_CAP,retries),
            "phases":{phase:{"attempts":count,"fresh_attempts":phases[phase],
                "retry_attempts":phase_retries[phase],"total_attempts":phases[phase]+phase_retries[phase],
                "cap":PHASE_CAPS[phase],"remaining":_remaining(PHASE_CAPS[phase],count)} for phase,count in counts.items()}}
    finally:db.close()


def reserve(path,attempt_id,phase,*,retry=False):
    if phase not in PHASE_CAPS:raise ValueError("invalid_evaluation_phase")
    db=_connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM evaluation_attempts WHERE attempt_id=?",(attempt_id,)).fetchone():
            raise ValueError("evaluation_attempt_already_reserved")
        total=db.execute("SELECT count(*) FROM evaluation_attempts").fetchone()[0]
        phase_count=db.execute("SELECT count(*) FROM evaluation_attempts WHERE phase=? AND is_retry=0",(phase,)).fetchone()[0]
        if phase=='task_retrieval':
            phase_count=db.execute('SELECT count(*) FROM evaluation_attempts WHERE phase=?',(phase,)).fetchone()[0]
        retry_count=db.execute("SELECT count(*) FROM evaluation_attempts WHERE is_retry=1").fetchone()[0]
        if ((TOTAL_ATTEMPT_CAP is not None and total>=TOTAL_ATTEMPT_CAP) or
                (retry and RETRY_ATTEMPT_CAP is not None and retry_count>=RETRY_ATTEMPT_CAP) or
                ((not retry or phase=='task_retrieval') and PHASE_CAPS[phase] is not None and phase_count>=PHASE_CAPS[phase])):
            raise ValueError("evaluation_campaign_budget_exhausted")
        now=time.time()
        db.execute("INSERT INTO evaluation_attempts(attempt_id,phase,status,created,updated,is_retry) VALUES(?,?, 'started',?,?,?)",
                   (attempt_id,phase,now,now,int(bool(retry))))
        db.commit()
        return snapshot(path)
    except Exception:
        db.rollback()
        raise
    finally:db.close()


def finish(path,attempt_id,status,usage=None):
    if status not in ("complete","failed"):raise ValueError("invalid_evaluation_attempt_status")
    usage=usage or {}
    db=_connect(path)
    try:
        with db:
            changed=db.execute("""UPDATE evaluation_attempts SET status=?,updated=?,input_tokens=?,
                output_tokens=?,reasoning_tokens=?,cached_input_tokens=?,cache_write_input_tokens=? WHERE attempt_id=?""",
                (status,time.time(),usage.get("input_tokens"),usage.get("output_tokens"),
                 usage.get("reasoning_output_tokens"),usage.get("cached_input_tokens"),
                 usage.get("cache_write_input_tokens"),attempt_id)).rowcount
            if not changed:raise ValueError("evaluation_attempt_not_reserved")
    finally:db.close()
