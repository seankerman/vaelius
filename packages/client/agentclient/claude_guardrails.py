"""Least-privilege guardrail profiles for Claude Code, and the PreToolUse check behind them.

The goal: an agent working in a captured repository does not read the person's
own files, so nothing personal reaches a transcript or shared memory.

standard (default) keeps normal engineering work unhindered. It is a denylist:
  * Read/Edit deny rules for credential files. Claude Code applies them to its
    file tools and to the file commands it recognizes in Bash (`cat`, `head`,
    `sed`, redirections ...), but not to programs that read their own
    configuration, so `aws`, `git`, `gh` and package managers keep working.
  * A PreToolUse hook (`check`) that refuses file-tool and Bash access to the
    personal content folders (Documents, Desktop, Downloads, Mail, iCloud
    Drive ...) unless the project itself lives there. Deny rules cannot do this
    part: a deny rule for ~/Documents would also block a project inside it.
  No Bash sandbox: its network isolation breaks git over SSH on macOS, database
  clients and localhost services, and its read denylist also applies to the
  child processes of a tool, so it cannot tell `cat ~/.aws/credentials` from
  `aws s3 ls` reading the same file.

strict (opt-in) confines the agent to the repository:
  permissions.blockReadsOutsideWorkingDirectories, the Bash sandbox (no
  unsandboxed fallback or retry; package caches stay usable), the hook refusing
  edits outside the project, and deny rules for the settings files that could
  switch the profile off. Tools that read configuration from the home folder
  (aws, gh, git over SSH) do not work inside the sandbox under strict.

Plugins cannot ship permission or sandbox settings, so `vaelius-client install
--claude-code --guardrails ...` writes the profile into a settings file.
"""
import copy
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

PROFILES = ('standard', 'strict')
DEFAULT_PROFILE = 'standard'
# Personal content folders, relative to the home folder (macOS and Linux names).
PERSONAL_DIRS = ('Documents', 'Desktop', 'Downloads', 'Pictures', 'Movies', 'Music', 'Videos',
                 'Library/Mobile Documents', 'Library/CloudStorage', 'Library/Mail', 'Library/Messages')
# Credential files, relative to the home folder; `/**` covers a whole folder.
CREDENTIAL_FILES = ('.aws/credentials', '.aws/sso/cache/**', '.aws/cli/cache/**', '.ssh/id_*', '.netrc',
                    '.git-credentials', '.config/gh/hosts.yml', '.docker/config.json', '.npmrc', '.pypirc',
                    '.kube/config')
# strict: package caches sandboxed commands may read and write. Specific subfolders:
# ~/.cache, ~/.cargo, ~/.m2 and ~/.gradle also hold credentials.
CACHE_PATHS = ('~/.cache/uv', '~/.cache/pip', '~/.cache/pre-commit', '~/.cache/go-build',
               '~/Library/Caches/pip', '~/Library/Caches/go-build', '~/.npm', '~/.yarn/berry/cache',
               '~/.local/share/pnpm/store', '~/Library/pnpm/store', '~/.cargo/registry', '~/.cargo/git',
               '~/go/pkg/mod', '~/.gradle/caches', '~/.m2/repository')
# strict: interpreters and toolchains installed under the home folder, read only.
TOOLCHAIN_PATHS = ('~/.local/share/uv/python', '~/.local/bin', '~/.cargo/bin', '~/.rustup',
                   '~/.nvm', '~/.pyenv', '~/.volta', '~/go/bin')
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
FILE_TOOLS = ('Read', 'Grep', 'Glob', 'LSP') + EDIT_TOOLS
# Only letters and `|`: Claude Code compares these as exact tool names, not a regex.
MATCHER = '|'.join(FILE_TOOLS + ('Bash',))
NOTICE_KEY = 'claude_code_guardrail_notice'
ALLOW_ENV = 'VAELIUS_GUARDRAIL_ALLOW'


def _rule_path(path):
    """An absolute path as a Read/Edit rule pattern (`//` anchors at the root)."""
    text = str(path)
    for special in ('\\', '[', ']', '*', '?'):
        text = text.replace(special, '\\' + special)
    return '//' + text.lstrip('/')


def credential_rules():
    rules = []
    for path in CREDENTIAL_FILES:
        rules += ['Read(~/' + path + ')', 'Edit(~/' + path + ')']
    return rules


