"""Origin labels and session taint for captured agent events.

A repository scope file decides capture for a whole session, but one tool call
can read far outside the repository: a document in the person's home folder, a
mail or chat MCP server, a credentials file. This module looks at what each
captured tool event touched and derives an **origin label**: a ceiling (the
widest sharing that content may get) plus the set of *other* projects whose
content it carries.

Paths (symlinks and `..` resolved first), first match wins:

* credential files (`.env`, `*.pem`, `~/.ssh`, `~/.aws`, ...), anywhere, even
  inside the project: always `private`;
* **scoped folders**: any folder holding a scope file, with its whole subtree, Git
  repository or not. The nearest scope file up the tree governs, so a nested one
  overrides its parent. The same project (the current root, another checkout or
  worktree, a sibling folder with the same `project`): no restriction. Another
  project: tagged with that project; the service then shares the event no wider
  than both projects allow, and for `team` only with people who are members of
  both. The other scope file's own sharing request also caps the ceiling. A scope
  file that is off or invalid: `private`. A scope file at the home folder, at any
  folder above it (including `/`), or directly at a top-level personal folder
  (`~/Documents`, `~/Desktop`, `~/Downloads`, iCloud Drive, ...) is ignored: it
  neither counts nor overrides. One deeper inside those folders counts normally;
* Claude Code's own state: `~/.claude/projects/<folder>/` (saved tool output,
  task output, auto-memory, transcripts) belongs to the project whose folder the
  name encodes (Claude Code replaces every character other than `A-Z`, `a-z`
  and `0-9` in the working directory with `-`): the current session's folder is
  unrestricted, a scoped folder follows that folder's project, anything else is
  `private`. User configuration already loaded into sessions (`~/.claude/CLAUDE.md`,
  `reference/`, `plans/`, `settings*.json`, `plugins/`): no restriction;
* personal folders (Documents, Desktop, Downloads, Pictures, Movies, Music,
  iCloud and cloud-drive folders, Mail, Messages) outside every scoped folder:
  always `private`, and no allowlist can open them;
* temporary folders, tool caches and package folders (`~/.cache`, `~/.npm`,
  `site-packages`, `node_modules`, ...), read-only system locations (`/usr`,
  `/opt/homebrew`, `/etc`, ...) and the scope file's `capture.allowed_paths`: no
  restriction;
* a Git repository without any governing scope file: `private`;
* anything else that exists outside every scoped folder (the rest of the home
  folder, other volumes): `private`. A path that does not exist exposes no content.

Shell commands are classified only by the paths they name, after expanding `~`,
`$HOME` and `$TMPDIR`, following `cd`, and looking inside substitutions and
quoted inline code. A command that names no outside path does not restrict,
whatever it runs (`aws`, `gh`, `git`, `curl`, `kubectl`, test runners): its
output comes from systems with their own permissions, which is a stated limit.
Inline code that asks for the home folder indirectly (`expanduser`,
`Path.home()`, ...) fails closed.

Public web fetches and searches do not restrict (origin `web`); a fetch from a
non-public address is `private`. MCP servers other than Vaelius itself are
`private` unless the scope file lists them in `capture.allowed_mcp_servers`.
Recalled memory carries the service's label for each item and follows the same
project-set rule; a shared item of the current project restricts only when its
sharing is narrower than the scope file's request. Unknown tools fail closed to `private`.

Labels are sticky per session (`SessionOrigins`): once an event is restricted,
it and every later event of that session carry at least that restriction,
because the content is now in the agent's context. Only a new session ID (`/clear`
or a new conversation) starts clean; compaction does not, because the summary
that replaces the context is written from the restricted content.

Deterministic, local and model-free. Only the ceiling, kinds, reason category
and project names are sent; matched paths, commands and server names stay here.
"""
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import time
from urllib.parse import urlsplit

# Where a scope file is ignored (UNSCOPABLE, NEVER_SCOPED): one rule, shared with capture discovery.
from agentclient.scope_file import NEVER_SCOPED, UNSCOPABLE, ignored_scope_root  # noqa: F401
from agentclient.general_contract import (ORIGIN_KINDS as KINDS, ORIGIN_LEVELS as LEVELS,
                                          ORIGIN_MAX_PROJECTS as MAX_PROJECTS, ORIGIN_REASONS as REASONS)

OPEN = LEVELS[-1]
RANK = {level: rank for rank, level in enumerate(LEVELS)}
STATE = 'origin-sessions.json'
STATE_DAYS = 30
MAX_CALLS = 512
PROJECT = re.compile(r'[A-Za-z0-9_-]{1,120}')

# Under the home folder: always private, whatever an allowlist says.
PERSONAL = ('Documents', 'Desktop', 'Downloads', 'Pictures', 'Movies', 'Music', 'Library/Mobile Documents',
            'Library/CloudStorage', 'Library/Mail', 'Library/Messages', 'Library/Containers/com.apple.mail',
            'Library/Group Containers', 'iCloud Drive', 'Dropbox', 'OneDrive', 'Google Drive')
CREDENTIALS = ('.ssh', '.aws', '.gnupg', '.netrc', '.git-credentials', '.kube', '.docker/config.json', '.config/gh',
               '.config/gcloud', '.azure', '.npmrc', '.pypirc', '.vault-token', '.password-store', 'Library/Keychains',
               '.terraform.d/credentials.tfrc.json', '.config/op', '.pgpass', '.my.cnf')
CREDENTIAL_NAMES = ('id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*', '*.pem', '*.key', '*.p12', '*.pfx',
                    '.env', '.env.*', 'credentials', 'credentials.json', '.netrc', '.pgpass')
# Checked-in templates that hold placeholders, not secrets.
CREDENTIAL_TEMPLATES = ('.env.example', '.env.sample', '.env.template', '.env.dist', '.env.defaults')
# Claude Code user configuration already loaded into every session (under ~/.claude).
CLAUDE_CONFIG = ('CLAUDE.md', 'reference', 'plans', 'plugins')
CLAUDE_SETTINGS = 'settings*.json'
SLUG_LISTING = 200000  # entries read from one folder to decode a name
SLUG_MAX = 200  # Claude Code shortens longer project folder names and appends a hash
# Under the home folder: caches, toolchains and plugin folders an agent normally touches.
TOOLING = ('.cache', '.npm', '.cargo', '.rustup', '.pyenv', '.rbenv', '.nvm', '.gradle', '.m2', '.bun', '.deno',
           '.yarn', '.pnpm-store', '.volta', '.asdf', '.sdkman', '.conda', 'miniconda3', 'anaconda3', 'go/pkg',
           '.local/share/uv', '.local/share/pnpm', '.local/lib', '.local/pipx', '.local/bin', '.claude/plugins',
           'Library/Caches', '.vscode/extensions', '.cursor/extensions', '.terraform.d/plugin-cache')
