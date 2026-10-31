"""`vaelius-client push-session`: send one past conversation of your own, deliberately.

Live capture is forward-only. This command lets a person push one earlier
conversation, one per invocation: a Claude Code transcript
(`~/.claude/projects/<folder>/<session>.jsonl`, with that session's subagent
transcripts) or a Codex rollout (`~/.codex/sessions/.../rollout-*-<session>.jsonl`).
There is no bulk mode.

* **Only your own.** The file must be under this computer's Claude Code
  configuration folder (`CLAUDE_CONFIG_DIR`, default `~/.claude`) or Codex home
  (`CODEX_HOME`, default `~/.codex`). A subagent transcript is pushed only as part
  of its parent session.
* **Only from a scoped folder where you are a writer.** The session must have
  started in a folder whose scope file (or explicit enrollment) has capture on, and
  the service must report the writer or admin role there. Anything else is refused.
* **Always owner-private.** Every event is sent without a sharing request, which the
  service stores as private to you, whatever the workspace default (there is no
  per-item sharing afterwards).
* **The live privacy rules, replayed.** Events go through the same origin
  classifier (`agentclient.origin`), sticky session taint and redaction as live
  capture, in order: a read of a personal folder, an unknown path, auto memory, a
  non-allowlisted MCP server or another workspace restricts that event and every
  later one, and carried workspaces are recorded. The replay runs as an unlabelled
  session, so a read of another workspace carries that workspace and its scope
  file's ceiling; that is never wider than the labelled rule. Taint state is kept
  in a temporary folder, never mixed with live sessions.
* **Dry run by default.** It prints counts only, never content. `--apply` queues
  the events in the capture outbox, which delivers them at the service's rate.
* **Idempotent and resumable.** Each event's source id comes from the session id
  and the event's position in the file, so a re-run sends nothing twice; what was
  queued is recorded in the profile (`pushed-sessions.json`, 0600).
* **Read only.** Transcript files are opened for reading and never changed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

RECEIPTS = 'pushed-sessions.json'
SESSION = re.compile(r'[A-Za-z0-9_-]{8,128}')
MAX_LINE = 32 * 1024 * 1024
MAX_EVENTS = 20000
BATCH = 64
RECENT = 20


class PushRefused(ValueError):
    """The session may not be pushed; `str()` is the reason code."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def source_id(host, session, index):
    """The stable external id of one pushed event: the session plus the event's position."""
    return 'push:' + hashlib.sha256(_canonical([host, session, index]).encode()).hexdigest()


def claude_dir(explicit=None):
    from agentclient.claude_code import config_dir
    return config_dir(explicit)


def codex_dir(explicit=None):
    return Path(explicit or os.environ.get('CODEX_HOME') or '~/.codex').expanduser().resolve()


def _inside(path, root):
    return path == root or root in path.parents


# ---- Finding the transcript ----------------------------------------------------------

def _first_json(path):
    try:
        with Path(path).open('rb') as stream:
            for _, line in zip(range(200), stream):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _codex_session(path):
    for record in _first_json(path):
        payload = record.get('payload')
        if record.get('type') == 'session_meta' and isinstance(payload, dict):
            return payload.get('id'), payload.get('cwd')
        break
    return None, None


def _claude_start(path):
    """(session id, the folder the session started in) from the first entries."""
    session = cwd = None
    for entry in _first_json(path):
        session = session or (entry.get('sessionId') if isinstance(entry.get('sessionId'), str) else None)
        if isinstance(entry.get('cwd'), str) and entry['cwd']:
            cwd = entry['cwd']
        if session and cwd:
            break
    return session, cwd


def _claude_file(path, claude):
    """A main Claude Code transcript: `<config>/projects/<folder>/<session>.jsonl`."""
    projects = claude / 'projects'
    return path.suffix == '.jsonl' and path.parent.parent == projects


def _codex_file(path, codex):
    return path.suffix == '.jsonl' and path.name.startswith('rollout-') and _inside(path, codex / 'sessions')


