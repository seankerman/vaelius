"""Fixed session labels (docs/ACCESS_MODEL.md section 7): the client half.

A captured session takes its label when it starts: the workspace of the folder it
starts in (`scope_of(cwd)`). Its audience is the readers of that workspace, or only
the person when the service says the session is private. Sessions that are
read-only, off or outside every workspace get no label and the read rule does not
apply to them.

The label is fixed. A `cd` does not change it; compaction keeps it; `/clear` or a
new conversation starts a new one. The person can narrow it explicitly with
`vaelius-client session use <workspace|path|remote|private>` or by asking the agent
to call the Vaelius MCP tool `use_workspace`: the audience becomes the intersection,
later captures carry the narrowed workspaces, and the status line says so.

The PreToolUse guardrail hook (`agentclient.claude_guardrails.check`) enforces the
read rule from local state only, never a network call:

* the session's own workspace and the ones it is narrowed to: allow;
* a scoped folder of a workspace listed in the descriptor's
  `contains_session_audience` (in a private session: `readable`): allow, and the
  PostToolUse origin label carries that workspace;
* a scoped folder of any other workspace (narrower, unreadable, unknown, or a scope
  file that does not parse): deny with one plain line, never `ask`;
* everything else falls through to the guardrail profile and the #32 backstop
  (unscoped and unknown paths are allowed and make the session private).

The descriptor comes from the service (`POST /enterprise/v3/workspaces/descriptor`)
at SessionStart, after `refresh_after` (15 minutes) at the next prompt, and when a
search reports a newer epoch. It is cached in the client profile, never in the
checked-in scope file: `workspaces/<id>.json` (the workspace's own descriptor) and,
per session, in `session-labels.json` (0600). After `expires_at` (24 hours) without
a refresh the hook allows only the session's own and narrowed workspaces.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

STATE = 'session-labels.json'
DESCRIPTORS = 'workspaces'
STATE_DAYS = 30
NAME = re.compile(r'[A-Za-z0-9_-]{1,120}')
# Scope origins that name a real workspace (capture-all fallbacks invent project names).
LABELLED_ORIGINS = ('scope_file', 'enrolled_root', 'enrolled_worktree', 'session')
SESSION_ENV = ('CLAUDE_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'VAELIUS_SESSION_ID')
ACTIVE_SECONDS = 12 * 3600  # `use` without --session picks a session active this recently


class UseRefused(ValueError):
    """`use` cannot apply (no labelled session, an unscoped target, ...)."""


def _key(session):
    return hashlib.sha256(str(session).encode()).hexdigest()[:32]


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.label-', suffix='.tmp', delete=False) as stream:
        json.dump(value, stream, sort_keys=True)
        temporary = Path(stream.name)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _read(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class SessionLabels:
    """Labels of this profile's sessions, keyed by a hash of the host session ID."""

    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / STATE

    def all(self):
        return _read(self.path)

    def get(self, session):
        entry = self.all().get(_key(session))
        return entry if isinstance(entry, dict) else None

    def _locked(self, change):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.home / 'session-labels.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.all()
            result = change(state)
            cutoff = time.time() - STATE_DAYS * 86400
            for key in [k for k, v in state.items() if not isinstance(v, dict) or v.get('updated', 0) < cutoff]:
                state.pop(key)
            _write(self.path, state)
            return result

    def start(self, session, scope, cwd):
        """The session's label, created from its starting folder the first time only."""
        def change(state):
            key = _key(session)
            entry = state.get(key)
            if not isinstance(entry, dict):
                entry = {'workspace': scope['project'], 'root': scope.get('root'), 'cwd': cwd,
                         'scope_file': scope.get('scope_file'), 'sharing': scope.get('sharing', 'organization'),
                         'allowed_paths': list(scope.get('allowed_paths') or ()),
                         'allowed_mcp_servers': list(scope.get('allowed_mcp_servers') or ()),
                         'narrowed': [], 'private': False, 'descriptor': None, 'announce': None,
                         'short': str(session)[:8], 'started': time.time()}
                state[key] = entry
            entry['updated'] = time.time()
            return dict(entry)
        return self._locked(change)

    def update(self, session, **values):
        def change(state):
            entry = state.get(_key(session))
            if not isinstance(entry, dict):
                return None
            entry.update(values, updated=time.time())
            return dict(entry)
        return self._locked(change)

    def take_announcement(self, session):
        """The one-time status line queued by a narrowing, once."""
        def change(state):
            entry = state.get(_key(session))
            if not isinstance(entry, dict) or not entry.get('announce'):
                return None
            line, entry['announce'] = entry['announce'], None
            return line
        return self._locked(change)

    def forget(self, session):
        self._locked(lambda state: state.pop(_key(session), None))

    def find(self, cwd, names=('.vaelius',)):
        """(key, entry) of the most recently active session started in cwd's scoped folder."""
        from agentclient.scope_file import discover
        try:
            found = discover(cwd, names)
        except Exception:
            found = None
        root = found.get('root') if found else None
        here = os.path.realpath(cwd)
        best = None
        for key, entry in self.all().items():
            if not isinstance(entry, dict) or time.time() - entry.get('updated', 0) > ACTIVE_SECONDS:
                continue
            start = entry.get('root') or entry.get('cwd') or ''
            if not ((root and entry.get('root') == root) or (start and (here == start or here.startswith(start + os.sep)))):
                continue
            if best is None or entry.get('updated', 0) > best[1].get('updated', 0):
                best = (key, entry)
        return best