PACKAGE_DIRS = ('site-packages', 'dist-packages', 'node_modules')
SYSTEM = ('/usr', '/opt/homebrew', '/Library/Developer', '/etc', '/bin', '/sbin', '/dev', '/System/Library',
          '/Library/Frameworks', '/Applications/Xcode.app', '/nix/store', '/opt/local', '/var/tmp', '/tmp',
          '/private/tmp', '/var/folders', '/private/var/folders', '/private/etc')


def lower(*levels):
    """The most restrictive of the given sharing levels."""
    return min(levels, key=RANK.__getitem__)


class Label(dict):
    """{'ceiling', 'kinds', 'reason', 'hint', 'projects'}.

    `projects` are other projects whose content the event carries; `hint` is a
    coarse location for the local status line and is never sent."""

    def __init__(self, ceiling=OPEN, kinds=(), reason=None, hint=None, projects=()):
        super().__init__(ceiling=ceiling, kinds=sorted(set(kinds)), reason=reason, hint=hint,
                         projects=sorted(set(projects)))


NEUTRAL = Label()
CURRENT = object()  # the session's own root, enrolled without a scope file


def _scope(category, root=None, project=None, sharing=None, capture=None, error=None, **extra):
    return dict(category=category, root=root, project=project, sharing=sharing, capture=capture, error=error, **extra)


def _restrict(kind, reason, hint=None):
    return Label('private', (kind,), reason, hint)


def combine(labels):
    """One label for an event that touched several things: the most restrictive wins."""
    labels = [label for label in labels if label]
    if not labels:
        return Label(OPEN, ('project',))
    ceiling = lower(*(label['ceiling'] for label in labels))
    first = (next((x for x in labels if x['ceiling'] == ceiling and x['reason']), None)
             or next((x for x in labels if x['reason']), None))
    kinds = set().union(*(label['kinds'] for label in labels))
    projects = set().union(*(label['projects'] for label in labels))
    return Label(ceiling, kinds, first['reason'] if first else None, first['hint'] if first else None, projects)


# Context ----------------------------------------------------------------------

def _real(path):
    return Path(os.path.realpath(path))


def _within(path, root):
    return path == root or root in path.parents


def _home():
    return _real(os.path.expanduser('~'))


def _git_top(start):
    current = start
    for _ in range(64):
        if (current / '.git').exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def slug(path):
    """Claude Code's folder name under `~/.claude/projects` for a working directory."""
    return re.sub(r'[^A-Za-z0-9]', '-', str(path))


def _temp_roots():
    roots = {_real(tempfile.gettempdir())}
    if os.environ.get('TMPDIR'):
        roots.add(_real(os.environ['TMPDIR']))
    return roots