def locate(target, *, claude=None, codex=None):
    """(host, session id, transcript path, start folder) for a session id or transcript path."""
    claude, codex = claude or claude_dir(), codex or codex_dir()
    candidate = Path(os.path.expanduser(str(target)))
    if candidate.is_absolute() or os.sep in str(target) or str(target).endswith('.jsonl'):
        path = candidate.resolve()
        if not path.is_file():
            raise PushRefused('transcript_not_found')
        if _claude_file(path, claude):
            session, cwd = _claude_start(path)
            if session != path.stem:
                raise PushRefused('transcript_identity')
            return 'claude-code', session, path, cwd
        if _codex_file(path, codex):
            session, cwd = _codex_session(path)
            if not isinstance(session, str) or not path.name.endswith(session + '.jsonl'):
                raise PushRefused('transcript_identity')
            return 'codex', session, path, cwd
        if _inside(path, claude / 'projects') and 'subagents' in path.parts:
            raise PushRefused('subagent_transcript_push_the_parent_session')
        raise PushRefused('not_your_transcript')
    if not SESSION.fullmatch(str(target)):
        raise PushRefused('invalid_session_id')
    found = [('claude-code', p) for p in sorted((claude / 'projects').glob('*/' + str(target) + '.jsonl'))]
    found += [('codex', p) for p in sorted((codex / 'sessions').rglob('rollout-*-' + str(target) + '.jsonl'))]
    if not found:
        raise PushRefused('session_not_found')
    if len(found) > 1:
        raise PushRefused('session_ambiguous_pass_the_transcript_path')
    return locate(found[0][1].resolve(), claude=claude, codex=codex)


def recent(config, *, limit=RECENT, claude=None, codex=None):
    """The most recently changed transcripts, newest first: host, session, time, start folder, scope."""
    claude, codex = claude or claude_dir(), codex or codex_dir()
    files = [p for p in (claude / 'projects').glob('*/*.jsonl')] + list((codex / 'sessions').rglob('rollout-*.jsonl'))
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(int(limit), 200))]
    rows = []
    for path in files:
        try:
            host, session, path, cwd = locate(path.resolve(), claude=claude, codex=codex)
        except PushRefused:
            continue
        scope = _scope(config, session, cwd)
        rows.append({'host': host, 'session': session, 'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(path.stat().st_mtime)),
                     'started_in': cwd, 'workspace': scope['project'] if scope else None,
                     'pushable': bool(scope)})
    return {'sessions': rows}


def _scope(config, session, cwd):
    """The capture scope of the session's starting folder when capture is on there, else None."""
    from agentclient.hooks import resolve_scope
    from agentclient.session_label import LABELLED_ORIGINS, NAME
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        scope = resolve_scope(config, {'session_id': session, 'cwd': cwd})
    except Exception:
        return None
    if (not scope or scope.get('capture') != 'on' or scope.get('origin') not in LABELLED_ORIGINS
            or not NAME.fullmatch(str(scope.get('project')))):
        return None
    return scope


# ---- Replaying a Claude Code transcript ---------------------------------------------

def _lines(path):
    """(line number, parsed entry or None) for every line; never writes."""
    with Path(path).open('rb') as stream:
        for number, line in enumerate(stream):
            if len(line) > MAX_LINE:
                yield number, None
                continue
            try:
                value = json.loads(line)
            except ValueError:
                yield number, None
                continue
            if isinstance(value, dict):
                yield number, value


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b['text'] for b in content if isinstance(b, dict) and b.get('type') == 'text'
                         and isinstance(b.get('text'), str))
    return ''


