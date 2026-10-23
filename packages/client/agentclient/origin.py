"""Origin labels and session taint for captured agent events.

A repository scope file decides capture for a whole session, but one tool call
can read far outside the repository: a document in the person's home folder, a
mail or chat MCP server, a credentials file. This module looks at what each
captured tool event touched and derives an **origin label**: a ceiling (the
widest sharing that content may get) plus the set of *other* projects whose
content it carries.

Paths (symlinks and `..` resolved first):

* inside the current project's root, or governed by the same project's scope
  file (another checkout or worktree): no restriction;
* inside another repository with its own scope file: tagged with that project.
  The service then shares the event no wider than both projects allow, and for
  `team` only with people who are members of both. The other scope file's own
  sharing request also caps the ceiling. A repository without a scope file, or
  whose scope file is off or invalid: `private`;
* personal folders (Documents, Desktop, Downloads, Pictures, Movies, Music,
  iCloud and cloud-drive folders, Mail, Messages) and credential files: always
  `private`, and no allowlist can open them;
* temporary folders, tool caches and package folders (`~/.cache`, `~/.npm`,
  `site-packages`, `node_modules`, Claude Code plugin folders, ...), read-only
  system locations (`/usr`, `/opt/homebrew`, `/etc`, ...) and the scope file's
  `capture.allowed_paths`: no restriction;
* anything else that exists outside the project (the rest of the home folder,
  other volumes): `private`. A path that does not exist exposes no content.

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
project-set rule. Unknown tools fail closed to `private`.

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
        self.allowed_paths = [p for p in (self._allowed(entry) for entry in (scope or {}).get('allowed_paths') or ())
                              if p is not None]
        self.allowed_mcp = {str(s).replace('-', '_').lower() for s in (scope or {}).get('allowed_mcp_servers') or ()}
        self.system = [_real(p) for p in SYSTEM] + sorted(_temp_roots())
        self.personal = [self.home / p for p in PERSONAL]
        self.credentials = [self.home / p for p in CREDENTIALS]
        self.tooling = [self.home / p for p in TOOLING]
        self._places = {}

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

    def _governing(self, path):
        """Scope request {project, sharing, capture, path} governing `path`, or None.

        Explicit enrollment without a scope file counts too, with private sharing."""
        from agentclient.scope_file import discover
        probe = path
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            found = discover(probe, self.names)
        except Exception:
            found = None
        if found is not None:
            return found
        for root, project in (self.config.get('projects') or {}).items():
            if _within(path, _real(root)):
                return {'project': project, 'sharing': 'private', 'capture': 'on', 'path': None}
        return None

    def place(self, raw, base=None):
        """Label for one filesystem path named by a tool, relative to `base` (default cwd)."""
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            return Label(OPEN, ())
        if raw.startswith('~'):
            head = raw.split('/', 1)[0]
            if head != '~':
                return _restrict('outside_repo', 'outside_repo', '~...')  # another user's home
            text = os.path.expanduser('~') + raw[1:]
        elif os.path.isabs(raw):
            text = raw
        else:
            base = base or self.cwd
            if base is None:
                return Label(OPEN, ())  # cannot be placed, and names nothing outside
            text = os.path.join(str(base), raw)
        if text in self._places:
            return self._places[text]
        label = self._place(_real(text))
        self._places[text] = label
        return label

    def _sensitive(self, path):
        if any(_within(path, p) for p in self.credentials) or any(
                fnmatch.fnmatch(path.name, pattern) for pattern in CREDENTIAL_NAMES):
            return 'credential_file'
        if any(_within(path, p) for p in self.personal):
            return 'personal_content'
        return None

    def _other(self, path, found):
        """Another project's files: a project-set label, capped by that scope file's request."""
        project = found.get('project')
        if found.get('error') or found.get('capture') == 'off' or not project or not PROJECT.fullmatch(project):
            return _restrict('other_project', 'other_project', self.hint(path))
        if self.project and project == self.project:
            return Label(OPEN, ('project',))  # another checkout or worktree of this project
        sharing = found.get('sharing') if found.get('sharing') in LEVELS else 'private'
        return Label(sharing, ('other_project',), 'other_project', project, (project,))

    def _place(self, path):
        if self.root is not None and _within(path, self.root):
            found = self._governing(path)
            # A nested repository with its own scope file for a different project.
            if found and found.get('path') and self.scope_file and found['path'] != self.scope_file \
                    and found.get('project') != self.project:
                return self._other(path, found)
            return Label(OPEN, ('project',))
        sensitive = self._sensitive(path)
        if sensitive:
            return _restrict('outside_repo', sensitive, self.hint(path))
        if any(_within(path, allowed) for allowed in self.allowed_paths):
            return Label(OPEN, ('allowed_path',))
        if any(_within(path, p) for p in self.tooling) or any(part in PACKAGE_DIRS for part in path.parts):
            return Label(OPEN, ('local_tooling',))
        found = self._governing(path)
        if found is not None:
            return self._other(path, found)
        exists = os.path.lexists(path)
        if exists and _git_top(path if path.is_dir() else path.parent) is not None:
            return _restrict('other_project', 'other_project', self.hint(path))  # a repository without a scope file
        if _within(path, self.home):
            return _restrict('outside_repo', 'outside_repo', self.hint(path)) if exists else Label(OPEN, ())
        if any(_within(path, p) for p in self.system):
            return Label(OPEN, ('local_tooling',))
        return _restrict('outside_repo', 'outside_repo', self.hint(path)) if exists else Label(OPEN, ())

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
    if self_tool(tool) or server in SELF_SERVERS:
        return recalled(response, context.project)
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

def item_label(item, project):
    """Label for one recalled item from the service's origin label.

    The service labels each item (`origin`) with its source project (None when
    its sources span several), its current sharing level and the other projects
    whose content it already carries (`projects`). An organization item can be restated
    anywhere; a team item keeps its level and adds its projects to the event's
    project set; a private or unlabelled item is private. A preference is the
    person's own standing instruction, like a prompt, and does not restrict."""
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
    if sharing == 'organization' and not carried:
        return Label(OPEN, ('recalled_memory',))
    if sharing == 'team' and not named:
        return _restrict('recalled_memory', 'recalled_memory', 'shared memory')  # spans projects it does not name
    if sharing in ('organization', 'team'):
        return Label('team' if sharing == 'team' else OPEN, ('recalled_memory',),
                     'recalled_memory', 'shared memory', projects)
    return _restrict('recalled_memory', 'recalled_memory', 'shared memory')