def descriptor_path(home, workspace):
    return Path(home) / DESCRIPTORS / (workspace + '.json')


def request_label(entry):
    """The wire session label: {workspace, narrowed, private, sharing}."""
    label = {'workspace': entry['workspace'], 'narrowed': list(entry.get('narrowed') or []),
             'private': bool(entry.get('private'))}
    if entry.get('sharing') in ('private', 'team', 'organization'):
        label['sharing'] = entry['sharing']
    return label


def refresh(home, config, session, entry=None, *, timeout=2):
    """Fetch the descriptor for this session's label and store it; returns the entry."""
    from agentclient.transport import enterprise_client
    labels = SessionLabels(home)
    entry = entry or labels.get(session)
    if not entry:
        return None
    previous = entry.get('descriptor') or {}
    descriptor = enterprise_client(config, timeout=timeout).request('/enterprise/v3/workspaces/descriptor',
                                                                    request_label(entry))
    if not entry.get('narrowed') and not entry.get('private'):
        try:
            _write(descriptor_path(home, descriptor['workspace']['id']), descriptor)
        except OSError:
            pass
    values = {'descriptor': descriptor}
    if previous.get('role') and descriptor.get('role') != previous.get('role'):
        values['announce'] = role_line(entry, descriptor)
    return labels.update(session, **values)


def role_line(entry, descriptor):
    role = descriptor.get('role') or 'not a reader'
    name = (descriptor.get('workspace') or {}).get('name') or entry['workspace']
    capture = 'capture is off' if role not in ('writer', 'admin') else 'capture is on'
    return 'vaelius: you are now ' + ('a ' + role if role in ('reader', 'writer') else 'an ' + role) + \
        ' in ' + name + ' — ' + capture


def captures(entry):
    """False when the service says the person cannot capture in the session's workspace any more."""
    role = ((entry or {}).get('descriptor') or {}).get('role')
    return role is None or role in ('writer', 'admin')


def stale(entry, now=None):
    descriptor = (entry or {}).get('descriptor')
    now = time.time() if now is None else now
    return not descriptor or now >= descriptor.get('refresh_after', 0)


def expired(entry, now=None):
    descriptor = (entry or {}).get('descriptor')
    now = time.time() if now is None else now
    return not descriptor or now >= descriptor.get('expires_at', 0)


def _names(items):
    found = set()
    for item in items or ():
        if isinstance(item, dict):
            found |= {v for v in (item.get('id'), item.get('name')) if isinstance(v, str)}
    return found


def own(entry):
    """The session's own and narrowed workspaces (names and ids)."""
    descriptor = entry.get('descriptor') or {}
    found = {entry['workspace'], *(entry.get('narrowed') or ())}
    found |= _names([descriptor.get('workspace')])
    return found