class Context:
    """Where the current project lives and what its scope file allows."""

    def __init__(self, scope, cwd, config=None):
        from agentclient.scope_file import scope_names, ScopeFileError
        self.config = config if isinstance(config, dict) else {}
        try:
            self.names = scope_names(self.config)
        except ScopeFileError:
            self.names = ('.vaelius',)
        self.project = (scope or {}).get('project')
        self.scope_file = (scope or {}).get('scope_file')
        try:
            self.cwd = _real(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None
        except (OSError, ValueError):
            self.cwd = None
        self.home = _home()
        # Without a project nothing is "this project": every path is outside.
        root = (scope or {}).get('root')
        if not root and scope and self.project and self.cwd is not None:
            top = _git_top(self.cwd)
            root = str(top) if top else None
        self.root = _real(root) if root else None
        self.sharing = (scope or {}).get('sharing')
        if self.root is not None and self._unscopable(self.root):
            self.root = None  # a scope file at home or a personal folder governs nothing here
        self.claude = self.home / '.claude'
        # This session's own Claude Code folder: named after where it started, inside the root.
        self.own_slugs = {slug(p) for p in (self.cwd, self.root)
                          if p is not None and self.root is not None and _within(p, self.root)}
        self.allowed_paths = [p for p in (self._allowed(entry) for entry in (scope or {}).get('allowed_paths') or ())
                              if p is not None]
        self.allowed_mcp = {str(s).replace('-', '_').lower() for s in (scope or {}).get('allowed_mcp_servers') or ()}
        self.temp = sorted(_temp_roots()) + [_real(p) for p in ('/tmp', '/private/tmp', '/var/tmp', '/var/folders')]
        self.system = [_real(p) for p in SYSTEM]
        self.personal = [self.home / p for p in PERSONAL]
        self.credentials = [self.home / p for p in CREDENTIALS]
        self.tooling = [self.home / p for p in TOOLING]
        # Workspaces the session's fixed label lets it read (agentclient.session_label):
        # the guardrail hook already denied every other scoped folder, so a read of one
        # of these carries its workspace without narrowing the session. None: no label.
        self.session_allowed = None
        self._places = {}
        self._dirs = {}
        self._requests = {}
        self._slugs = {}
        self._listings = {}

    def transcript(self, value):
        """The session's own transcript folder (`~/.claude/projects/<folder>/`), from a hook event."""
        if not isinstance(value, str) or not os.path.isabs(value):
            return
        try:
            folder = _real(value).parent
        except (OSError, ValueError):
            return
        if folder.parent == self.claude / 'projects' and folder.name not in self.own_slugs:
            self.own_slugs.add(folder.name)
            self._places.clear()

    def _unscopable(self, directory):
        """A scope file directly in `directory` is ignored: home, above home, a top-level personal folder."""
        return ignored_scope_root(directory, self.home)

    def _request(self, found):
        if found not in self._requests:
            from agentclient.scope_file import parse, ScopeFileError
            try:
                request = parse(found)
            except ScopeFileError as error:
                request = {'project': None, 'capture': 'off', 'sharing': 'private', 'path': str(found), 'error': str(error)}
            self._requests[found] = dict(request, root=str(found.parent))
        return self._requests[found]

    def _scoped(self, path):
        """The scope governing `path`: CURRENT (this session's root), a scope request, or None.

        Walks up from the nearest existing folder; unlike capture discovery it does
        not stop at a Git root, so a scope file covers its whole subtree. A linked
        worktree without its own scope file falls back to the main worktree's."""
        directory = path
        while not directory.is_dir() and directory.parent != directory:
            directory = directory.parent
        return self._scoped_dir(directory)

    def _scoped_dir(self, directory):
        if directory in self._dirs:
            return self._dirs[directory]
        from agentclient.scope_file import _regular_file, discover
        result = None
        if not self._unscopable(directory):
            found = next((directory / n for n in self.names if _regular_file(directory / n)), None)
            if found is not None:
                result = self._request(found)
        if result is None and directory == self.root:
            result = CURRENT
        if result is None and (directory / '.git').is_file():
            try:
                linked = discover(directory, self.names)
            except Exception:
                linked = None
            if linked and linked.get('path') and not self._unscopable(Path(linked['path']).parent):
                result = linked
        if result is None and directory.parent != directory:
            result = self._scoped_dir(directory.parent)
        self._dirs[directory] = result
        return result

    def _unslug(self, name):
        """The existing folder a `~/.claude/projects/<name>` encodes, or None."""
        if name in self._slugs:
            return self._slugs[name]
        budget = [4000]

        def candidates(directory, body):
            """Folder names whose encoding starts `body` at a `-` boundary, longest first."""
            index = self._listing(directory)
            cuts = [i for i, c in enumerate(body) if c == '-'] + [len(body)]
            for cut in reversed(cuts):
                for entry in index.get(body[:cut], ()):
                    if os.path.isdir(os.path.join(directory, entry)):
                        yield entry

        def walk(directory, rest, depth):
            if not rest:
                return directory
            if depth > 48 or not rest.startswith('-'):
                return None
            body = rest[1:]
            for entry in candidates(directory, body):
                budget[0] -= 1
                if budget[0] < 0:
                    return None
                found = walk(os.path.join(directory, entry), body[len(slug(entry)):], depth + 1)
                if found:
                    return found
            return None

        found = walk('/', name, 0) if len(name) <= SLUG_MAX else None
        self._slugs[name] = _real(found) if found else None
        return self._slugs[name]

    def _listing(self, directory):
        """{encoded name: [names]} for one folder, cached; very large folders are cut off."""
        if directory not in self._listings:
            index = {}
            try:
                with os.scandir(directory) as entries:
                    for _, entry in zip(range(SLUG_LISTING), entries):
                        index.setdefault(slug(entry.name), []).append(entry.name)
            except OSError:
                pass
            self._listings[directory] = index
        return self._listings[directory]

    def _allowed(self, entry):
        """A scope-file path exception, or None when it is too broad to honour.

        Anyone who can edit a repository can edit its scope file, so an entry may
        not open the filesystem root, the home folder or anything above it, or a
        hidden folder or `Library` directly under home. Personal folders and
        credentials stay private even below an allowed entry."""
        if not isinstance(entry, str) or not entry:
            return None
        text = os.path.expanduser(entry) if entry.startswith('~') else entry
        if not os.path.isabs(text):
            if self.root is None:
                return None
            text = str(self.root / text)
        path = _real(text)
        if path == Path(path.anchor) or _within(self.home, path):
            return None
        if _within(path, self.home):
            first = path.relative_to(self.home).parts[0]
            if first.startswith('.') or first == 'Library':
                return None
        return path

    def hint(self, path):
        """Coarse location for the person's status line: `~/Documents/...` or `/Volumes/...`."""
        if _within(path, self.home):
            parts = path.relative_to(self.home).parts
            return '~/' + (parts[0] + '/...' if len(parts) > 1 else '...')
        parts = path.parts
        return (parts[0] + parts[1] + '/...') if len(parts) > 1 else parts[0]

    def _credential(self, path):
        return (any(_within(path, p) for p in self.credentials)
                or (path.name not in CREDENTIAL_TEMPLATES
                    and any(fnmatch.fnmatch(path.name, pattern) for pattern in CREDENTIAL_NAMES)))

    def scope_of(self, raw, base=None):
        """Where one path sits, independent of session taint: the reusable scope lookup.

        Returns {'category', 'root', 'project', 'sharing', 'capture', 'error'}.
        `category` is one of `credential`, `project` (this session's scoped folder or
        another folder of the same project), `scoped` (another project's scoped
        folder), `claude_state` (`~/.claude/projects/...`, resolved through the folder
        it belongs to), `claude_config`, `personal`, `allowed_path`, `tooling`
        (caches and package folders), `temp`, `system`, `unscoped_repo`, `unknown`
        (exists outside every scoped folder) or `missing` (does not exist, outside
        every scoped folder). `root`, `project`, `sharing` and `capture` describe the
        governing scope file, when there is one."""
        path = self._resolve(raw, base)
        if path is None:
            return _scope('missing')
        return self._scope_of(path)

    def _resolve(self, raw, base=None):
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            return None
        if raw.startswith('~'):
            if raw.split('/', 1)[0] != '~':
                return None
            raw = os.path.expanduser('~') + raw[1:]
        elif not os.path.isabs(raw):
            base = base or self.cwd
            if base is None:
                return None
            raw = os.path.join(str(base), raw)
        return _real(raw)

    def _from_request(self, category, found):
        return _scope(category, found.get('root'), found.get('project'),
                      found.get('sharing') if found.get('sharing') in LEVELS else 'private',
                      found.get('capture'), found.get('error'))

    def _governed(self, scoped):
        """Scope info for a CURRENT marker or a scope request."""
        if scoped is CURRENT:
            return _scope('project', str(self.root), self.project, self.sharing, 'on')
        same = (self.project and scoped.get('project') == self.project and not scoped.get('error')
                and scoped.get('capture') != 'off')
        return self._from_request('project' if same else 'scoped', scoped)

    def _scope_of(self, path):
        if self._credential(path):
            return _scope('credential')
        scoped = self._scoped(path)
        if scoped is not None:
            return self._governed(scoped)
        if _within(path, self.claude) and path != self.claude:
            parts = path.relative_to(self.claude).parts
            if parts[0] in CLAUDE_CONFIG or (len(parts) == 1 and fnmatch.fnmatch(parts[0], CLAUDE_SETTINGS)):
                return _scope('claude_config')
            if parts[0] == 'projects' and len(parts) >= 2:
                return self._claude_state(parts[1])
        if any(_within(path, p) for p in self.personal):
            return _scope('personal')
        if any(_within(path, allowed) for allowed in self.allowed_paths):
            return _scope('allowed_path')
        if any(_within(path, p) for p in self.tooling) or any(part in PACKAGE_DIRS for part in path.parts):
            return _scope('tooling')
        for root, project in (self.config.get('projects') or {}).items():
            if _within(path, _real(root)):  # explicit enrollment without a scope file
                return self._governed({'project': project, 'sharing': 'private', 'capture': 'on', 'root': str(_real(root))})
        exists = os.path.lexists(path)
        if exists and _git_top(path if path.is_dir() else path.parent) is not None:
            return _scope('unscoped_repo', str(_git_top(path if path.is_dir() else path.parent)))
        if not _within(path, self.home) and any(_within(path, p) for p in self.temp):
            return _scope('temp')
        if not _within(path, self.home) and any(_within(path, p) for p in self.system):
            return _scope('system')
        return _scope('unknown' if exists else 'missing')

    def _claude_state(self, name):
        """Scope of `~/.claude/projects/<name>`: the folder the name encodes decides."""
        if self.root is not None and any(name == own or (len(own) > SLUG_MAX and name[:SLUG_MAX] == own[:SLUG_MAX])
                                         for own in self.own_slugs):
            return dict(self._governed(CURRENT), category='claude_state', own=True)
        folder = self._unslug(name)
        scoped = self._scoped(folder) if folder is not None else None
        if scoped is None:
            return _scope('claude_state', own=False)
        return dict(self._governed(scoped), category='claude_state', own=False,
                    same=self._governed(scoped)['category'] == 'project')

    def place(self, raw, base=None):
        """Label for one filesystem path named by a tool, relative to `base` (default cwd)."""
        if isinstance(raw, str) and raw.startswith('~') and raw.split('/', 1)[0] != '~':
            return _restrict('outside_repo', 'outside_repo', '~...')  # another user's home
        path = self._resolve(raw, base)
        if path is None:
            return Label(OPEN, ())  # cannot be placed, and names nothing outside
        if path in self._places:
            return self._places[path]
        label = self._label(path, self._scope_of(path))
        self._places[path] = label
        return label

    def _other(self, path, info):
        """Another project's files: a project-set label, capped by that scope file's request."""
        project = info.get('project')
        if (self.session_allowed is not None and project in self.session_allowed and not info.get('error')
                and PROJECT.fullmatch(project)):
            # Allowed by the session label (PR B): carried, never a narrower ceiling.
            return Label(OPEN, ('other_project',), 'other_project', project, (project,))
        if info.get('error') or info.get('capture') == 'off' or not project or not PROJECT.fullmatch(project):
            return _restrict('other_project', 'other_project', self.hint(path))
        return Label(info['sharing'], ('other_project',), 'other_project', project, (project,))

    def _label(self, path, info):
        category = info['category']
        if category == 'credential':
            return _restrict('outside_repo', 'credential_file', self.hint(path))
        if category == 'project' or (category == 'claude_state' and (info.get('own') or info.get('same'))):
            return Label(OPEN, ('project',))
        if category == 'scoped' or (category == 'claude_state' and info.get('root')):
            return self._other(path, info)
        if category == 'personal':
            return _restrict('outside_repo', 'personal_content', self.hint(path))
        if category == 'allowed_path':
            return Label(OPEN, ('allowed_path',))
        if category in ('tooling', 'temp', 'system', 'claude_config'):
            return Label(OPEN, ('local_tooling',))
        if category == 'unscoped_repo':
            return _restrict('other_project', 'other_project', self.hint(path))
        if category == 'missing':
            return Label(OPEN, ())
        return _restrict('outside_repo', 'outside_repo', self.hint(path))  # unknown, foreign Claude Code state

    def url(self, raw):
        """Public web: unrestricted, origin `web`. Non-public addresses are private; `file:` is a path."""
        try:
            parts = urlsplit(str(raw).strip())
            host = (parts.hostname or '').rstrip('.').lower()
        except ValueError:
            return _restrict('private_network', 'private_network')
        if parts.scheme == 'file':
            return self.place(parts.path or '/')
        if parts.scheme not in ('http', 'https') or not host:
            return _restrict('private_network', 'private_network')
        try:
            address = ipaddress.ip_address(host.strip('[]'))
        except ValueError:
            address = None
        if address is not None:
            return Label(OPEN, ('web',)) if address.is_global else _restrict('private_network', 'private_network', host)
        if ('.' not in host or host == 'localhost'
                or host.endswith(('.localhost', '.local', '.internal', '.intranet', '.corp', '.lan',
                                  '.home', '.home.arpa', '.private', '.test', '.example', '.invalid'))):
            return _restrict('private_network', 'private_network', host)
        return Label(OPEN, ('web',))


# Tool classification ----------------------------------------------------------

_MCP = re.compile(r'mcp__(.+?)__(.+)')
PATH_KEYS = ('file_path', 'notebook_path', 'path', 'filePath', 'directory', 'dir_path', 'target_file')
# Tools that read nothing outside the session: planning, delegation (a subagent's
# own tool calls are classified as they happen), questions to the person, output
# of a shell already classified when it started.
NEUTRAL_TOOLS = {
    'todowrite', 'todoread', 'task', 'agent', 'exitplanmode', 'enterplanmode', 'askuserquestion',
    'skill', 'slashcommand', 'toolsearch', 'bashoutput', 'taskoutput', 'killshell', 'killbash',
    'taskstop', 'taskcreate', 'taskupdate', 'tasklist', 'taskget', 'update_plan', 'spawn_agent',
    'wait', 'subagentactivity', 'collabagenttoolcall', 'send_input', 'close_agent', 'resume_agent',
    'request_user_input', 'sleep',
}
PATH_TOOLS = {'read', 'write', 'edit', 'multiedit', 'notebookedit', 'notebookread', 'ls', 'lsp', 'view_image',
              'read_file', 'list_dir', 'write_file'}
SHELL_TOOLS = {'bash', 'shell', 'local_shell', 'exec_command', 'commandexecution', 'container.exec',
               'unified_exec', 'shell_command', 'monitor', 'powershell', 'write_stdin'}
WEB_TOOLS = {'websearch', 'web_search', 'web_search_call'}
RESOURCE_TOOLS = {'listmcpresourcestool', 'readmcpresourcetool', 'list_mcp_resources',
                  'list_mcp_resource_templates', 'read_mcp_resource'}


def mcp_server(name):
    """Server segment of `mcp__<server>__<tool>` (normalized), or None."""
    match = _MCP.fullmatch(str(name).replace('-', '_').lower())
    return match.group(1) if match else None


def _arguments(value):
    """Rollout function-call arguments arrive JSON-encoded."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except ValueError:
            return value
    return value


def _patch_paths(text):
    return [a or b for a, b in re.findall(r'(?m)^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$',
                                          str(text))]


def classify(event, context):
    """Origin label for one host event (hook or rollout shape)."""
    kind = event.get('hook_event_name')
    if kind not in ('PostToolUse', 'PostToolUseFailure'):
        return NEUTRAL
    from agentclient.enterprise_capture import SELF_SERVERS, self_tool
    tool = str(event.get('tool_name', ''))
    name = tool.replace('-', '_').lower()
    arguments = _arguments(event.get('tool_input', {}))
    arguments = arguments if isinstance(arguments, dict) else {'input': arguments}
    response = event.get('tool_response')
    cwd = arguments.get('workdir') or arguments.get('cwd') or event.get('cwd')
    base = context.cwd
    if isinstance(cwd, str) and cwd:
        base = _real(cwd if os.path.isabs(cwd) else os.path.join(str(context.cwd or '/'), cwd))
    server = event.get('mcp_server') or mcp_server(tool)
    context.transcript(event.get('transcript_path'))
    if self_tool(tool) or server in SELF_SERVERS:
        return recalled(response, context.project, sharing=context.sharing,
                        labelled=context.session_allowed is not None)
    if name in RESOURCE_TOOLS:
        server = str(arguments.get('server', '')).replace('-', '_').lower() or 'unknown'
    if server is not None:
        if server in context.allowed_mcp or any(
                server.startswith('plugin_') and server.endswith('_' + allowed) for allowed in context.allowed_mcp):
            return Label(OPEN, ('mcp', 'allowed_mcp'))
        return _restrict('mcp', 'mcp_server', server)
    if name in NEUTRAL_TOOLS:
        return Label(OPEN, ())
    if name in PATH_TOOLS:
        targets = [arguments[k] for k in PATH_KEYS if isinstance(arguments.get(k), str)] or ['.']
        return combine([context.place(t, base) for t in targets])
    if name in ('glob', 'grep'):
        labels = [context.place(arguments.get('path') or '.', base)]
        for key in ('pattern', 'glob') if name == 'glob' else ('glob',):
            value = arguments.get(key)
            if isinstance(value, str) and (value.startswith(('/', '~')) or '..' in value.split('/')):
                labels.append(context.place(_glob_base(value), base))
        listed = response.get('filenames') if isinstance(response, dict) else None
        for item in (listed if isinstance(listed, list) else [])[:2000]:
            if isinstance(item, str):
                labels.append(context.place(item, base))
        return combine(labels)
    if name in ('apply_patch', 'filechange'):
        targets = _patch_paths(arguments.get('command') or arguments.get('input') or arguments.get('patch') or '')
        changes = arguments.get('changes')
        if isinstance(changes, dict):
            targets.extend(str(path) for path in changes)
        elif isinstance(changes, list):
            targets.extend(str(c.get('path')) for c in changes if isinstance(c, dict) and c.get('path'))
        return combine([context.place(t, base) for t in targets])
    if name == 'webfetch':
        return context.url(arguments.get('url', ''))
    if name in WEB_TOOLS:
        return Label(OPEN, ('web',))
    if name in SHELL_TOOLS:
        command = arguments.get('command', arguments.get('cmd', arguments.get('chars', arguments.get('input'))))
        return shell(command, context, base)
    return _restrict('unknown_tool', 'unknown_tool', tool[:64] or 'unnamed tool')


def _glob_base(pattern):
    """The directory part of a glob pattern before its first wildcard."""
    parts = []
    for part in pattern.split('/'):
        if re.search(r'[*?\[{]', part):
            break
        parts.append(part)
    joined = '/'.join(parts)
    return joined or ('/' if pattern.startswith('/') else '.')


# Recalled memory ----------------------------------------------------------------

def item_label(item, project, sharing_request=None, *, labelled=False):
    """Label for one recalled item from the service's origin label.

    The service labels each item (`origin`) with its source project (None when
    its sources span several), its current sharing level and the other projects
    whose content it already carries (`projects`). An organization item can be restated
    anywhere; a team item keeps its level and adds its projects to the event's
    project set; a private or unlabelled item is private. A preference is the
    person's own standing instruction, like a prompt, and does not restrict. A
    team or organization item of the current project alone does not restrict when
    its sharing is at least the scope file's request (`sharing_request`): its
    audience is not narrower than the session's."""
    if not isinstance(item, dict):
        return _restrict('recalled_memory', 'recalled_memory', 'shared memory')
    provenance = item.get('provenance') if isinstance(item.get('provenance'), dict) else {}
    if provenance.get('source_type') == 'preference' or item.get('evidence_status') == 'source_linked_preference':
        return Label(OPEN, ())
    label = item.get('origin') if isinstance(item.get('origin'), dict) else {}
    sharing = label.get('sharing')
    carried = label.get('projects') if isinstance(label.get('projects'), list) else []
    named = [p for p in [label.get('project'), *carried] if p]
    projects = {p for p in named if isinstance(p, str) and PROJECT.fullmatch(p)}
    if len(projects) != len(set(map(str, named))):
        return _restrict('recalled_memory', 'recalled_memory', 'shared memory')
    projects.discard(project)
    if labelled and sharing in ('organization', 'team'):
        # A labelled session's recall is filtered by the service to items whose audience
        # contains the session's: it carries their workspaces and narrows nothing.
        return Label(OPEN, ('recalled_memory',), 'recalled_memory' if projects else None,
                     'shared memory' if projects else None, projects)
    if sharing == 'organization' and not carried:
        return Label(OPEN, ('recalled_memory',))
    if sharing == 'team' and not named:
        return _restrict('recalled_memory', 'recalled_memory', 'shared memory')  # spans projects it does not name
    if (sharing in ('organization', 'team') and not projects and sharing_request in LEVELS
            and RANK[sharing] >= RANK[sharing_request]):
        return Label(OPEN, ('recalled_memory',))
    if sharing in ('organization', 'team'):
        return Label('team' if sharing == 'team' else OPEN, ('recalled_memory',),
                     'recalled_memory', 'shared memory', projects)
    return _restrict('recalled_memory', 'recalled_memory', 'shared memory')


def recalled(items, project, *, cards=False, sharing=None, labelled=False):
    """Label for recalled memory: injected `cards`, or a memory tool's result.

    Every injected card is a memory item, so one without a label is private. A
    tool result is searched for items; text without any item restricts nothing."""
    found = [item for item in items if isinstance(item, dict)] if cards and isinstance(items, list) else _cards(items)
    if not found:
        return Label(OPEN, ())
    return combine([item_label(item, project, sharing, labelled=labelled) for item in found])


def _cards(value, depth=0):
    """Memory items in a result: dicts carrying an id plus a label or provenance."""
    if depth > 6:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in '[{':
            try:
                return _cards(json.loads(text), depth + 1)
            except ValueError:
                return []
        return []
    if isinstance(value, list):
        return [card for item in value[:200] for card in _cards(item, depth + 1)]
    if isinstance(value, dict):
        if (isinstance(value.get('origin'), dict) and 'sharing' in value['origin']) or (
                'provenance' in value and ('id' in value or 'document_id' in value)):
            return [value]
        return [card for item in list(value.values())[:200] for card in _cards(item, depth + 1)]
    return []


# Shell ----------------------------------------------------------------------------

SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish'}
# Their remaining arguments are a command for another computer, not local paths.
REMOTE = {'ssh', 'mosh'}
PREFIXES = {'command', 'builtin', 'exec', 'nice', 'nohup', 'time', 'stdbuf', 'caffeinate', 'xargs', 'noglob',
            'unbuffer', 'ionice', 'chronic', 'env', 'timeout', 'gtimeout', 'sudo'}
REDIRECT = re.compile(r'\d*(?:<|>|>>|>\||&>|&>>|<>|>&|<&|<<<)')
HEREDOC = re.compile(r'\d*<<-?')
SEPARATOR = re.compile(r'[;&|()!{}\n`]+')
# Inline code that asks for the home folder without naming a path.
INDIRECT_HOME = re.compile(r'expanduser|Path\.home|os\.homedir|homedir\(|USERPROFILE|getpwuid|'
                           r'environ\[\s*[\'"]HOME|getenv\(\s*[\'"]HOME|env\.HOME|ENV\[\s*[\'"]HOME')
ABSOLUTE = re.compile(r'(?<![\w.:/$~@-])/(?:[\w.@+%,-]+/?)+')
UP = re.compile(r'(?<![\w./])(?:\.\./)+[\w.@+%,/-]*|(?<![\w./])\.\.(?![\w./])')
TILDE = re.compile(r'(?<![\w$~/])~(?=/|[\s\'")]|$)[\w.@+%,/-]*')
VARIABLE = re.compile(r'^\$(?:\{(\w+)\}|(\w+))')


def _script(command):
    """The script text from a string, or from an argv such as ["bash", "-lc", "..."]."""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command and all(isinstance(x, str) for x in command):
        if Path(command[0]).name in SHELLS and len(command) >= 3 and _dash_c(command[1:-1]):
            return command[-1]
        return shlex.join(command)
    return None


def _dash_c(args):
    return any(a.startswith('-') and not a.startswith('--') and 'c' in a[1:] for a in args)


def _tokens(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = ''
    return list(lexer)


def _commands(tokens):
    """(words, redirect targets) for each simple command between control operators."""
    words, redirects = [], []
    i = 0
    while i < len(tokens):
        token = tokens[i].strip('`')
        if HEREDOC.fullmatch(token) and i + 1 < len(tokens):
            # The body up to the delimiter line is data for this command, not commands.
            delimiter = tokens[i + 1]
            i += 2
            while i < len(tokens) and tokens[i] != '\n':
                words.append(tokens[i])
                i += 1
            while i < len(tokens) and not (tokens[i] == delimiter and tokens[i - 1] == '\n'):
                i += 1
            i += 1
            continue
        if REDIRECT.fullmatch(token):
            if i + 1 < len(tokens):
                target = tokens[i + 1]
                if not (target.isdigit() or target == '-' or SEPARATOR.fullmatch(target)):
                    redirects.append(target)
            i += 2
            continue
        if not token or SEPARATOR.fullmatch(token) or token == '$':
            if words or redirects:
                yield words, redirects
            words, redirects = [], []
        else:
            words.append(token)
        i += 1
    yield words, redirects


def _expand(value, context):
    """`~`, `$HOME` and `$TMPDIR` expanded; None for any other variable (unknowable)."""
    if value.startswith('~'):
        return value
    match = VARIABLE.match(value)
    if not match:
        return value
    name = match.group(1) or match.group(2)
    rest = value[match.end():]
    if name == 'HOME':
        return str(context.home) + rest
    if name == 'TMPDIR':
        return tempfile.gettempdir() + rest
    if name == 'PWD' and context.cwd is not None:
        return str(context.cwd) + rest
    return None


def shell(command, context, base=None, depth=0):
    """Label for a shell command, from the paths it names; no paths, no restriction."""
    text = _script(command)
    if text is None or depth > 4:
        return Label(OPEN, ('shell',))
    base = base or context.cwd
    labels = []
    if INDIRECT_HOME.search(text):
        labels.append(_restrict('outside_repo', 'shell_outside_repo', '~/...'))
    try:
        tokens = _tokens(text)
    except ValueError:
        tokens = []  # unbalanced quotes: the raw scans below still see every path
    cwd = base
    for words, redirects in _commands(tokens):
        for target in redirects:
            labels.append(_shell_path(_place_word(target, context, cwd)))
        words = list(words)
        while words and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0]):
            labels.append(_shell_path(_place_word(words.pop(0).split('=', 1)[1], context, cwd)))
        while words and Path(words[0]).name in PREFIXES and len(words) > 1:
            program = Path(words.pop(0)).name
            while words and (words[0].startswith('-') or (program == 'env' and '=' in words[0])):
                words.pop(0)
            if program in ('timeout', 'gtimeout') and words:
                words.pop(0)
        if not words:
            continue
        program, args = Path(words[0]).name, words[1:]
        if '/' in words[0]:
            labels.append(_shell_path(_place_word(words[0], context, cwd)))
        if program in ('cd', 'pushd', 'chdir'):
            target = next((a for a in args if not a.startswith('-')), '~')
            expanded = _expand(target, context)
            if target == '-' or expanded is None:
                cwd = None  # unknown from here on: relative paths cannot be placed
                continue
            labels.append(_shell_path(_place_word(target, context, cwd)))
            if cwd is not None or os.path.isabs(os.path.expanduser(expanded)):
                cwd = _real(os.path.join(str(cwd or '/'), os.path.expanduser(expanded)))
            continue
        if program == 'eval' or (program in SHELLS and _dash_c([a for a in args if a.startswith('-')])):
            script = ' '.join(args) if program == 'eval' else next((a for a in args if not a.startswith('-')), '')
            labels.append(shell(script, context, cwd, depth + 1))
            continue
        if program in REMOTE:
            continue
        for arg in args:
            value = arg.split('=', 1)[1] if arg.startswith('-') and '=' in arg else arg
            if '://' in value or (program in ('scp', 'rsync') and re.match(r'[\w.@-]+:', value)):
                continue  # a URL or a remote path names nothing on this computer
            if _pathish(value):
                labels.append(_shell_path(_place_word(value, context, cwd)))
            elif (cwd is not None and value and not value.startswith(('-', '$')) and len(value) < 256
                    and os.path.lexists(os.path.join(str(cwd), value))):
                # A bare name that exists here can still be a symlink out of the project.
                labels.append(_shell_path(context.place(value, cwd)))
    # Paths inside quoted inline code (python -c "open('/x')") or substitutions.
    for pattern in (ABSOLUTE, TILDE):
        for match in pattern.finditer(text):
            labels.append(_shell_path(context.place(_glob_base(match.group(0)), base)))
    if base is not None:
        for match in UP.finditer(text):
            labels.append(_shell_path(context.place(_glob_base(match.group(0)), base)))
    if not labels:
        return Label(OPEN, ('project', 'shell'))
    result = combine(labels)
    return Label(result['ceiling'], set(result['kinds']) | {'shell'}, result['reason'], result['hint'],
                 result['projects'])


