"""Operator backfill of past conversations held server-side (`vaelius backfill`).

For conversation history that already lives where the service runs (a transcript
archive, another memory service's export), not on people's computers. The input
is a generic event stream (docs/SELF_HOSTING.md, "Backfilling past conversations"):
one JSON object per line with a session id, a time, the author, the workspace, a
role (`user`, `assistant` or `tool`) and the text, optionally a tool name and paths.
A mapping file names the principal and the workspace each input author and
workspace becomes.

* **Always owner-private.** The service cannot see the author's computer, so it
  cannot evaluate scoped folders or origin labels. Every item is therefore stored
  private to its author: each event is sent without a sharing request, through a
  per-author connection enrolled `private`. There are no sharing options. Owners
  share afterwards, per session, with the ordinary owner tools
  (`vaelius-client share --session ...`).
* **Mapped or skipped.** An author or workspace missing from the mapping file, an
  unknown or inactive principal, an unknown workspace, or an author without the
  writer role there is skipped and counted, never guessed.
* **Dry run by default.** Counts per principal and workspace, never content.
  `--apply` ingests through `ingest_general`, the same path as live capture, so
  server redaction, legal hold, retention and erasure apply as to any item.
* **Idempotent and resumable.** Each event's source id comes from the principal,
  workspace, session and the event's position in its session (or its own `id`), so
  a re-run reports duplicates instead of writing twice. A run stops at its event or
  time budget and the next run continues.
* **Rate limited and audited.** Every write passes the tenant's write limits as the
  author (agenthub.write_limits) and waits out a rejection; each run, dry or not,
  writes one operator audit row with who ran it and the counts, and each item is
  audited as an ordinary ingest with the run id as its request id.
"""
import getpass
import hashlib
import json
from pathlib import Path
import re
import secrets
import time

from agentclient.enterprise_capture import normalize_capture

NAMESPACE = 'backfill'
ROLES = ('user', 'assistant', 'tool')
FIELDS = {'session', 'timestamp', 'author', 'workspace', 'role', 'text', 'tool', 'paths', 'id'}
MAX_LINE = 4 * 1024 * 1024
MAX_FILES = 10000
FRESHNESS = 30 * 86400
KEY = re.compile(r'[^\x00-\x1f]{1,256}')
PRINCIPAL = re.compile(r'[A-Za-z0-9_-]{1,80}')
EVENT_KIND = {'user': 'UserPromptSubmit', 'assistant': 'AssistantMessage', 'tool': 'PostToolUse'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _hash(*parts):
    return hashlib.sha256(_canonical(list(parts)).encode()).hexdigest()


def load_mapping(path):
    """{'principals': {input author: principal}, 'workspaces': {input workspace: workspace}}."""
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) - {'principals', 'workspaces'}:
        raise ValueError('invalid_backfill_mapping')
    result = {}
    for key in ('principals', 'workspaces'):
        table = value.get(key)
        if not isinstance(table, dict) or not table:
            raise ValueError('invalid_backfill_mapping:' + key)
        for name, target in table.items():
            if not KEY.fullmatch(name) or not isinstance(target, str) or not PRINCIPAL.fullmatch(target):
                raise ValueError('invalid_backfill_mapping:' + key)
        result[key] = dict(table)
    return result


def _files(source):
    source = Path(source)
    if source.is_dir():
        files = sorted(p for p in source.rglob('*.jsonl') if p.is_file())
        if len(files) > MAX_FILES:
            raise ValueError('backfill_too_many_files')
        return files
    if source.is_file():
        return [source]
    raise ValueError('backfill_input_not_found')


def _record(raw):
    """A validated input event, or None."""
    if len(raw) > MAX_LINE:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) - FIELDS:
        return None
    for key in ('session', 'author', 'workspace'):
        if not isinstance(value.get(key), str) or not KEY.fullmatch(value[key]):
            return None
    if value.get('role') not in ROLES or not isinstance(value.get('text'), str):
        return None
    if 'tool' in value and not (isinstance(value['tool'], str) and 0 < len(value['tool']) <= 128):
        return None
    paths = value.get('paths', [])
    if not isinstance(paths, list) or len(paths) > 100 or not all(isinstance(p, str) and len(p) <= 4096 for p in paths):
        return None
    if 'id' in value and not (isinstance(value['id'], str) and KEY.fullmatch(value['id'])):
        return None
    stamp = value.get('timestamp')
    if not (isinstance(stamp, str) and len(stamp) <= 64) and not (type(stamp) in (int, float) and stamp >= 0):
        return None
    return value