def allowed(entry, *, private=False, now=None):
    """(workspaces a read may come from, whether the descriptor is usable)."""
    found = own(entry)
    if expired(entry, now):
        return found, False
    descriptor = entry['descriptor']
    found |= _names(descriptor.get('contains_session_audience'))
    if private or entry.get('private') or (descriptor.get('session') or {}).get('private'):
        found |= _names(descriptor.get('readable'))
    return found, True


def display(entry):
    descriptor = entry.get('descriptor') or {}
    names = [(descriptor.get('workspace') or {}).get('name') or entry['workspace'], *(entry.get('narrowed') or ())]
    return ' ∩ '.join(names)


def status_line(entry, *, private=False):
    """The session label for the person, or None for an ordinary unnarrowed session."""
    if not entry:
        return None
    if entry.get('private'):
        return 'vaelius: session ' + display(entry) + ' is private (you only) — capture limited to you'
    if entry.get('narrowed'):
        return 'vaelius: session uses ' + display(entry) + ' — capture limited to people in ' + \
            ('both' if len(entry['narrowed']) == 1 else 'all of them')
    return None


def session_private(home, session):
    """The #32 backstop (or an explicit `use private`) made this session private."""
    from agentclient.origin import SessionOrigins
    try:
        entry = SessionOrigins(home).get(session)
    except Exception:
        return False
    return bool(entry) and entry.get('ceiling') == 'private'


# The read rule ---------------------------------------------------------------------

def _where(info, path, home):
    root = info.get('root') or str(path)
    try:
        return '~/' + str(Path(root).relative_to(home)) if Path(root) != Path(home) else '~'
    except ValueError:
        return root


def decide(entry, info, path, *, private=False, now=None, home=None):
    """None (allow) or the one-line deny reason for one classified path."""
    category = info.get('category')
    if category == 'claude_state' and (info.get('own') or info.get('same') or not info.get('root')):
        return None
    if category not in ('scoped', 'claude_state'):
        return None  # project, credentials and personal folders (the profile), unscoped (the backstop)
    home = home or os.path.expanduser('~')
    where = _where(info, path, home)
    label = display(entry)
    workspace = info.get('project')
    if info.get('error') or not workspace or not NAME.fullmatch(str(workspace)):
        return ('vaelius: denied — ' + where + ' has a scope file Vaelius cannot read, so its workspace is '
                'unknown and this session (' + label + ') does not read it.')
    found, usable = allowed(entry, private=private, now=now)
    if workspace in found:
        return None
    if not usable:
        return ('vaelius: denied — ' + where + ' belongs to workspace "' + workspace + '" and this session\'s '
                'access descriptor could not be refreshed, so only ' + label + ' folders are readable. Check '
                '`vaelius-client backend-check`.')
    if workspace not in _names((entry.get('descriptor') or {}).get('readable')):
        return ('vaelius: denied — ' + where + ' belongs to workspace "' + workspace + '", which you cannot read '
                'or which does not exist; this session (' + label + ') does not read it.')
    summary = narrower(entry, workspace)
    if summary and who(summary):
        name = summary.get('name') or workspace
        return ('vaelius: denied — ' + where + ': ' + name + " isn't visible to " + who(summary) + " (in this "
                "session's audience, " + label + "). The person can run 'vaelius-client session use " + name +
                "' for this session, or ask " + name + "'s admin to add them.")
    return ('vaelius: denied — ' + where + ' (workspace "' + workspace + '") is visible to fewer people than this '
            'session (' + label + '). Start a session there, or ask the person to run `vaelius-client session use '
            + workspace + '`; capture is then limited to people in both.')


# Why a narrower workspace is narrower ------------------------------------------------
#
# The descriptor's `narrower` list carries, for each workspace the person can read but
# this session cannot, who of the session's audience is missing from it: a count and
# at most three names, drawn by the service only from the session workspace's own
# access list. The hook and the recall note read it locally, never per read.

