"""Repository scope files: a checked-in request for capture, recall and sharing.

A scope file (default name `.vaelius`, TOML) at a repository root declares::

    workspace = "example-repo"   # service workspace name
    capture = "on"               # on | read-only | off

The keys are `workspace` and `capture`. A `visibility` key is ignored with a
one-time warning: who can read a workspace is set on the server (`vaelius-client
workspace create [--private]`, `workspace set-visibility`), never in a checked-in
file. A `read` key (an earlier draft's recall list) is ignored with a one-time
warning too: recall covers what the session label allows, the current workspace
first.

The current access model builds on **workspaces** rather than flat projects
(see `docs/ACCESS_MODEL.md`). The new `workspace` key is the explicit opt-in to
the workspace audience: when it is set, the per-item `sharing` ladder of the
earlier model is replaced by the workspace's access list, and the only
per-event opt-out the client sends is `sharing = "private"` (owner only).

Legacy keys stay readable so earlier repositories keep working. In priority:

* `project`: the workspace with that id. If both `workspace` and `project` are
  present and they name different ids, the file is invalid and fails closed
  (capture and recall off) exactly like any other invalid file.
* `sharing`: a REQUEST and therefore an upper bound. In a file without
  `workspace` keep today's meaning on the wire (`private` -> omitted,
  `team` -> `team`, `organization` -> `organization`), so legacy capture
  continues unchanged. In a file WITH `workspace`, only `sharing = "private"`
  is honored (sent as omission); any other value is ignored with a one-time
  warning, and the client sends `sharing = "organization"` which the service
  reads as "inherit the workspace audience".
* `originals`: ignored with a one-time warning. Originals are never sent any
  more; sharing them is an explicit per-item grant.
* legacy `mode` names (`default`, `read-only`, `disabled`): as today.

`capture` may instead be a table that also lists origin exceptions
(`agentclient.origin`): content read from these places is not restricted to
private even though it comes from outside the repository::

    [capture]
    mode = "on"                               # on | read-only | off (default on)
    allowed_paths = ["../shared-fixtures"]    # relative to this file, absolute or ~/
    allowed_mcp_servers = ["docs"]            # MCP server names as the host registers them

The file is a REQUEST made by whoever can edit the repository. It never grants
anything: the service decides who can read captures by the workspace's access
list and per-item owner opt-outs.

Discovery walks up from the working directory and stops at the Git root (a
directory holding `.git`) or the filesystem root. A linked worktree whose
checkout lacks the file falls back to the same relative place in the main
worktree, so every worktree of a repository resolves to the same request.

A scope file at an ignored location (`ignored_scope_root`) is skipped
entirely: the home folder, any folder above it (including `/`), a top-level
personal folder such as `~/Documents` or a cloud-drive root, or anywhere in a
mail or chat store. It neither applies nor stops the walk, so a stray
`~/.vaelius` never turns capture on for every folder under home. The origin
labels (`agentclient.origin`) apply the same predicate.

Operators may add legacy names through the profile's `scope_files` option (for
example `[".vaelius", ".mindshare"]`). A legacy file that is not TOML is read
as plain `key: value` / `key = value` lines; only `workspace`, `project`,
`capture`, `sharing`, `originals` and the legacy `mode` are mapped. Every other
key (tags, team, search, ...) is ignored. Anything unreadable or invalid fails
closed: capture and recall are both off for that directory.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import tomllib

DEFAULT_NAMES = ('.vaelius',)
PRIMARY = '.vaelius'
CAPTURE_MODES = ('on', 'read-only', 'off')
SHARING = ('private', 'team', 'organization')
LEGACY_MODES = {'default': 'on', 'read-only': 'read-only', 'readonly': 'read-only',
                'disabled': 'off', 'off': 'off', 'on': 'on'}
PROJECT = re.compile(r'[A-Za-z0-9_-]{1,120}')
WORKSPACE = PROJECT  # same shape as a project id
MAX_BYTES = 64 * 1024
MAX_EXCEPTIONS = 64
MCP_SERVER = re.compile(r'[A-Za-z0-9_.-]{1,128}')
MAX_DEPTH = 64

# One-time warnings per scope file for keys the workspace model drops.
WARN_SHARING_IGNORED = 'scope_file_sharing_ignored_under_workspace'
WARN_ORIGINALS_IGNORED = 'scope_file_originals_ignored'
# `visibility` is set on the server only (`vaelius-client workspace set-visibility`).
WARN_VISIBILITY_IGNORED = 'scope_file_visibility_ignored'
# `read` (a recall list) is not a scope-file key; recall follows the session label.
WARN_READ_IGNORED = 'scope_file_read_ignored'
# One-time warning for a scope file skipped because of where it is.
WARN_IGNORED_LOCATION = 'scope_file_ignored_location'

# Relative to the home folder. A scope file directly in one of these would scope a
# whole personal area, so it is ignored.
UNSCOPABLE = ('Documents', 'Desktop', 'Downloads', 'Pictures', 'Movies', 'Music', 'iCloud Drive',
              'Library/Mobile Documents', 'Library/Mobile Documents/com~apple~CloudDocs', 'Library/CloudStorage',
              'Dropbox', 'OneDrive', 'Google Drive')
# Mail and chat stores: a scope file anywhere inside is ignored.
NEVER_SCOPED = ('Library/Mail', 'Library/Messages', 'Library/Containers/com.apple.mail', 'Library/Group Containers')


class ScopeFileError(ValueError):
    pass


def scope_names(config):
    """Validated scope file names from the profile (`scope_files`), in priority order."""
    names = config.get('scope_files', list(DEFAULT_NAMES)) if isinstance(config, dict) else list(DEFAULT_NAMES)
    if (not isinstance(names, list) or not 1 <= len(names) <= 8
            or any(not isinstance(n, str) or not re.fullmatch(r'\.?[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', n)
                   or n in ('.', '..', '.git') for n in names)):
        raise ScopeFileError('invalid_scope_files_option')
    return tuple(dict.fromkeys(names))


def _regular_file(path):
    """A regular file only: a directory of the same name (for example a data dir) is not a scope file."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def user_home():
    """The person's home folder, symlinks resolved (follows `$HOME`)."""
    return Path(os.path.realpath(os.path.expanduser('~')))