def protected_settings_rules(settings_file):
    """Edit rules for the files that could switch the profile off mid-session."""
    rules = [_rule_path(settings_file), './.claude/settings.json', './.claude/settings.local.json']
    return ['Edit(' + rule + ')' for rule in dict.fromkeys(rules)]


def profile(name, *, hook_command, settings_file):
    """{'scalars': {dotted key: value}, 'lists': {dotted key: [entries]}, 'hook': {...}}."""
    if name not in PROFILES:raise ValueError('invalid_guardrail_profile')
    scalars, lists = {}, {'permissions.deny': credential_rules()}
    if name == 'strict':
        scalars = {'permissions.blockReadsOutsideWorkingDirectories': True, 'sandbox.enabled': True,
                   # No unsandboxed retry, and no unsandboxed fallback when the sandbox can't start.
                   'sandbox.failIfUnavailable': True, 'sandbox.allowUnsandboxedCommands': False}
        lists['permissions.deny'] += protected_settings_rules(settings_file)
        lists['sandbox.filesystem.allowRead'] = list(CACHE_PATHS + TOOLCHAIN_PATHS)
        lists['sandbox.filesystem.allowWrite'] = list(CACHE_PATHS)
    return {'name': name, 'scalars': scalars, 'lists': lists,
            'hook': {'event': 'PreToolUse', 'matcher': MATCHER, 'command': hook_command}}


def hook_group(desired):
    hook = desired['hook']
    return {'matcher': hook['matcher'], 'hooks': [{'type': 'command', 'command': hook['command'], 'timeout': 10}]}


def render(name, **values):
    """The profile as a settings document (for the reference files and docs)."""
    desired = profile(name, **values);settings = {}
    for key, value in desired['scalars'].items():_set(settings, key, value)
    for key, entries in desired['lists'].items():_set(settings, key, list(entries))
    settings['hooks'] = {'PreToolUse': [hook_group(desired)]}
    return settings


# ---- Merging into a settings file ---------------------------------------------------

def _get(settings, key):
    node = settings
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:return False, None
        node = node[part]
    return True, node


def _container(settings, key, created):
    """The dict that holds `key`'s last part, creating (and recording) missing parents."""
    node = settings;path = []
    for part in key.split('.')[:-1]:
        path.append(part)
        if part not in node:
            node[part] = {};created.append('.'.join(path))
        node = node[part]
        if not isinstance(node, dict):raise ValueError('claude_settings_not_an_object:' + '.'.join(path))
    return node


def _set(settings, key, value, created=None):
    _container(settings, key, [] if created is None else created)[key.split('.')[-1]] = value


def _delete(settings, key):
    present, _ = _get(settings, key)
    if present:
        parent = settings
        for part in key.split('.')[:-1]:parent = parent[part]
        parent.pop(key.split('.')[-1])


def _prune(settings, created):
    """Drop containers this installation created once they are empty, deepest first."""
    for key in sorted(created, key=lambda k: -k.count('.')):
        present, value = _get(settings, key)
        if present and value in ({}, []):_delete(settings, key)


def _restore(settings, key, record):
    """Put back what was there, unless someone changed the value since."""
    present, current = _get(settings, key)
    if not present or current != record['value']:return
    if record['had']:_set(settings, key, record['previous'])
    else:_delete(settings, key)


def _remove_hook(hooks, command):
    from agentclient.claude_code import _remove_command
    _remove_command(hooks, command)


def validate(settings, desired, owned):
    """Raise before any write when a key the profile needs has an unexpected type."""
    apply(copy.deepcopy(settings), desired, owned)