def narrower(entry, workspace):
    """The descriptor's summary for one narrower workspace (by id or name), or None."""
    for item in ((entry or {}).get('descriptor') or {}).get('narrower') or ():
        if isinstance(item, dict) and workspace in (item.get('id'), item.get('name')):
            return item
    return None


def _join(parts):
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def who(summary):
    """'carol, dave and 2 more', or '3 people' when nobody can be named; '' when unknown."""
    missing = (summary or {}).get('missing_readers') or {}
    count = missing.get('count')
    names = [str(n) for n in missing.get('names') or () if isinstance(n, str) and n][:3]
    more = missing.get('more') if type(missing.get('more')) is int else 0
    if names:
        return _join(names + ([str(more) + ' more'] if more > 0 else []))
    if type(count) is int and count > 0:
        return str(count) + (' person' if count == 1 else ' people')
    return ''


def hidden_line(entry):
    """The once-per-session status line for memories the session's recall leaves out, or None."""
    found = [item for item in ((entry or {}).get('descriptor') or {}).get('narrower') or ()
             if isinstance(item, dict) and type(item.get('hidden_items')) is int and item['hidden_items'] > 0]
    if not found:
        return None
    found.sort(key=lambda item: (-item['hidden_items'], str(item.get('name') or item.get('id'))))
    first = found[0]
    name = str(first.get('name') or first.get('id'))
    count = first['hidden_items']
    line = 'vaelius: ' + str(count) + (' memory' if count == 1 else ' memories') + ' from ' + name + ' hidden'
    if who(first):
        line += ' (not visible to ' + who(first) + ')'
    others = found[1:]
    if others:
        line += '; also ' + _join([str(item['hidden_items']) + ' from ' + str(item.get('name') or item.get('id'))
                                   for item in others[:2]] + ([str(len(others) - 2) + ' more workspaces']
                                                              if len(others) > 2 else []))
    target = name if not others else '<workspace>'
    return line + "; 'vaelius-client session use " + target + "' to include them"


class _Recorder:
    """Wraps an origin Context so the shell parser reports every path it places."""

    def __init__(self, context):
        self.context = context
        self.found = []

    def __getattr__(self, name):
        return getattr(self.context, name)

    def place(self, raw, base=None):
        from agentclient.origin import Label
        path = self.context._resolve(raw, base)
        if path is not None:
            self.found.append(path)
        return Label()


READ_TOOLS = ('Read', 'Grep', 'Glob', 'LSP', 'NotebookRead')


def targets(event, context):
    """The filesystem paths one PreToolUse call reads, resolved."""
    tool = str(event.get('tool_name', ''))
    values = event.get('tool_input') if isinstance(event.get('tool_input'), dict) else {}
    cwd = context.cwd
    if tool == 'Bash':
        from agentclient.origin import shell
        recorder = _Recorder(context)
        command = values.get('command')
        if isinstance(command, str):
            shell(command, recorder, cwd)
        return list(dict.fromkeys(recorder.found))
    if tool not in READ_TOOLS:
        return []
    raw = [v for k in ('file_path', 'notebook_path', 'filePath', 'path') for v in [values.get(k)]
           if isinstance(v, str) and v]
    if tool in ('Grep', 'Glob') and not values.get('path'):
        raw.append('.')
    pattern = values.get('pattern') if tool == 'Glob' else None
    if isinstance(pattern, str) and (pattern.startswith(('/', '~')) or '..' in pattern.split('/')):
        from agentclient.origin import _glob_base
        raw.append(_glob_base(pattern))
    found = [context._resolve(item, cwd) for item in raw]
    return list(dict.fromkeys(p for p in found if p is not None))