def _place_word(word, context, cwd):
    expanded = _expand(word, context)
    if expanded is None:
        return Label(OPEN, ())  # an unknown variable names nothing we can see
    if not os.path.isabs(expanded) and not expanded.startswith('~') and cwd is None:
        return Label(OPEN, ())
    return context.place(_glob_base(expanded), cwd)


def _pathish(value):
    return bool(value) and (value.startswith(('/', '~', './', '../', '$')) or value in ('.', '..') or '/' in value)


def _shell_path(label):
    """A path named by a shell command; a plain outside path reads as `shell_outside_repo`."""
    if label['ceiling'] == OPEN or label['reason'] != 'outside_repo':
        return label
    return Label(label['ceiling'], label['kinds'], 'shell_outside_repo', label['hint'], label['projects'])


# Session state --------------------------------------------------------------------

def _key(session):
    return hashlib.sha256(str(session).encode()).hexdigest()[:32]


class SessionOrigins:
    """Sticky per-session labels in the client profile (`origin-sessions.json`, 0600).

    Hooks run as separate processes, so the state is a locked file. An entry holds
    the session's ceiling, other projects, the first reason category, when it
    started and a coarse display hint, plus recent tool-call labels so a rollout's
    separate call output inherits its call's label. Entries untouched for 30 days
    are dropped."""

    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / STATE

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, value):
        with tempfile.NamedTemporaryFile(mode='w', dir=self.home, prefix='origin-', suffix='.tmp', delete=False) as stream:
            json.dump(value, stream, sort_keys=True)
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def _locked(self, change):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.home / 'origin-sessions.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._read()
            result = change(state)
            cutoff = time.time() - STATE_DAYS * 86400
            for key in [k for k, v in state.items() if not isinstance(v, dict) or v.get('updated', 0) < cutoff]:
                state.pop(key)
            self._write(state)
            return result

    def get(self, session, *, key=None):
        return self._read().get(key or _key(session))

    def apply(self, session, label, *, project=None, call_id=None, call_output=False, key=None):
        """Record one event of `project` (the captured project, if any); returns its effective label.

        {'ceiling', 'kinds', 'reason', 'projects', 'inherited', 'newly_restricted',
        'hint'}: the ceiling is the lower of the event's own and the session's. The
        projects are every other project whose content is in the session: those
        its tools read, and those it was captured in before (a session that moves
        from one repository to another carries the first one's context along).
        `inherited` is true when the restriction comes only from earlier events."""
        now = time.time()
        key = key or _key(session)
        if label['ceiling'] == OPEN and not label['projects'] and not call_id and not call_output:
            entry = self.get(session, key=key)
            if (entry is None and project is None) or (
                    entry is not None and entry.get('ceiling', OPEN) == OPEN
                    and not set(entry.get('projects', [])) | set(entry.get('visited', [])) - {project}
                    and (project is None or project in entry.get('visited', []))):
                # Nothing new to record: no file write on the common path.
                return {'ceiling': OPEN, 'kinds': list(label['kinds']), 'reason': None, 'projects': [],
                        'inherited': False, 'newly_restricted': False, 'hint': None}

        def change(state):
            entry = state.setdefault(key, {'ceiling': OPEN, 'projects': [], 'visited': [], 'calls': {}})
            entry['updated'] = now
            calls = entry.setdefault('calls', {})
            own = label
            if call_output:
                # A rollout reports a call's output separately; it carries the call's label.
                recorded = calls.get(str(call_id)) if call_id else None
                if isinstance(recorded, list) and len(recorded) == 3 and recorded[0] in LEVELS:
                    own = Label(recorded[0], (), recorded[1], None, recorded[2])
                else:
                    own = _restrict('unknown_call', 'unknown_call', 'tool output')
            elif call_id:
                calls[str(call_id)] = [own['ceiling'], own['reason'], own['projects']]
                for stale in list(calls)[:-MAX_CALLS]:
                    calls.pop(stale)
            visited = set(entry.get('visited', []))
            others = set(entry.get('projects', []))
            before = entry.get('ceiling', OPEN)
            # What the session's previous event carried: moving to another project is news too.
            carried_before = set(entry.get('carried', []))
            others |= set(own['projects']) - {project}
            if project:
                visited.add(project)
            ceiling = lower(own['ceiling'], before)
            if len(others | visited) > MAX_PROJECTS:
                # Too many projects to name: the session falls back to private.
                others, ceiling = set(), 'private'
                own = Label('private', own['kinds'], 'other_project', own.get('hint'))
            projects = (others | visited) - {project}
            lowered = RANK[ceiling] < RANK[before]
            added = projects - carried_before
            newly = lowered or bool(added)
            if newly and (lowered or not entry.get('reason')):
                entry.update(reason=own['reason'] or 'other_project', since=now,
                             hint=own.get('hint') if own['reason'] else ', '.join(sorted(added)))
            entry.update(ceiling=ceiling, projects=sorted(others), visited=sorted(visited), carried=sorted(projects))
            if ceiling == OPEN and not projects:
                reason, inherited = None, False
            elif newly or own['reason']:
                reason, inherited = own['reason'] or 'other_project', False
            else:
                reason, inherited = entry.get('reason') or 'other_project', True
            return {'ceiling': ceiling, 'kinds': list(own['kinds']), 'reason': reason, 'projects': sorted(projects),
                    'inherited': inherited, 'newly_restricted': newly, 'hint': entry.get('hint')}

        return self._locked(change)

    def forget(self, session):
        self._locked(lambda state: state.pop(_key(session), None))