def recalled(items, project, *, cards=False):
    """Label for recalled memory: injected `cards`, or a memory tool's result.

    Every injected card is a memory item, so one without a label is private. A
    tool result is searched for items; text without any item restricts nothing."""
    found = [item for item in items if isinstance(item, dict)] if cards and isinstance(items, list) else _cards(items)
    if not found:
        return Label(OPEN, ())
    return combine([item_label(item, project) for item in found])


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

    def get(self, session):
        return self._read().get(_key(session))

    def apply(self, session, label, *, project=None, call_id=None, call_output=False):
        """Record one event of `project` (the captured project, if any); returns its effective label.

        {'ceiling', 'kinds', 'reason', 'projects', 'inherited', 'newly_restricted',
        'hint'}: the ceiling is the lower of the event's own and the session's. The
        projects are every other project whose content is in the session: those
        its tools read, and those it was captured in before (a session that moves
        from one repository to another carries the first one's context along).
        `inherited` is true when the restriction comes only from earlier events."""
        now = time.time()
        if label['ceiling'] == OPEN and not label['projects'] and not call_id and not call_output:
            entry = self.get(session)
            if (entry is None and project is None) or (
                    entry is not None and entry.get('ceiling', OPEN) == OPEN
                    and not set(entry.get('projects', [])) | set(entry.get('visited', [])) - {project}
                    and (project is None or project in entry.get('visited', []))):
                # Nothing new to record: no file write on the common path.
                return {'ceiling': OPEN, 'kinds': list(label['kinds']), 'reason': None, 'projects': [],
                        'inherited': False, 'newly_restricted': False, 'hint': None}

        def change(state):
            entry = state.setdefault(_key(session), {'ceiling': OPEN, 'projects': [], 'visited': [], 'calls': {}})
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