def ignored_scope_root(directory, home=None):
    """True when a scope file directly in `directory` must be ignored.

    Ignored: the home folder, any ancestor of it (up to `/`), a top-level personal
    or cloud-drive folder (`UNSCOPABLE`), and anything inside a mail or chat store
    (`NEVER_SCOPED`). `directory` and `home` are compared as given, so pass
    resolved paths. The single rule shared by discovery and the origin labels.
    """
    directory = Path(directory)
    home = user_home() if home is None else Path(home)
    if directory == home or directory in home.parents:
        return True
    if any(directory == home / p for p in UNSCOPABLE):
        return True
    return any(directory == home / p or home / p in directory.parents for p in NEVER_SCOPED)


def _lenient(text):
    values = {}
    for line in text.splitlines():
        if not line or line[0] in ' \t#-' or line.startswith('---'):
            continue  # nested YAML, comments, lists and document markers are not mapped
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*', line)
        if not match:
            continue
        value = re.sub(r'\s+#.*$', '', match.group(2)).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def parse(path):
    """Parsed request; raises ScopeFileError when invalid.

    Returned fields (so existing consumers keep working):

    * ``project``: the workspace id, from `workspace` or legacy `project`.
    * ``workspace``: alias of ``project``.
    * ``workspace_key``: True when the file carries the explicit `workspace` key.
    * ``capture``, ``sharing``, ``originals``, ``path``,
      ``allowed_paths``, ``allowed_mcp_servers``: as before.
    * ``warnings``: one-time warnings for keys the workspace model drops.
    """
    path = Path(path)
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ScopeFileError('scope_file_unreadable') from None
    if len(raw) > MAX_BYTES:
        raise ScopeFileError('scope_file_too_large')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ScopeFileError('scope_file_not_utf8') from None
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        if path.name == PRIMARY:
            raise ScopeFileError('scope_file_invalid_toml') from None
        values = _lenient(text)
    capture = values.get('capture')
    allowed_paths, allowed_mcp = [], []
    if isinstance(capture, dict):
        if set(capture) - {'mode', 'allowed_paths', 'allowed_mcp_servers'}:
            raise ScopeFileError('scope_file_invalid_capture')
        allowed_paths = capture.get('allowed_paths', [])
        allowed_mcp = capture.get('allowed_mcp_servers', [])
        if (not isinstance(allowed_paths, list) or len(allowed_paths) > MAX_EXCEPTIONS
                or any(not isinstance(x, str) or not 0 < len(x) <= 1024 or '\x00' in x for x in allowed_paths)):
            raise ScopeFileError('scope_file_invalid_allowed_paths')
        if (not isinstance(allowed_mcp, list) or len(allowed_mcp) > MAX_EXCEPTIONS
                or any(not isinstance(x, str) or not MCP_SERVER.fullmatch(x) for x in allowed_mcp)):
            raise ScopeFileError('scope_file_invalid_allowed_mcp_servers')
        capture = capture.get('mode')
    if capture is None and 'mode' in values:
        capture = LEGACY_MODES.get(str(values['mode']).strip().lower(), 'invalid')
    capture = 'on' if capture is None else capture
    if capture not in CAPTURE_MODES:
        raise ScopeFileError('scope_file_invalid_capture')
    warnings = []
    sharing = values.get('sharing', 'private')
    if sharing not in SHARING:
        raise ScopeFileError('scope_file_invalid_sharing')
    originals = values.get('originals', 'private')
    if originals not in SHARING:
        raise ScopeFileError('scope_file_invalid_originals')
    if 'originals' in values:
        # The access model drops the originals ladder; the key is still accepted
        # so an existing file is not invalid, but it is never sent on the wire.
        warnings.append(WARN_ORIGINALS_IGNORED)
    originals = 'private'
    workspace = values.get('workspace')
    project = values.get('project')
    if workspace is not None and (not isinstance(workspace, str) or not WORKSPACE.fullmatch(workspace)):
        raise ScopeFileError('scope_file_invalid_workspace')
    if project is not None and (not isinstance(project, str) or not PROJECT.fullmatch(project)):
        raise ScopeFileError('scope_file_invalid_project')
    if workspace is not None and project is not None and workspace != project:
        # `workspace` and legacy `project` disagree: fail closed like any invalid file.
        raise ScopeFileError('scope_file_workspace_project_mismatch')
    workspace_key = workspace is not None
    identifier = workspace or project  # may still be None (legacy, workspace-less file)
    if 'visibility' in values:
        # Who can read a workspace is decided on the server, never by a checked-in file.
        warnings.append(WARN_VISIBILITY_IGNORED)
    if 'read' in values:
        warnings.append(WARN_READ_IGNORED)
    if workspace_key:
        # In a workspace file the sharing ladder is replaced by the workspace
        # access list. Only `sharing = "private"` is honored (sent as omission;
        # the item is owner-only); every other value, and the absent-key
        # default, mean "inherit the workspace audience" and go on the wire as
        # `sharing = "organization"` (see docs/ACCESS_MODEL.md section 5 and
        # section 11). A wider request that the file still carries is ignored
        # with a one-time warning.
        if 'sharing' in values and sharing != 'private':
            warnings.append(WARN_SHARING_IGNORED)
        sharing = 'private' if sharing == 'private' and 'sharing' in values else 'organization'
    return {'project': identifier, 'workspace': identifier, 'workspace_key': workspace_key,
            'capture': capture, 'sharing': sharing, 'originals': originals, 'path': str(path),
            'allowed_paths': allowed_paths, 'allowed_mcp_servers': allowed_mcp, 'warnings': warnings}