def wire(effective, project=None):
    """The `origin_label` sent with an event of `project`, or None when there is nothing to say.

    Only the ceiling, the kinds touched, the reason category and other project
    names travel; never the display hint, a path, a command or a server name."""
    if not effective:
        return None
    kinds = sorted({k for k in effective.get('kinds', ()) if k in KINDS})
    projects = sorted(set(effective.get('projects') or ()) - {project})
    restricted = effective['ceiling'] != OPEN or bool(projects)
    if not restricted and set(kinds) <= {'project', 'shell', 'local_tooling'}:
        return None
    reason = effective.get('reason') if restricted else ''
    if restricted and reason not in REASONS:
        reason = 'other_project' if projects and effective['ceiling'] != 'private' else 'unknown_tool'
    return {'ceiling': effective['ceiling'], 'kinds': kinds, 'reason': reason, 'projects': projects,
            'inherited': bool(effective.get('inherited'))}


PHRASES = {
    'outside_repo': 'read outside the repo ({hint})',
    'shell_outside_repo': 'ran a shell command outside the repo ({hint})',
    'personal_content': 'read personal files ({hint})',
    'credential_file': 'read a credentials file ({hint})',
    'other_project': 'read project {hint}',
    'mcp_server': 'used MCP server {hint}',
    'private_network': 'fetched a non-public address ({hint})',
    'recalled_memory': 'recalled memory with narrower sharing',
    'unknown_tool': 'used a tool Vaelius cannot classify ({hint})',
    'unknown_call': 'received a tool result Vaelius cannot place',
    'explicit_private': 'was made private on request',
}