def hook_reason(home, event, *, now=None):
    """The label rule for one PreToolUse event: None (no opinion) or a deny reason.

    Reads only local files: the session's label and descriptor, the taint state and
    the scope files on the way up from each path."""
    session = str(event.get('session_id') or '')
    tool = str(event.get('tool_name', ''))
    if not session or (tool != 'Bash' and tool not in READ_TOOLS):
        return None
    entry = SessionLabels(home).get(session)
    if not entry:
        return None
    from agentclient.origin import Context
    config = _read(Path(home) / 'config.json')
    scope = {'project': entry['workspace'], 'root': entry.get('root'), 'scope_file': entry.get('scope_file'),
             'allowed_paths': entry.get('allowed_paths'), 'allowed_mcp_servers': entry.get('allowed_mcp_servers')}
    cwd = event.get('cwd') if isinstance(event.get('cwd'), str) else None
    context = Context(scope, cwd, config)
    context.transcript(event.get('transcript_path'))
    private = None
    for path in targets(event, context):
        info = context._scope_of(path)
        if info.get('category') not in ('scoped', 'claude_state'):
            continue
        if private is None:
            private = session_private(home, session)
        reason = decide(entry, info, path, private=private, now=now, home=str(context.home))
        if reason:
            return reason
    return None


# Explicit narrowing ----------------------------------------------------------------

def resolve_session(home, cwd, session=None, names=('.vaelius',)):
    """(session key or id, entry): an explicit ID, the host's session variable, else the
    most recently active labelled session started in this folder's scoped folder."""
    labels = SessionLabels(home)
    for candidate in [session] + [os.environ.get(name) for name in SESSION_ENV]:
        if candidate:
            entry = labels.get(candidate)
            if entry:
                return _key(candidate), entry
            if candidate == session:
                raise UseRefused('no labelled Vaelius session with that ID: ' + candidate[:8] + '...')
    found = labels.find(cwd, names)
    if not found:
        raise UseRefused('no labelled Vaelius session started in this folder. `use` needs a session that started '
                         'in a workspace folder with capture on; it is refused in an unscoped session.')
    return found


def _target(config, target, cwd):
    """A workspace name or id, a remote, or the workspace of an existing scoped folder."""
    if target == 'private':
        return target
    candidate = os.path.expanduser(target)
    if not os.path.isabs(candidate):
        candidate = os.path.join(cwd, candidate)
    if os.path.exists(candidate) and (os.sep in target or target.startswith(('.', '~')) or not NAME.fullmatch(target)):
        from agentclient.origin import Context
        info = Context(None, cwd, config).scope_of(candidate)
        if info.get('category') not in ('scoped', 'project') or info.get('error') or not info.get('project'):
            raise UseRefused('that folder belongs to no workspace (no valid scope file)')
        return info['project']
    return target


def use(home, config, target, *, cwd=None, session=None):
    """Narrow a labelled session: the server checks the person can read the target."""
    from agentclient.transport import enterprise_client
    from agentclient.scope_file import scope_names
    cwd = cwd or os.getcwd()
    key, entry = resolve_session(home, cwd, session, scope_names(config))
    resolved = _target(config, target, cwd)
    label = request_label(entry)
    if resolved == 'private':
        label['private'] = True
    else:
        label['narrowed'] = list(dict.fromkeys([*label['narrowed'], resolved]))
    descriptor = enterprise_client(config, timeout=10).request('/enterprise/v3/workspaces/descriptor', label)
    return apply_use(home, key, entry, descriptor, private=resolved == 'private', keyed=True)


def apply_use(home, session, entry, descriptor, *, private=False, keyed=False):
    """Record a server-checked narrowing for the session; capture follows it."""
    from agentclient import origin
    labels = SessionLabels(home)
    narrowed = list((descriptor.get('session') or {}).get('narrowed') or [])
    values = {'narrowed': narrowed, 'descriptor': descriptor}
    if private:
        values['private'] = True
    updated = dict(entry, **values)
    values['announce'] = status_line(updated)
    def change(state):
        key = session if keyed else _key(session)
        current = state.get(key)
        if not isinstance(current, dict):
            return None
        current.update(values, updated=time.time())
        return dict(current)
    result = labels._locked(change)
    if result is None:
        raise UseRefused('the session ended')
    try:
        # The MCP connection picks the narrower label up when it next connects.
        record_mcp_label(home, None, result, key=session if keyed else _key(session))
    except OSError:
        pass
    # Capture follows the narrowing: later events carry the narrowed workspaces, or are private.
    label = (origin.Label('private', ('project',), 'explicit_private', 'use private') if private else
             origin.Label(origin.OPEN, ('other_project',), 'other_project', ', '.join(narrowed), narrowed))
    origin.SessionOrigins(home).apply(session, label, project=entry['workspace'], key=session if keyed else None)
    return {'session': result.get('short'), 'label': display(result), 'narrowed': narrowed,
            'private': bool(result.get('private')), 'status': values['announce']}