def claude_events(path, session, start):
    """(index, hook-shaped event) for a Claude Code session, in order, as live hooks see it.

    Prompts, each tool call with its result, and the final reply of each turn, like
    the live hooks; a subagent's tool calls (its own transcript, or inline sidechain
    entries) belong to the parent session, its prompts and replies do not."""
    path = Path(path)
    files = [('main', path)] + [('agent-' + p.stem, p) for p in sorted((path.parent / path.stem / 'subagents').glob('*.jsonl'))]
    rows = []
    for rank, (tag, file) in enumerate(files):
        for number, entry in _lines(file):
            stamp = str(entry.get('timestamp') or '') if entry else ''
            rows.append((stamp, rank, number, tag, entry))
    if len(files) > 1:
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
    pending, turn, last = {}, '', None
    base = {'session_id': session, 'transcript_path': str(path)}

    def stop():
        if last:
            key, text, stamp, cwd = last
            yield 'stop:' + key, dict(base, hook_event_name='Stop', last_assistant_message=text, prompt_id=turn,
                                      timestamp=stamp or 'unknown', cwd=cwd)

    for stamp, _, number, tag, entry in rows:
        if entry is None:
            yield tag + ':' + str(number), dict(base, hook_event_name='gap', prompt_id=turn, cwd=start)
            continue
        side = tag != 'main' or entry.get('isSidechain') is True
        cwd = entry.get('cwd') if isinstance(entry.get('cwd'), str) and entry.get('cwd') else start
        message = entry.get('message') if isinstance(entry.get('message'), dict) else {}
        content = message.get('content')
        kind = entry.get('type')
        common = dict(base, cwd=cwd, timestamp=stamp or 'unknown')
        if kind == 'system' and entry.get('subtype') == 'compact_boundary' and not side:
            trigger = (entry.get('compactMetadata') or {}).get('trigger', 'unknown')
            yield tag + ':' + str(number), dict(common, hook_event_name='PostCompact', trigger=str(trigger), prompt_id=turn)
        elif kind == 'user' and isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            results = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result']
            for block_no, block in enumerate(content):
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                call = pending.pop(str(block.get('tool_use_id')), None)
                if call is None:
                    continue
                name, arguments, call_cwd = call
                response = entry.get('toolUseResult') if len(results) == 1 and 'toolUseResult' in entry else block.get('content')
                event = dict(common, cwd=call_cwd, tool_name=name, tool_input=arguments, prompt_id=turn,
                             tool_use_id=str(block.get('tool_use_id')))
                if block.get('is_error') is True:
                    event.update(hook_event_name='PostToolUseFailure', error=_text(block.get('content')) or 'error')
                else:
                    event.update(hook_event_name='PostToolUse', tool_response=response)
                yield tag + ':' + str(number) + ':' + str(block_no), event
        elif kind == 'user' and not side and not entry.get('isMeta') and not entry.get('isCompactSummary'):
            text = _text(content)
            if text.strip():
                yield from stop()
                last = None
                turn = str(entry.get('promptId') or entry.get('uuid') or tag + ':' + str(number))
                yield tag + ':' + str(number), dict(common, hook_event_name='UserPromptSubmit', prompt=text, prompt_id=turn)
        elif kind == 'assistant' and isinstance(content, list):
            for block_no, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and block.get('id'):
                    arguments = block.get('input') if isinstance(block.get('input'), dict) else {}
                    pending[str(block['id'])] = (str(block.get('name', '')), arguments, cwd)
                elif block.get('type') == 'text' and not side and isinstance(block.get('text'), str) and block['text'].strip():
                    last = (tag + ':' + str(number) + ':' + str(block_no), block['text'], stamp, cwd)
    yield from stop()


def _claude_values(config, scope, session, path, start, connection, state, remote):
    """The session's events as wire values, labelled and redacted exactly as live capture."""
    from agentclient import origin as origin_labels
    from agentclient.enterprise_capture import normalize_capture
    from agentclient.hooks import TOOL_EVENTS
    taint = origin_labels.SessionOrigins(state)
    contexts = {}
    project = scope['project']
    for index, event in claude_events(path, session, start):
        kind = event['hook_event_name']
        try:
            label = origin_labels.NEUTRAL
            if kind in TOOL_EVENTS:
                cwd = event.get('cwd')
                if cwd not in contexts:
                    contexts[cwd] = origin_labels.Context(scope, cwd, config)
                    contexts[cwd].session_allowed = None  # replayed unlabelled: never wider
                label = origin_labels.classify(event, contexts[cwd])
            wire = origin_labels.wire(taint.apply(session, label, project=project), project)
        except Exception:
            wire = {'ceiling': 'private', 'kinds': ['unknown_tool'], 'reason': 'unknown_tool', 'projects': [],
                    'inherited': False}
        value = normalize_capture(dict(event, event_id=index), project, connection, origin='host_hook',
                                  sharing='private', origin_label=wire, remote=remote,
                                  workspace_key=bool(scope.get('workspace_key')))
        yield index, value


def _codex_values(config, scope, session, path, start, connection, state, remote):
    from agentclient.enterprise_capture import normalize_capture
    from agentclient.enterprise_desktop import convert, origin_labeller
    labeller = origin_labeller(state, config, scope, start, session)
    project = scope['project']
    turn = ''
    for number, record in _lines(path):
        index = 'line:' + str(number)
        if record is None:
            yield index, normalize_capture({'hook_event_name': 'gap', 'session_id': session, 'turn_id': turn,
                                            'event_id': index}, project, connection)
            continue
        payload = record.get('payload')
        if isinstance(payload, dict) and isinstance(payload.get('turn_id'), str) and payload['turn_id']:
            turn = payload['turn_id']
        try:
            value = convert(record, session, project, connection, turn=turn, order=number, sharing='private',
                            origin=labeller)
        except (ValueError, TypeError, KeyError):
            value = normalize_capture({'hook_event_name': 'gap', 'session_id': session, 'turn_id': turn,
                                       'event_id': index}, project, connection)
        if value is not None:
            yield index, value


def _values(config, host, scope, session, path, start, connection, state, remote):
    """Every event of the session, with its stable source id; owner-private, labelled, redacted."""
    from agentclient.general_contract import validate_event
    replay = _claude_values if host == 'claude-code' else _codex_values
    for count, (index, value) in enumerate(replay(config, scope, session, path, start, connection, state, remote)):
        if count >= MAX_EVENTS:
            raise PushRefused('session_too_large')
        value['external_id'] = source_id(host, session, index)
        value['revision'] = '1'
        value['event']['expected_events'] = []
        # Owner-private, whatever the scope file or workspace asks: never a sharing request.
        value.pop('sharing', None)
        yield index, validate_event(value)