def apply(settings, desired, owned):
    """Bring `settings` to the profile; returns the new ownership record.

    Only entries this installation added or changed are recorded. A value that
    was already as the profile wants stays someone else's. Entries owned by an
    earlier profile that this one does not want are taken back out."""
    created = list(owned.get('created', []))
    scalars = {}
    for key, value in desired['scalars'].items():
        present, current = _get(settings, key)
        record = owned.get('scalars', {}).get(key)
        if record is None:
            if present and current == value:continue
            record = {'had': present, 'previous': current}
        _set(settings, key, value, created)
        scalars[key] = dict(record, value=value)
    for key, record in owned.get('scalars', {}).items():
        if key not in scalars:_restore(settings, key, record)
    lists = {}
    for key, entries in desired['lists'].items():
        parent = _container(settings, key, created);name = key.split('.')[-1]
        if name not in parent:
            parent[name] = [];created.append(key)
        current = parent[name]
        if not isinstance(current, list):raise ValueError('claude_settings_not_a_list:' + key)
        mine = owned.get('lists', {}).get(key, [])
        kept = []
        for entry in entries:
            if entry in current:
                if entry in mine:kept.append(entry)
                continue
            current.append(entry);kept.append(entry)
        for entry in mine:
            if entry not in entries and entry in current:current.remove(entry)
        if kept:lists[key] = kept
    for key, mine in owned.get('lists', {}).items():
        if key in desired['lists']:continue
        present, current = _get(settings, key)
        if present and isinstance(current, list):
            for entry in mine:
                if entry in current:current.remove(entry)
    if 'hooks' not in settings:
        settings['hooks'] = {};created.append('hooks')
    hooks = settings['hooks']
    if not isinstance(hooks, dict):raise ValueError('claude_settings_hooks_not_an_object')
    if not isinstance(hooks.get('PreToolUse', []), list):raise ValueError('claude_settings_hooks_event_not_a_list:PreToolUse')
    previous = owned.get('hook_command')
    if previous and previous != desired['hook']['command']:_remove_hook(hooks, previous)
    if 'PreToolUse' not in hooks:
        hooks['PreToolUse'] = [];created.append('hooks.PreToolUse')
    groups = hooks['PreToolUse']
    wanted = hook_group(desired);found = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):continue
        for i, item in enumerate(group['hooks']):
            if isinstance(item, dict) and item.get('command') == desired['hook']['command']:
                group['hooks'][i] = wanted['hooks'][0];found = True
                if len(group['hooks']) == 1:group['matcher'] = wanted['matcher']
    if not found:groups.append(wanted)
    # Containers left empty by a profile switch go; the ones this profile fills stay.
    _prune(settings, [c for c in created if c not in desired['lists']])
    created = [c for c in dict.fromkeys(created) if _get(settings, c)[0]]
    return {'profile': desired['name'], 'scalars': scalars, 'lists': lists, 'created': created,
            'hook_command': desired['hook']['command']}


def remove(settings, owned):
    """Take out exactly what `owned` records; keep entries someone else changed."""
    for key, record in owned.get('scalars', {}).items():_restore(settings, key, record)
    for key, mine in owned.get('lists', {}).items():
        present, current = _get(settings, key)
        if present and isinstance(current, list):
            for entry in mine:
                if entry in current:current.remove(entry)
    hooks = settings.get('hooks')
    if isinstance(hooks, dict) and owned.get('hook_command'):_remove_hook(hooks, owned['hook_command'])
    _prune(settings, owned.get('created', []))


# ---- PreToolUse check ---------------------------------------------------------------

def _inside(path, root):
    return path == root or root in path.parents


def _real(path):
    return Path(os.path.realpath(path))


def _claude_dir(env):
    return _real(os.path.expanduser(env.get('CLAUDE_CONFIG_DIR') or '~/.claude'))


def _credential(relative):
    text = relative.as_posix()
    for pattern in CREDENTIAL_FILES:
        if pattern.endswith('/**'):
            if text.startswith(pattern[:-2]):return True
        elif fnmatch.fnmatchcase(text, pattern):return True
    return False


def blocked(path, roots, home, credentials=True):
    """Why `path` is off limits under the standard denylist, or None.

    A personal folder stays readable when the project (a root) lives inside it,
    so a repository under ~/Documents keeps working; a project that contains a
    personal folder (a session started in the home folder) does not open it."""
    for candidate in dict.fromkeys((Path(os.path.abspath(path)), _real(path))):
        for base in dict.fromkeys((Path(home), _real(home))):
            if not _inside(candidate, base) or candidate == base:continue
            relative = candidate.relative_to(base)
            if credentials and _credential(relative):return 'a credential file'
            for name in PERSONAL_DIRS:
                folder = base / name
                if _inside(candidate, folder) and not any(_inside(candidate, r) and _inside(r, folder) for r in roots):
                    return 'a personal folder (~/' + name + ')'
    return None


def _roots(event, env):
    cwd = event.get('cwd') if isinstance(event.get('cwd'), str) and event.get('cwd') else os.getcwd()
    roots = [_real(cwd)]
    if env.get('CLAUDE_PROJECT_DIR'):roots.append(_real(env['CLAUDE_PROJECT_DIR']))
    # Folders the person opens for one session by launching Claude Code with this set.
    roots += [_real(os.path.expanduser(p)) for p in env.get(ALLOW_ENV, '').split(os.pathsep) if p.strip()]
    return cwd, roots


