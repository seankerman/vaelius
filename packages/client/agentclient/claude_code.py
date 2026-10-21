"""Claude Code integration without the plugin: owned settings hooks and MCP entries.

`install` adds this profile's hook handlers to a Claude Code settings file and a
`vaelius` HTTP MCP server, bound to one enrolled project, to Claude Code's
`.claude.json`. Only entries this profile installed are ever changed or removed;
every other key, hook and server is kept byte-for-byte in meaning. The first
modification of each file keeps a private backup, and a receipt records exactly
what was written so `uninstall` can take it out again.

Scopes:
  user     hooks in <config dir>/settings.json, active in every folder.
  project  hooks in <root>/.claude/settings.local.json (per user, not committed).

The MCP connection must name a project, so it is registered per enrolled
project root (Claude Code "local" scope, `projects[<root>].mcpServers`) for
both scopes. Re-run `install` after enrolling another project.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

SERVER = 'vaelius'
RECEIPT = 'claude-code-installation.json'
# Events the hook handles; the rest of Claude Code's events are not registered.
EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PostToolUseFailure',
          'Stop', 'StopFailure', 'PostCompact', 'SessionEnd')
# Claude Code allows SessionEnd 1.5 s unless a settings hook asks for more.
TIMEOUTS = {'SessionEnd': 5}
STATUS_EVENTS = ('SessionStart', 'UserPromptSubmit')


def config_dir(explicit=None):
    if explicit:return Path(explicit).expanduser().resolve()
    if os.environ.get('CLAUDE_CONFIG_DIR'):return Path(os.environ['CLAUDE_CONFIG_DIR']).expanduser().resolve()
    return (Path.home()/'.claude').resolve()


def claude_json(directory):
    """Claude Code keeps MCP and per-project state in ~/.claude.json, or inside
    the configuration directory when CLAUDE_CONFIG_DIR moves it."""
    default=(Path.home()/'.claude').resolve()
    return Path.home()/'.claude.json' if Path(directory)==default else Path(directory)/'.claude.json'


def _python_command(home, *args):
    return ' '.join(shlex.quote(x) for x in (sys.executable, '-m', 'agentclient.cli', '--home', str(home), *args))


def hook_command(home):
    return _python_command(home, 'hook', '--host', 'claude-code')


def mcp_entry(home, endpoint, project):
    return {'type': 'http', 'url': endpoint + '/mcp',
            'headersHelper': _python_command(home, 'mcp-headers', '--project', project)}


def _handler(event, command):
    handler = {'type': 'command', 'command': command, 'timeout': TIMEOUTS.get(event, 10)}
    if event in STATUS_EVENTS:handler['statusMessage'] = 'Vaelius memory'
    return handler


def _read_json(path):
    if not path.exists():return None
    text = path.read_text()
    value = json.loads(text) if text.strip() else {}
    if not isinstance(value, dict):raise ValueError('claude_settings_not_an_object:' + path.name)
    return value


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
        json.dump(value, stream, indent=2);stream.write('\n');temporary = Path(stream.name)
    temporary.chmod(mode)
    temporary.replace(path)


def _backup(home, path, backups):
    """Keep the file as it was before this profile first changed it."""
    key = str(path)
    if key in backups:return
    folder = home / 'claude-code-backups';folder.mkdir(mode=0o700, exist_ok=True)
    if path.exists():
        target = folder / (hashlib.sha256(key.encode()).hexdigest()[:16] + '-' + path.name)
        if not target.exists():
            shutil.copy2(path, target);target.chmod(0o600)
        backups[key] = str(target)
    else:
        backups[key] = None


def _remove_command(hooks, command):
    """Drop handlers whose command is exactly `command`; keep everything else."""
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                kept.append(group);continue
            remaining = [h for h in group['hooks'] if not (isinstance(h, dict) and h.get('command') == command)]
            if remaining or len(remaining) == len(group['hooks']):
                kept.append(dict(group, hooks=remaining))
        if kept or not groups:hooks[event] = kept
        else:hooks.pop(event)


def _add_hooks(settings, command):
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):raise ValueError('claude_settings_hooks_not_an_object')
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):raise ValueError('claude_settings_hooks_event_not_a_list:' + event)
        handler = _handler(event, command);found = False
        for group in groups:
            for i, item in enumerate(group.get('hooks', []) if isinstance(group, dict) else []):
                if isinstance(item, dict) and item.get('command') == command:
                    group['hooks'][i] = handler;found = True
        if not found:groups.append({'hooks': [handler]})


def _receipt(home):
    path = home / RECEIPT
    return json.loads(path.read_text()) if path.exists() else {'installs': {}}


def _save_receipt(home, receipt):
    path = home / RECEIPT
    with tempfile.NamedTemporaryFile(mode='w', dir=home, prefix='claude-code-', suffix='.tmp', delete=False) as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True);stream.write('\n');temporary = Path(stream.name)
    temporary.chmod(0o600);temporary.replace(path)


def _project_roots(home, config, scope, root):
    """{root: project} that get an MCP entry: enrolled roots and scope-file enrollments.

    Project scope needs a root that is enrolled, or holds a repository scope file
    naming its project, and is not switched off there."""
    from agentclient.hooks import resolve_scope
    from agentclient.scope_file import enrollments
    if scope == 'project':
        found = resolve_scope(config, {'cwd': str(root)})
        if not found or found['capture'] == 'off' or found['origin'] not in ('enrolled_root', 'enrolled_worktree', 'scope_file'):
            raise ValueError('project_root_not_enrolled: run vaelius-client enroll-project or add a .vaelius scope file')
        return {str(root): found['project']}
    roots = {str(Path(r).resolve()): v['project'] for r, v in enrollments(home).items()
             if isinstance(v, dict) and isinstance(v.get('project'), str)}
    roots.update({str(Path(r).resolve()): p for r, p in config.get('projects', {}).items()})
    return roots


def install(home, *, scope='user', claude_config_dir=None, project_root=None):
    """Add or refresh this profile's Claude Code hooks and MCP entries; idempotent."""
    from agentclient.local_state import private_dir
    from agentclient.transport import require_backend, backend_endpoint
    if scope not in ('user', 'project'):raise ValueError('invalid_claude_code_scope')
    home = private_dir(home)
    config = json.loads((home / 'config.json').read_text())
    endpoint = backend_endpoint(require_backend(config))
    directory = config_dir(claude_config_dir)
    root = Path(project_root or os.getcwd()).expanduser().resolve(strict=True) if scope == 'project' else None
    settings_path = (root / '.claude' / 'settings.local.json') if root else directory / 'settings.json'
    mcp_path = claude_json(directory)
    roots = _project_roots(home, config, scope, root)
    command = hook_command(home)
    with (home / 'claude-code.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = _receipt(home)
        key = scope + ':' + str(settings_path)
        owned = receipt['installs'].get(key, {})
        backups = dict(owned.get('backups', {}))
        # Validate everything before the first write, so a refusal changes nothing.
        settings = _read_json(settings_path)
        created_settings = owned.get('created_settings', settings is None)
        settings = {} if settings is None else settings
        created_hooks = owned.get('created_hooks_key', 'hooks' not in settings)
        state = _read_json(mcp_path) or {}
        projects = state.get('projects', {})
        if not isinstance(projects, dict):raise ValueError('claude_json_projects_not_an_object')
        previous = owned.get('mcp', {})
        entries = {}
        for path, project in roots.items():
            entry = mcp_entry(home, endpoint, project)
            current = ((projects.get(path) or {}).get('mcpServers') or {}).get(SERVER)
            if current is not None and current != entry and current != previous.get(path):
                raise ValueError('existing_MCP_registration_not_owned_by_this_installation:' + path)
            entries[path] = entry
        _backup(home, settings_path, backups);_backup(home, mcp_path, backups)
        # An interpreter upgrade changes the command; drop only the recorded old one.
        if owned.get('command') and owned['command'] != command and isinstance(settings.get('hooks'), dict):
            _remove_command(settings['hooks'], owned['command'])
        _add_hooks(settings, command)
        _write_json(settings_path, settings)
        # Re-read immediately before writing: Claude Code updates this file itself.
        state = _read_json(mcp_path) or {}
        projects = state.setdefault('projects', {})
        for path in set(previous) - set(entries):
            servers = (projects.get(path) or {}).get('mcpServers') or {}
            if servers.get(SERVER) == previous[path]:servers.pop(SERVER)
        for path, entry in entries.items():
            project_state = projects.setdefault(path, {})
            project_state.setdefault('mcpServers', {})[SERVER] = entry
        _write_json(mcp_path, state)
        receipt['installs'][key] = {'scope': scope, 'settings_file': str(settings_path), 'command': command,
            'mcp_file': str(mcp_path), 'mcp': entries, 'backups': backups, 'project_root': str(root) if root else None,
            'created_settings': created_settings, 'created_hooks_key': created_hooks}
        _save_receipt(home, receipt)
    return {'host': 'claude-code', 'scope': scope, 'settings_file': str(settings_path), 'mcp_file': str(mcp_path),
            'mcp_projects': sorted(roots.values()),
            'events': list(EVENTS), 'status': 'installed_restart_claude_code'}


def uninstall(home, *, scope=None):
    """Remove only what this profile installed; returns the installs removed."""
    home = Path(home)
    if not (home / RECEIPT).exists():return []
    removed = []
    with (home / 'claude-code.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = _receipt(home)
        for key, owned in list(receipt['installs'].items()):
            if scope and owned['scope'] != scope:continue
            settings_path = Path(owned['settings_file'])
            settings = _read_json(settings_path)
            if settings is not None:
                hooks = settings.get('hooks')
                if isinstance(hooks, dict):
                    _remove_command(hooks, owned['command'])
                    if not hooks and owned.get('created_hooks_key'):settings.pop('hooks')
                if not settings and owned.get('created_settings'):settings_path.unlink()
                else:_write_json(settings_path, settings)
            mcp_path = Path(owned['mcp_file'])
            state = _read_json(mcp_path)
            if state is not None:
                for path, entry in owned.get('mcp', {}).items():
                    servers = ((state.get('projects') or {}).get(path) or {}).get('mcpServers')
                    if isinstance(servers, dict) and servers.get(SERVER) == entry:servers.pop(SERVER)
                _write_json(mcp_path, state)
            receipt['installs'].pop(key)
            removed.append({'scope': owned['scope'], 'settings_file': str(settings_path), 'mcp_file': str(mcp_path)})
        _save_receipt(home, receipt)
    return removed


def settings_install_covers(home, cwd):
    """True when a settings install of this profile already runs hooks in `cwd`."""
    path = Path(home) / RECEIPT
    if not path.exists():return False
    try:
        installs = json.loads(path.read_text()).get('installs', {})
        folder = Path(cwd).resolve() if isinstance(cwd, str) and cwd else None
    except (OSError, ValueError):
        return False
    for owned in installs.values():
        if owned.get('scope') == 'user':return True
        root = owned.get('project_root')
        if root and folder and (folder == Path(root) or Path(root) in folder.parents):return True
    return False