def use_result(response):
    """The `vaelius_session_use` record in an MCP use_workspace result, or None."""
    def walk(value, depth=0):
        if depth > 6:
            return None
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in '[{':
                try:
                    return walk(json.loads(text), depth + 1)
                except ValueError:
                    return None
            return None
        if isinstance(value, list):
            for item in value[:20]:
                found = walk(item, depth + 1)
                if found:
                    return found
            return None
        if isinstance(value, dict):
            record = value.get('vaelius_session_use')
            if isinstance(record, dict):
                return record
            for item in list(value.values())[:20]:
                found = walk(item, depth + 1)
                if found:
                    return found
        return None
    return walk(response)


# The MCP connection's label ----------------------------------------------------------
#
# Claude Code runs the MCP header helper once per connection (session start and
# reconnect) and gives it no session ID, only the server's name and URL. The hooks
# therefore record the label of the session most recently active in each scoped
# folder (`mcp-labels/<folder hash>.json`, 0600), and the helper sends it. The record
# may only make the folder's own label narrower or equal (`compose`): same workspace,
# same sharing request, extra narrowed workspaces, or private. Narrower means fewer
# people, so recall may return more of what the person can already read; it is never
# a wider audience than a session starting in that folder gets.

MCP_LABELS = 'mcp-labels'
MCP_LABEL_SECONDS = ACTIVE_SECONDS


def mcp_label_path(home, root):
    digest = hashlib.sha256(os.path.realpath(str(root)).encode()).hexdigest()[:32]
    return Path(home) / MCP_LABELS / (digest + '.json')


def record_mcp_label(home, session, entry, *, private=False, key=None):
    """Record this session's current label for its starting folder (last writer wins).

    `key` is the stored session key when the caller has no host session ID."""
    root = (entry or {}).get('root') or (entry or {}).get('cwd')
    if not root or not NAME.fullmatch(str(entry.get('workspace'))):
        return None
    value = {'root': os.path.realpath(str(root)), 'session': key or _key(session), 'workspace': entry['workspace'],
             'narrowed': [n for n in entry.get('narrowed') or () if isinstance(n, str) and NAME.fullmatch(n)],
             'private': bool(entry.get('private') or private), 'updated': time.time()}
    path = mcp_label_path(home, root)
    _write(path, value)
    return value


def clear_mcp_label(home, session, entry):
    """Drop the folder's record when this session wrote it (the session ended)."""
    root = (entry or {}).get('root') or (entry or {}).get('cwd')
    if not root:
        return
    path = mcp_label_path(home, root)
    if _read(path).get('session') == _key(session):
        try:
            path.unlink()
        except OSError:
            pass


def compose(folder, recorded, *, now=None):
    """The header label: the folder's label, narrowed (never widened) by the record.

    `folder` is the label a session starting in the folder gets ({workspace,
    sharing?}); `recorded` is the folder's record, or None. A record for another
    workspace, a stale one or a malformed one is ignored."""
    if not folder:
        return folder
    now = time.time() if now is None else now
    if (not isinstance(recorded, dict) or recorded.get('workspace') != folder.get('workspace')
            or not isinstance(recorded.get('updated'), (int, float))
            or now - recorded['updated'] > MCP_LABEL_SECONDS):
        return folder
    label = dict(folder)
    extra = [n for n in recorded.get('narrowed') or () if isinstance(n, str) and NAME.fullmatch(n)]
    if extra:
        label['narrowed'] = list(dict.fromkeys([*(folder.get('narrowed') or []), *extra]))[:16]
    if recorded.get('private') is True or folder.get('private'):
        label['private'] = True
    return label


def mcp_label(home, root, folder):
    """`compose` over the folder's record, read from the profile."""
    if not folder or not root:
        return folder
    return compose(folder, _read(mcp_label_path(home, root)))