GLOB = re.compile(r'[*?\[]')
PATH_TEXT = re.compile(r'(?:~|\$\{?HOME\}?|/)[^\s\'"`;|&<>()]*')


def _home_expand(text, home):
    text = re.sub(r'^(?:\$HOME|\$\{HOME\})(?=/|$)', lambda _: home, text)
    return home + text[1:] if text == '~' or text.startswith('~/') else text


def _targets(text, base, home):
    """The paths a word or pattern names. For a glob: its fixed prefix and each personal
    folder it could match, such as ~/Doc* or ~/D?wnloads."""
    path = os.path.normpath(os.path.join(base, _home_expand(text, home)))
    match = GLOB.search(path)
    if not match:return [path]
    found = [path[:match.start()]]
    parts = Path(path).parts
    for name in PERSONAL_DIRS:
        folder = Path(home, name).parts
        if len(parts) >= len(folder) and all(fnmatch.fnmatchcase(f, p) for f, p in zip(folder, parts)):
            found.append(str(Path(home, name)))
    return found


def _bash_paths(command, cwd, home):
    try:words = shlex.split(command, posix=True)
    except ValueError:words = command.split()
    found = []
    # Relative words may follow a `cd`; resolve them against every folder the command moves to.
    bases = [cwd];previous = None
    for word in words:
        if previous in ('cd', 'pushd') and not word.startswith('-'):
            # A bare `cd` (followed by an operator) goes to the home folder.
            target = home if word in ('&&', '||', ';', '|', '&') else _home_expand(word, home)
            bases.append(os.path.normpath(os.path.join(bases[-1], target)))
        previous = word
        pieces = [word] + word.split('=')[1:] + PATH_TEXT.findall(word)
        for piece in pieces:
            piece = piece.strip()
            if not piece or piece.startswith('-'):continue
            for base in bases:found += _targets(piece, base, home)
    return found


# Commands that read whole folder trees; for these, naming the home folder reaches the personal ones.
RECURSIVE_TOOLS = {'grep', 'egrep', 'rg', 'ag', 'ack', 'find', 'fd', 'tree', 'du', 'tar', 'zip', 'rsync'}
OPERATORS = {'&&', '||', ';', '|', '&'}


def _recursive(command):
    """Whether a command walks folder trees: a tree-walking tool, or -r/-R/--recursive (-R for ls)."""
    try:words = shlex.split(command, posix=True)
    except ValueError:words = command.split()
    name = None
    for word in words:
        if word in OPERATORS:
            name = None;continue
        if name is None:
            name = os.path.basename(word)
            if name in RECURSIVE_TOOLS:return True
            continue
        letters = word[1:] if word.startswith('-') and not word.startswith('--') else ''
        if word == '--recursive' or ('R' in letters) or ('r' in letters and name not in ('ls', 'sort', 'git')):
            return True
    return False


def _contains_personal(path, home):
    """The personal folder a search rooted at `path` would walk into, if any."""
    for candidate in dict.fromkeys((Path(os.path.normpath(path)), _real(path))):
        for base in dict.fromkeys((Path(home), _real(home))):
            for name in PERSONAL_DIRS:
                if _inside(base / name, candidate) and base / name != candidate:return name
    return None


