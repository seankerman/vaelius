"""Trusted context boundaries and compact delivery decisions for local memory.

Rollout compaction is metadata, never source evidence. Automatic delivery may use
these hints to avoid repeating an unchanged fact in an observed context epoch;
it cannot infer what a model remembers from a session ID or a token estimate.
"""

from __future__ import annotations

from agenthub.processing.storage import begin_write

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import time


def initialize(db):
    db.require_schema()


@contextmanager
def _write(db):
    """Own a short write transaction, or nest safely in an existing transaction."""
    own = not db.in_transaction
    if own:
        begin_write(db)
    else:
        db.execute('SAVEPOINT context_write')
    try:
        yield
    except BaseException:
        if own:
            db.rollback()
        else:
            db.execute('ROLLBACK TO context_write')
            db.execute('RELEASE context_write')
        raise
    else:
        if own:
            db.commit()
        else:
            db.execute('RELEASE context_write')


def _ensure_state(db, session):
    db.execute("""INSERT INTO context_state(session,updated)
        VALUES(?,?) ON CONFLICT DO NOTHING""", (session, time.time()))














def record_boundary(db, boundary):
    """Advance once per trusted compaction, including after cursor replay."""
    initialize(db)
    if (not isinstance(boundary, dict) or boundary.get('kind') != 'compacted'
        or boundary.get('source_adapter') not in {'scoped_desktop_rollout','codex_postcompact'}
        or not isinstance(boundary.get('session'), str)
        or not isinstance(boundary.get('boundary_id'), str)
        or type(boundary.get('sequence')) is not int):
        raise ValueError('untrusted_context_boundary')
    session = boundary['session']
    with _write(db):
        _ensure_state(db, session)
        if db.execute('SELECT 1 FROM context_boundaries WHERE session=? AND boundary_id=?',
                      (session, boundary['boundary_id'])).fetchone():
            return False
        current = db.execute('SELECT epoch,latest_sequence FROM context_state WHERE session=?',
                             (session,)).fetchone()
        applied = boundary['sequence'] > current[1]
        db.execute("""INSERT INTO context_boundaries(session,boundary_id,kind,
            observed_at,source_adapter,sequence,source_cursor,applied,created)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (session, boundary['boundary_id'], boundary['kind'], boundary['observed_at'],
             boundary['source_adapter'], boundary['sequence'], boundary.get('source_cursor'),
             int(applied), time.time()))
        if applied:
            db.execute("""UPDATE context_state SET epoch=?,boundary_id=?,latest_sequence=?,
                boundary_support=?,continuity_status='known',
                coverage_status='unknown',uncertain_since=0,gap_reason=NULL,
                updated=? WHERE session=?""",
                (current[0]+1, boundary['boundary_id'], boundary['sequence'],
                 'codex_postcompact' if boundary['source_adapter']=='codex_postcompact'
                 else 'scoped_rollout', time.time(), session))
        return applied


def record_postcompact_boundary(db, event):
    """Advance from Codex's dedicated after-compaction hook, once per turn.

    This only accepts the event-specific host shape. It records no transcript
    content or claimed retained facts. A repeated hook invocation is idempotent.
    """
    if (not isinstance(event,dict) or event.get('hook_event_name')!='PostCompact'
            or event.get('trigger') not in {'manual','auto'}
            or not isinstance(event.get('session_id'),str) or not event['session_id']
            or not isinstance(event.get('turn_id'),str) or not event['turn_id']):
        raise ValueError('untrusted_postcompact_event')
    session=event['session_id']
    stable=[session,event['turn_id'],event['trigger']]
    boundary_id=hashlib.sha256(json.dumps(stable,separators=(',',':')).encode()).hexdigest()
    if db.execute('SELECT 1 FROM context_boundaries WHERE session=? AND boundary_id=?',
                  (session,boundary_id)).fetchone():
        return False
    sequence=current_epoch(db,session)['latest_sequence']+1
    return record_boundary(db,{'session':session,'boundary_id':boundary_id,
        'kind':'compacted','source_adapter':'codex_postcompact',
        'sequence':sequence,'observed_at':datetime.now().astimezone().isoformat()})


def current_epoch(db, session):
    initialize(db)
    row = db.execute("""SELECT epoch,boundary_id,latest_sequence,observed_cursor,
        observed_turn,boundary_support,
        continuity_status,coverage_status,uncertain_since,gap_reason
        FROM context_state WHERE session=?""",
        (session,)).fetchone()
    if not row:
        return {'session': session, 'epoch': 0, 'boundary_id': None,
                'latest_sequence': -1, 'observed_cursor': None, 'observed_turn': None,
                'boundary_support': 'unknown',
                'continuity_status': 'unknown', 'coverage_status': 'unknown',
                'uncertain_since': 0,'gap_reason':None}
    return dict(row,session=session)