def read(source):
    """(position, record or None) for every input line, in file then line order."""
    for file in _files(source):
        with file.open('rb') as stream:
            for number, line in enumerate(stream):
                if line.strip():
                    yield (str(file), number), _record(line)


def event(record, workspace, connection, external_id):
    """One input record as a general event; owner-private (no sharing request, ever)."""
    kind = EVENT_KIND[record['role']]
    hook = {'hook_event_name': kind, 'session_id': record['session'], 'timestamp': record.get('timestamp', 'unknown')}
    if kind == 'UserPromptSubmit':
        hook['prompt'] = record['text']
    elif kind == 'AssistantMessage':
        hook.update(message=record['text'], channel='final')
    else:
        hook.update(tool_name=record.get('tool') or 'tool', tool_input={'paths': list(record.get('paths', []))},
                    tool_response={'text': record['text']})
    value = normalize_capture(hook, workspace, connection, source_type='conversation', origin='server_backfill',
                              sharing='private')
    value['external_id'] = external_id
    value['revision'] = '1'
    if 'sharing' in value:
        raise ValueError('backfill_must_be_private')
    return value


def _connection_id(tenant, principal, workspace):
    return 'backfill-' + _hash(tenant, principal, workspace)[:32]


def _context(store, principal, connection, run):
    """The author's capture context for the operator's run: `ingest` only, its own connection."""
    return {'tenant': store.tenant_id, 'principal': principal, 'actor': principal, 'acting_for': None,
            'enrollment': connection, 'actions': {'ingest'}, 'delegated_projects': None,
            'request_id': 'operator-backfill:' + run}


def _resolve(store, mapping, records):
    """{(principal, workspace id): [(external id, record)]} plus skip counts."""
    skipped = {'invalid': 0, 'unmapped_author': 0, 'unmapped_workspace': 0, 'unknown_principal': 0,
               'unknown_workspace': 0, 'not_writer': 0}
    groups, positions, known = {}, {}, {}
    with store.open() as state:
        db = state.db
        for _, record in records:
            if record is None:
                skipped['invalid'] += 1
                continue
            principal = mapping['principals'].get(record['author'])
            target = mapping['workspaces'].get(record['workspace'])
            if principal is None:
                skipped['unmapped_author'] += 1
                continue
            if target is None:
                skipped['unmapped_workspace'] += 1
                continue
            if (principal, target) not in known:
                row = db.execute('SELECT active FROM enterprise_principals WHERE tenant=? AND id=?',
                                 (store.tenant_id, principal)).fetchone()
                workspace = store._resolve_workspace(db, store.tenant_id, target, active_only=True)
                if not row or not row['active']:
                    known[(principal, target)] = 'unknown_principal'
                elif workspace is None:
                    known[(principal, target)] = 'unknown_workspace'
                elif not store._project_member(db, {'tenant': store.tenant_id, 'actor': principal,
                                                    'delegated_projects': None}, workspace['id'], 2):
                    known[(principal, target)] = 'not_writer'
                else:
                    known[(principal, target)] = workspace['id']
            resolved = known[(principal, target)]
            if resolved in skipped:
                skipped[resolved] += 1
                continue
            session_key = (principal, resolved, record['session'])
            index = positions.get(session_key, 0)
            positions[session_key] = index + 1
            ident = 'backfill:' + _hash(principal, resolved, record['session'], record.get('id', index))
            groups.setdefault((principal, resolved), []).append((ident, record))
    return groups, skipped


def _ensure_connection(store, principal, workspace, run):
    """The author's private backfill connection in the workspace, created or re-observed."""
    ident = _connection_id(store.tenant_id, principal, workspace)
    with store.delivery_lock(), store.open() as state, state.db:
        row = state.db.execute('SELECT * FROM backend_connections WHERE id=?', (ident,)).fetchone()
        if row is not None:
            if (row['owner'] != principal or row['visibility'] != 'private' or row['namespace'] != NAMESPACE
                    or json.loads(row['reader_ids'])):
                raise ValueError('backfill_connection_not_private')
            state.db.execute('UPDATE backend_connections SET permission_observed=?,active=1 WHERE id=?',
                             (time.time(), ident))
            return ident
    store.enroll_connection(_context(store, principal, ident, run), ident, NAMESPACE, workspace, ['conversation'],
                            visibility='private', freshness_seconds=FRESHNESS)
    return ident