def _decision(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': reason}}


def check(event, profile_name, env=None):
    """The hook's answer for one PreToolUse event: {} (no opinion) or deny."""
    if profile_name not in PROFILES:raise ValueError('invalid_guardrail_profile')
    env = os.environ if env is None else env
    tool = str(event.get('tool_name', ''))
    if tool not in FILE_TOOLS and tool != 'Bash':return {}
    values = event.get('tool_input') if isinstance(event.get('tool_input'), dict) else {}
    cwd, roots = _roots(event, env)
    home = os.path.expanduser('~')
    searched = []
    if tool == 'Bash':
        command = values.get('command')
        targets = _bash_paths(command, cwd, home) if isinstance(command, str) else []
        if isinstance(command, str) and _recursive(command):searched = targets
    else:
        targets = [v for k in ('file_path', 'notebook_path', 'filePath', 'path') for v in [values.get(k)]
                   if isinstance(v, str) and v]
        pattern = values.get('pattern') if tool == 'Glob' else None
        if isinstance(pattern, str) and pattern.startswith(('/', '~')):targets.append(pattern)
        if tool in ('Grep', 'Glob'):searched = [os.path.join(cwd, _home_expand(values.get('path') or cwd, home))]
        targets = [path for t in targets for path in _targets(t, cwd, home)]
    for target in searched:
        # A search from the home folder (or above) walks into the personal folders too.
        name = _contains_personal(target, home)
        if name:
            return _decision('Vaelius ' + profile_name + ' guardrails: searching ' + target + ' would read ~/' + name
                             + ', a personal folder agents in captured projects do not read. Search the project instead.')
    for target in targets:
        # In Bash, credential files are left to the Read deny rules: Claude Code applies them to
        # `cat`, `head`, `sed` and redirections, and `ssh -i ~/.ssh/id_x` must keep working.
        reason = blocked(target, roots, home, credentials=tool != 'Bash')
        if reason:
            return _decision('Vaelius ' + profile_name + ' guardrails: ' + target + ' is ' + reason
                             + ', which agents in captured projects do not read or change. If the person wants it '
                             'used, they can paste the content or start Claude Code with ' + ALLOW_ENV + ' set.')
    if profile_name == 'strict' and tool in EDIT_TOOLS:
        for target in targets:
            path = _real(target)
            if any(_inside(path, root) for root in roots):continue
            # Claude Code's own plan files and per-project auto memory live in its config folder.
            claude = _claude_dir(env)
            if _inside(path, claude / 'plans'):continue
            if _inside(path, claude / 'projects') and 'memory' in path.relative_to(claude / 'projects').parts[1:2]:continue
            return _decision('Vaelius strict guardrails: edits outside the project folder are not allowed.')
    return {}


# ---- Session status --------------------------------------------------------------

MANAGED_DIRS = {'darwin': '/Library/Application Support/ClaudeCode', 'linux': '/etc/claude-code'}


def _documents(cwd, env):
    """[(scope, settings)] for the settings files Claude Code reads, highest precedence first.

    MDM profiles and server-managed settings are not files, so they are not seen."""
    managed = MANAGED_DIRS.get(sys.platform)
    paths = []
    if managed:
        folder = Path(managed)
        paths.append(('managed', folder / 'managed-settings.json'))
        try:paths += [('managed', p) for p in sorted((folder / 'managed-settings.d').glob('*.json'))]
        except OSError:pass
    roots = []
    for root in (cwd, env.get('CLAUDE_PROJECT_DIR')):
        if root and Path(root) not in roots:roots.append(Path(root))
    try:
        top = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'], capture_output=True,
                             text=True, timeout=.5, check=True).stdout.strip()
        if top and Path(top) not in roots:roots.append(Path(top))
    except (OSError, subprocess.SubprocessError):
        pass
    paths += [('local', root / '.claude' / 'settings.local.json') for root in roots]
    paths += [('project', root / '.claude' / 'settings.json') for root in roots]
    paths.append(('user', _claude_dir(env) / 'settings.json'))
    found = []
    for scope, path in paths:
        try:value = json.loads(path.read_text())
        except (OSError, ValueError):continue
        if isinstance(value, dict):found.append((scope, value))
    return found


def state(cwd, env=None):
    """What the settings files on disk turn on: the guardrail hook, the read fence, the sandbox."""
    env = os.environ if env is None else env
    documents = _documents(cwd, env)
    hooked = any('guardrail-check' in json.dumps(doc.get('hooks', {}).get('PreToolUse', []))
                 for _, doc in documents if isinstance(doc.get('hooks'), dict))
    # A `true` in any file applies; for sandbox.enabled the highest-precedence file wins.
    fenced = any(_get(doc, 'permissions.blockReadsOutsideWorkingDirectories') == (True, True) for _, doc in documents)
    sandbox = next((value for _, doc in documents for present, value in [_get(doc, 'sandbox.enabled')]
                    if present and isinstance(value, bool)), False)
    return {'hook': hooked, 'reads_fenced': fenced, 'sandbox': sandbox}


def status_line(cwd, env=None):
    """One line for a captured project with no guardrails, else None."""
    try:value = state(cwd, env)
    except Exception:return None
    if value['hook'] or (value['reads_fenced'] and value['sandbox']):return None
    return ('Vaelius guardrails off in this captured project: Claude can read your personal folders and '
            'credential files. Turn them on with `vaelius-client install --claude-code` (standard profile).')