def _walk(start, names, stop=None, home=None, ignored=None):
    """(file, root) for the nearest scope file from start up to the Git root.

    A scope file at an ignored location is skipped (appended to `ignored` when a
    list is given); the walk carries on past it and still stops at the Git root,
    `stop` or the filesystem root."""
    home = user_home() if home is None else home
    current = start
    for _ in range(MAX_DEPTH):
        for name in names:
            candidate = current / name
            if _regular_file(candidate):
                if ignored_scope_root(current, home):
                    if ignored is not None and candidate not in ignored:
                        ignored.append(candidate)
                    break  # a lower-priority name in the same folder is ignored too
                return candidate, current
        if (current / '.git').exists() or current == stop or current.parent == current:
            return None, current
        current = current.parent
    return None, current


def _main_worktree(top):
    """Main worktree root for a linked worktree, else None. Only called when needed."""
    try:
        result = subprocess.run(['git', '-C', str(top), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                capture_output=True, text=True, timeout=.5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    common = Path(result.stdout.strip())
    if common.name != '.git':
        return None
    main = common.parent.resolve()
    return None if main == top else main


def discover(cwd, names=DEFAULT_NAMES, ignored=None):
    """The scope request governing cwd, or None when no scope file applies.

    Returns the fields documented on ``parse`` plus ``root``; an invalid file
    yields capture 'off' with an ``error`` so the caller fails closed rather
    than open. Scope files skipped by ``ignored_scope_root`` are appended to
    ``ignored`` when a list is given.
    """
    try:
        start = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        return None
    if not start.is_dir():
        start = start.parent
    home = user_home()
    found, top = _walk(start, names, home=home, ignored=ignored)
    if found is None and (top / '.git').is_file():
        main = _main_worktree(top)
        if main is not None:
            try:
                relative = start.relative_to(top)
            except ValueError:
                relative = Path()
            target = main / relative
            while not target.is_dir() and target != main:
                target = target.parent
            found, _ = _walk(target, names, stop=main, home=home, ignored=ignored)
    if found is None:
        return None
    try:
        request = parse(found)
    except ScopeFileError as error:
        return {'project': None, 'workspace': None, 'workspace_key': False, 'capture': 'off',
                'sharing': 'private', 'originals': 'private', 'path': str(found),
                'root': str(found.parent), 'warnings': [], 'error': str(error)}
    request['root'] = str(found.parent)
    return request


# Per-process cache of normalized remotes keyed by scope root. One lookup per
# session is enough and keeps us off `git` on every captured event; the entry
# ages out so an added remote becomes visible without restarting.
_REMOTE_CACHE = {}
_REMOTE_LOCK = threading.Lock()
_REMOTE_TTL = 300.0  # seconds


def _remote_lookup(root):
    try:
        result = subprocess.run(['git', '-C', str(root), 'remote', '-v'],
                                capture_output=True, text=True, timeout=1.0, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    remotes = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            # `git remote -v` lists each remote twice (fetch and push).
            remotes.setdefault(parts[0], parts[1])
    if 'origin' in remotes:
        url = remotes['origin']
    elif len(remotes) == 1:
        url = next(iter(remotes.values()))
    else:
        return None
    from agentclient.general_contract import normalize_remote
    return normalize_remote(url)


def discover_remote(root):
    """Normalized remote for a scope root: ``origin``, else the only remote, else None.

    A scoped folder that is not a Git repository, a bare worktree, a repository
    with no remotes, or one with several remotes and no `origin` all return
    None; the server accepts a remote-less capture on the person's membership
    alone (see `docs/ACCESS_MODEL.md` section 4). Cached per-process for
    ``_REMOTE_TTL`` seconds."""
    if not root:
        return None
    key = str(root)
    now = time.monotonic()
    with _REMOTE_LOCK:
        entry = _REMOTE_CACHE.get(key)
        if entry is not None and now - entry[1] < _REMOTE_TTL:
            return entry[0]
    value = _remote_lookup(key)
    with _REMOTE_LOCK:
        _REMOTE_CACHE[key] = (value, now)
        # Keep the cache bounded; a hundred scope roots per process is plenty.
        if len(_REMOTE_CACHE) > 128:
            oldest = min(_REMOTE_CACHE, key=lambda k: _REMOTE_CACHE[k][1])
            if oldest != key:
                del _REMOTE_CACHE[oldest]
    return value


ENROLLMENTS = 'scope-enrollments.json'


def _read_enrollments(home):
    try:
        import json
        value = json.loads((Path(home) / ENROLLMENTS).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def enrollments(home):
    """Recorded scope-file enrollments, minus roots at an ignored location.

    `home` is the client profile folder. A root recorded before the location
    rule existed (for example the home folder itself) is no longer reported."""
    person = user_home()
    return {root: value for root, value in _read_enrollments(home).items()
            if not ignored_scope_root(Path(root), person)}


def record_enrollment(home, scope):
    """Record, once, that a scope file enrolled its root (forward-only from now).

    Kept beside config.json, never in it: the scope file stays authoritative, so
    removing it or setting `capture = "off"` stops capture without a stale entry.
    """
    import fcntl
    import json
    import tempfile
    import time
    home = Path(home)
    root = scope.get('root')
    if not root or ignored_scope_root(Path(root)) or root in _read_enrollments(home):
        return False
    with (home / 'scope-enrollments.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = _read_enrollments(home)
        if root in value:
            return False
        value[root] = {'project': scope['project'], 'scope_file': scope.get('scope_file'),
                       'first_seen': time.time(), 'status': 'enrolled_forward_only'}
        with tempfile.NamedTemporaryFile(mode='w', dir=home, prefix='scope-', suffix='.tmp', delete=False) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        temporary.replace(home / ENROLLMENTS)
    return True


# Warnings record file; keyed by `(root, warning)`. Used so each warning is
# surfaced on the status line once per scope root rather than every session.
WARNINGS_FILE = 'scope-warnings.json'


def _warnings_path(home):
    return Path(home) / WARNINGS_FILE


def _read_warnings(home):
    import json
    try:
        value = json.loads(_warnings_path(home).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_warnings(home, value):
    import json
    import tempfile
    home = Path(home)
    with tempfile.NamedTemporaryFile(mode='w', dir=home, prefix='scope-warn-',
                                     suffix='.tmp', delete=False) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        temporary = Path(stream.name)
    os.chmod(temporary, 0o600)
    temporary.replace(_warnings_path(home))


def record_warning_once(home, root, warning):
    """Record a one-time warning for a scope root; True the first time only."""
    import fcntl
    if not root or not warning:
        return False
    home = Path(home)
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return False
    try:
        with (home / 'scope-warn.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = _read_warnings(home)
            shown = value.setdefault(root, [])
            if warning in shown:
                return False
            shown.append(warning)
            _write_warnings(home, value)
    except OSError:
        return False
    return True