def refresh_connections(store):
    """Re-observe every backfill connection whose author is active and still a writer.

    A connection whose permission was not observed within its freshness window hides
    its items, from their owner too. Run this at least every 30 days (it is also done
    for each connection an `--apply` run writes to)."""
    refreshed = expired = 0
    with store.delivery_lock(), store.open() as state, state.db:
        db = state.db
        rows = db.execute('SELECT id,owner,project FROM backend_connections WHERE tenant=? AND namespace=? AND active=1',
                          (store.tenant_id, NAMESPACE)).fetchall()
        for row in rows:
            ctx = {'tenant': store.tenant_id, 'actor': row['owner'], 'delegated_projects': None}
            active = db.execute('SELECT active FROM enterprise_principals WHERE tenant=? AND id=?',
                                (store.tenant_id, row['owner'])).fetchone()
            if active and active['active'] and store._project_member(db, ctx, row['project'], 2):
                db.execute('UPDATE backend_connections SET permission_observed=? WHERE id=?', (time.time(), row['id']))
                refreshed += 1
            else:
                expired += 1
        store._operator_audit(db, 'server_backfill_refresh', 'applied',
                              _canonical({'refreshed': refreshed, 'left_to_expire': expired}))
    return {'refreshed': refreshed, 'left_to_expire': expired}


def run(store, source, mapping, *, apply=False, max_events=5000, max_seconds=600, run_by=None,
        sleep=time.sleep, clock=time.monotonic):
    """Dry run (counts only) or apply one backfill; returns counts, never content."""
    from agenthub.enterprise import Conflict, Denied
    from agenthub.write_limits import RateLimited, WriteTooLarge, check
    if type(max_events) is not int or not 1 <= max_events <= 1_000_000 or not 1 <= max_seconds <= 86400:
        raise ValueError('invalid_backfill_budget')
    mapping = load_mapping(mapping) if not isinstance(mapping, dict) else mapping
    run_id = secrets.token_hex(8)
    run_by = run_by or getpass.getuser()
    groups, skipped = _resolve(store, mapping, read(source))
    report = {'dry_run': not apply, 'run': run_id, 'skipped': skipped, 'sharing': 'private',
              'groups': [{'principal': p, 'workspace': w, 'sessions': len({r['session'] for _, r in items}),
                          'events': len(items)} for (p, w), items in sorted(groups.items())]}
    written = {'accepted': 0, 'redacted': 0, 'duplicate': 0, 'conflict': 0, 'denied': 0, 'too_large': 0}
    stopped = None
    if apply:
        started, done = clock(), 0
        for (principal, workspace), items in sorted(groups.items()):
            if stopped:
                break
            connection = _ensure_connection(store, principal, workspace, run_id)
            ctx = _context(store, principal, connection, run_id)
            for ident, record in items:
                if done >= max_events:
                    stopped = 'event_budget'
                    break
                value = event(record, workspace, connection, ident)
                size = len(json.dumps(value, ensure_ascii=False).encode())
                while True:
                    try:
                        check(store, ctx, size)
                        break
                    except RateLimited as limited:
                        wait = getattr(limited, 'retry_after', 1)
                        if clock() - started + wait > max_seconds:
                            stopped = 'rate_limited'
                            break
                        sleep(wait)
                    except WriteTooLarge:
                        stopped = 'too_large'
                        break
                if stopped == 'too_large':
                    written['too_large'] += 1
                    stopped = None
                    continue
                if stopped:
                    break
                try:
                    result = store.ingest_general(ctx, value)
                    written[result['disposition'] if result['disposition'] in written else 'accepted'] += 1
                except Conflict:
                    written['conflict'] += 1
                except Denied:
                    written['denied'] += 1
                done += 1
                if clock() - started > max_seconds:
                    stopped = 'time_budget'
                    break
        report['written'] = written
        if stopped:
            report['stopped'] = stopped
            report['resume'] = 'run the same command again; written events are reported as duplicates'
    summary = {'run': run_id, 'run_by': run_by, 'apply': bool(apply), 'skipped': skipped,
               'groups': [[g['principal'], g['workspace'], g['events']] for g in report['groups']],
               'written': written if apply else None, 'stopped': stopped}
    with store.open() as state, state.db:
        store._operator_audit(state.db, 'server_backfill', 'applied' if apply else 'dry_run', _canonical(summary))
    return report