# ---- Receipts -------------------------------------------------------------------------

def _receipt_key(host, session):
    return hashlib.sha256((host + ':' + session).encode()).hexdigest()[:32]


def _receipts(home):
    try:
        value = json.loads((Path(home) / RECEIPTS).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_receipts(home, value):
    path = Path(home) / RECEIPTS
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.pushed-', suffix='.tmp')
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


# ---- The command ----------------------------------------------------------------------

def _role(config, workspace):
    """The person's role in the workspace, from the service's descriptor."""
    from agentclient.transport import enterprise_client
    descriptor = enterprise_client(config, timeout=5).request(
        '/enterprise/v3/workspaces/descriptor', {'workspace': workspace, 'narrowed': [], 'private': False})
    return (descriptor or {}).get('role')


def push(config, home, target, *, apply=False, claude=None, codex=None, max_seconds=60):
    """Dry run (counts only) or `apply` for one session; raises PushRefused when it may not be pushed."""
    from agentclient.capture_outbox import Outbox
    from agentclient.transport import capture_connection, enterprise_client
    host, session, path, start = locate(target, claude=claude, codex=codex)
    result = {'dry_run': not apply, 'host': host, 'session': session, 'sharing': 'private'}
    from agentclient.session_label import was_read_only
    if was_read_only(home, session):
        raise PushRefused('session_ran_read_only')
    scope = _scope(config, session, start)
    if scope is None:
        raise PushRefused('session_not_started_in_a_scoped_folder_with_capture_on')
    if config.get('paused'):
        raise PushRefused('capture_paused')
    result['workspace'] = scope['project']
    try:
        role = _role(config, scope['project'])
    except Exception:
        if apply:
            raise PushRefused('workspace_role_unavailable') from None
        role = None
    if role is not None and role not in ('writer', 'admin'):
        raise PushRefused('writer_role_required')
    result['role'] = role or 'unchecked'
    remote = None
    if scope.get('root'):
        try:
            from agentclient.scope_file import discover_remote
            remote = discover_remote(scope['root'])
        except Exception:
            remote = None
    connection = capture_connection(config, scope['project'])
    receipts = _receipts(home)
    key = _receipt_key(host, session)
    queued = set((receipts.get(key) or {}).get('queued', []))
    counts = {'events': 0, 'already_pushed': 0, 'tainted_private': 0, 'carried': 0, 'gaps': 0, 'by_kind': {}}
    carried, todo = set(), []
    with tempfile.TemporaryDirectory(prefix='vaelius-push-') as state:
        for index, value in _values(config, host, scope, session, path, start, connection, state, remote):
            counts['events'] += 1
            kind = value['event']['kind']
            counts['by_kind'][kind] = counts['by_kind'].get(kind, 0) + 1
            label = value.get('origin_label') or {}
            counts['tainted_private'] += label.get('ceiling') == 'private'
            counts['carried'] += bool(label.get('projects'))
            carried |= set(label.get('projects') or ())
            counts['gaps'] += value['disposition'] in ('incomplete', 'unsupported')
            if value['external_id'] in queued:
                counts['already_pushed'] += 1
            else:
                todo.append(value)
    result.update(counts=counts, carried_workspaces=sorted(carried))
    if not apply:
        return result
    outbox = Outbox(home)
    sent = {'queued': 0, 'acknowledged': 0}
    started = time.monotonic()
    try:
        client = enterprise_client(config, timeout=5)
        for offset in range(0, len(todo), BATCH):
            for value in todo[offset:offset + BATCH]:
                outbox.put(value)
                queued.add(value['external_id'])
                sent['queued'] += 1
            receipts[key] = {'host': host, 'queued': sorted(queued), 'updated': time.time()}
            _save_receipts(home, receipts)
            drained = outbox.drain(client, max_events=BATCH, max_seconds=10)
            sent['acknowledged'] += drained['acknowledged_events']
            if drained['blocked'] or time.monotonic() - started > max_seconds:
                break
        result['outbox'] = outbox.status()
    except ValueError as error:
        if str(error) != 'capture_outbox_full':
            raise
        result['stopped'] = 'outbox_full_run_again_later'
    finally:
        outbox.close()
        if sent['queued']:
            receipts[key] = {'host': host, 'queued': sorted(queued), 'updated': time.time()}
            _save_receipts(home, receipts)
    result['sent'] = dict(sent, remaining=len(todo) - sent['queued'])
    return result