def status_line(entry, *, now=False):
    """One short line for the person: why this session's capture became restricted."""
    if not entry or (entry.get('ceiling', OPEN) == OPEN
                     and len(set(entry.get('projects', [])) | set(entry.get('visited', []))) < 2):
        return None
    projects = sorted(set(entry.get('projects', [])) | set(entry.get('visited', [])))
    if entry.get('reason') == 'other_project' and entry.get('ceiling') != 'private' and len(projects) > 1:
        phrase = 'spans projects ' + ', '.join(projects)
    else:
        phrase = PHRASES.get(entry.get('reason'), 'touched content Vaelius cannot place').format(
            hint=entry.get('hint') or 'unknown')
    if entry['ceiling'] == 'private':
        level = 'private'
    elif len(projects) > 1:
        level = ('limited to ' + entry['ceiling'] + ' and to members of every project involved'
                 if entry['ceiling'] != OPEN else 'limited to members of every project involved')
    else:
        level = 'limited to ' + entry['ceiling']
    return 'vaelius: this session ' + phrase + ' — capture is ' + ('now ' if now else '') + level


# Session start --------------------------------------------------------------------

SCOPE_NOTICE_KEY = 'scope_context_notice'
SCOPE_LINE_MAX = 200
SCOPE_SIBLINGS = 200  # folder entries examined next to the root


