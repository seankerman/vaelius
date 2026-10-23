"""Repository scope files: a checked-in request for capture, recall and sharing.

A scope file (default name `.vaelius`, TOML) at a repository root declares::

    project = "example-repo"   # service project name
    capture = "on"             # on | read-only | off
    sharing = "private"        # private | team | organization  (redacted passages)
    originals = "private"      # private | team | organization  (unredacted originals)

The file is a REQUEST made by whoever can edit the repository. It never grants
anything: the service caps it by the person's own sharing setting, the operator's
project and tenant ceilings and the connection visibility, and stores the
effective level per captured source. Originals are never shared more widely than
passages, so `originals` above `sharing` is lowered to it by the service.

Discovery walks up from the working directory and stops at the Git root (a
directory holding `.git`) or the filesystem root. A linked worktree whose
checkout lacks the file falls back to the same relative place in the main
worktree, so every worktree of a repository resolves to the same request.

Operators may add legacy names through the profile's `scope_files` option (for
example `[".vaelius", ".mindshare"]`). A legacy file that is not TOML is read as
plain `key: value` / `key = value` lines; only `project`, `capture`, `sharing`,
`originals` and the legacy `mode` (`default`, `read-only`, `disabled`) are mapped. Every
other key (tags, team, search, ...) is ignored. Anything unreadable or invalid
fails closed: capture and recall are both off for that directory.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib

DEFAULT_NAMES = ('.vaelius',)
PRIMARY = '.vaelius'
CAPTURE_MODES = ('on', 'read-only', 'off')
SHARING = ('private', 'team', 'organization')
LEGACY_MODES = {'default': 'on', 'read-only': 'read-only', 'readonly': 'read-only',
                'disabled': 'off', 'off': 'off', 'on': 'on'}
PROJECT = re.compile(r'[A-Za-z0-9_-]{1,120}')
MAX_BYTES = 64 * 1024
MAX_DEPTH = 64


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
    """Parsed request {project, capture, sharing, originals, path}; raises ScopeFileError when invalid."""
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
    if capture is None and 'mode' in values:
        capture = LEGACY_MODES.get(str(values['mode']).strip().lower(), 'invalid')
    capture = 'on' if capture is None else capture
    if capture not in CAPTURE_MODES:
        raise ScopeFileError('scope_file_invalid_capture')
    sharing = values.get('sharing', 'private')
    if sharing not in SHARING:
        raise ScopeFileError('scope_file_invalid_sharing')
    originals = values.get('originals', 'private')
    if originals not in SHARING:
        raise ScopeFileError('scope_file_invalid_originals')
    project = values.get('project')
    if project is not None and (not isinstance(project, str) or not PROJECT.fullmatch(project)):
        raise ScopeFileError('scope_file_invalid_project')
    return {'project': project, 'capture': capture, 'sharing': sharing, 'originals': originals, 'path': str(path)}


def _walk(start, names, stop=None):
    """(file, root) for the nearest scope file from start up to the Git root."""
    current = start
    for _ in range(MAX_DEPTH):
        for name in names:
            candidate = current / name
            if _regular_file(candidate):
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


def discover(cwd, names=DEFAULT_NAMES):
    """The scope request governing cwd, or None when no scope file applies.

    Returns {project, capture, sharing, originals, path, root}; an invalid file yields
    capture 'off' with an `error` so the caller fails closed rather than open.
    """
    try:
        start = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        return None
    if not start.is_dir():
        start = start.parent
    found, top = _walk(start, names)
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
            found, _ = _walk(target, names, stop=main)
    if found is None:
        return None
    try:
        request = parse(found)
    except ScopeFileError as error:
        return {'project': None, 'capture': 'off', 'sharing': 'private', 'originals': 'private', 'path': str(found),
                'root': str(found.parent), 'error': str(error)}
    request['root'] = str(found.parent)
    return request


ENROLLMENTS = 'scope-enrollments.json'


def enrollments(home):
    try:
        import json
        value = json.loads((Path(home) / ENROLLMENTS).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


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
    if not root or root in enrollments(home):
        return False
    with (home / 'scope-enrollments.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = enrollments(home)
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