def in_scope_line(scope, cwd, config=None, *, labelled=False, allowed=None):
    """One informational line naming the session's scoped folders, or None.

    The current root plus scoped sibling folders next to it, while the line stays
    under SCOPE_LINE_MAX characters. A root at an ignored location (home, a
    personal folder) is never listed."""
    context = Context(scope, cwd, config)
    root = context.root
    if root is None or not context.project:
        return None
    if not _within(root, context.home) and any(_within(root, p) for p in context.temp):
        shown = str(root)
    elif _within(root, context.home):
        shown = '~/' + str(root.relative_to(context.home))
    else:
        shown = str(root)
    head, tail = 'vaelius: in-scope folders: ', ' (reads elsewhere make this session private)'
    if labelled:
        tail = ' (narrower workspaces are denied; unscoped reads make this session private)'
    line = head + shown + tail
    if len(line) > SCOPE_LINE_MAX:
        return None
    try:
        with os.scandir(root.parent) as entries:
            names = sorted(e.name for _, e in zip(range(SCOPE_SIBLINGS), entries))
    except OSError:
        names = []
    pieces = [shown]
    for name in names:
        sibling = root.parent / name
        if sibling == root or not sibling.is_dir() or context._unscopable(sibling):
            continue
        info = context._scoped_dir(sibling)
        if not isinstance(info, dict) or info.get('root') != str(sibling) or info.get('error') or info.get('capture') == 'off':
            continue
        if allowed is not None and info.get('project') not in allowed:
            continue  # a labelled session does not read narrower workspaces
        candidate = head + ', '.join(pieces + ['../' + name]) + tail
        if len(candidate) > SCOPE_LINE_MAX:
            break
        pieces.append('../' + name)
    return head + ', '.join(pieces) + tail
