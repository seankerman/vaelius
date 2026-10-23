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

`guardrails` writes a least-privilege profile (`claude_guardrails`) into the
same settings file: 'standard' or 'strict', 'default' (the profile already
installed there, else 'standard'), 'none' (take it out) or None (leave it as it
is). `guardrails_only` writes just the profile, for people who capture with the
plugin. The profile has its own receipt entry, so it can be switched or removed
without touching the hooks.
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


def guardrail_command(home, profile):
    # -P: the hook runs in the project folder; never import modules from there.
    return ' '.join(shlex.quote(x) for x in (sys.executable, '-P', '-m', 'agentclient.cli', '--home', str(home),
                                              'guardrail-check', '--profile', profile))


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


def _created_by_us(receipt, path, flag):
    """Whether any record of this profile created `path` (or its hooks key)."""
    records = list(receipt.get('installs', {}).values()) + list(receipt.get('guardrails', {}).values())
    return any(r.get('settings_file') == str(path) and r.get(flag) for r in records)


def install(home, *, scope='user', claude_config_dir=None, project_root=None, guardrails=None, guardrails_only=False):
    """Add or refresh this profile's Claude Code hooks, MCP entries and guardrails; idempotent."""
    from agentclient import claude_guardrails
    from agentclient.local_state import private_dir
    from agentclient.transport import require_backend, backend_endpoint
    if scope not in ('user', 'project'):raise ValueError('invalid_claude_code_scope')
    if guardrails not in (None, 'default', 'none') + claude_guardrails.PROFILES:raise ValueError('invalid_guardrail_profile')
    if guardrails_only and guardrails is None:raise ValueError('guardrails_only_needs_a_profile')
    home = private_dir(home)
    config = json.loads((home / 'config.json').read_text())
    endpoint = backend_endpoint(require_backend(config))
    directory = config_dir(claude_config_dir)
    root = Path(project_root or os.getcwd()).expanduser().resolve(strict=True) if scope == 'project' else None
    settings_path = (root / '.claude' / 'settings.local.json') if root else directory / 'settings.json'
    mcp_path = claude_json(directory)
    roots = {} if guardrails_only else _project_roots(home, config, scope, root)
    command = hook_command(home)
    with (home / 'claude-code.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = _receipt(home)
        receipt.setdefault('guardrails', {})
        key = scope + ':' + str(settings_path)
        owned = receipt['installs'].get(key, {})
        guarded = receipt['guardrails'].get(key, {})
        backups = dict(owned.get('backups', {}))
        guard_backups = dict(guarded.get('backups', {}))
        # Validate everything before the first write, so a refusal changes nothing.
        settings = _read_json(settings_path)
        settings_absent = settings is None
        settings = {} if settings is None else settings
        hooks_absent = 'hooks' not in settings
        desired = None;removing = False
        if guardrails == 'default':guardrails = guarded.get('profile', claude_guardrails.DEFAULT_PROFILE)
        if guardrails in claude_guardrails.PROFILES:
            desired = claude_guardrails.profile(guardrails, hook_command=guardrail_command(home, guardrails),
                                                settings_file=settings_path)
            claude_guardrails.validate(settings, desired, guarded)
        elif guardrails == 'none' and guarded:
            removing = True
            claude_guardrails.remove(settings, guarded)
            hooks = settings.get('hooks')
            if isinstance(hooks, dict) and not hooks and guarded.get('created_hooks_key'):settings.pop('hooks')
        if not guardrails_only:
            created_settings = owned.get('created_settings', settings_absent or _created_by_us(receipt, settings_path, 'created_settings'))
            created_hooks = owned.get('created_hooks_key', hooks_absent or _created_by_us(receipt, settings_path, 'created_hooks_key'))
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
        if desired:
            _backup(home, settings_path, guard_backups)
            record = claude_guardrails.apply(settings, desired, guarded)
            record.update({'scope': scope, 'settings_file': str(settings_path), 'project_root': str(root) if root else None,
                'backups': guard_backups,
                # The other record of this file is removed after this one, so it settles
                # whether a file or hooks key it created goes.
                'created_settings': guarded.get('created_settings', settings_absent),
                'created_hooks_key': guarded.get('created_hooks_key', hooks_absent)})
        # `--guardrails none --guardrails-only` with nothing installed leaves the file untouched.
        if desired or removing or not guardrails_only:_write_json(settings_path, settings)
        if not guardrails_only:
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
        if desired:receipt['guardrails'][key] = record
        elif guardrails == 'none':receipt['guardrails'].pop(key, None)
        if not receipt['guardrails']:receipt.pop('guardrails')
        _save_receipt(home, receipt)
    result = {'host': 'claude-code', 'scope': scope, 'settings_file': str(settings_path)}
    if not guardrails_only:
        result.update({'mcp_file': str(mcp_path), 'mcp_projects': sorted(roots.values()), 'events': list(EVENTS)})
    if guardrails is not None:result['guardrails'] = guardrails
    result['status'] = 'installed_restart_claude_code'
    return result


def _strip_settings(settings_path, removers):
    """Apply each remover to the settings file; delete the file only if it is ours and empty."""
    settings = _read_json(settings_path)
    if settings is None:return
    created = False
    for remover, owned in removers:
        remover(settings, owned)
        hooks = settings.get('hooks')
        if isinstance(hooks, dict) and not hooks and owned.get('created_hooks_key'):settings.pop('hooks')
        created = created or bool(owned.get('created_settings'))
    if not settings and created:settings_path.unlink()
    else:_write_json(settings_path, settings)


def _remove_install_hooks(settings, owned):
    hooks = settings.get('hooks')
    if isinstance(hooks, dict):_remove_command(hooks, owned['command'])


def uninstall(home, *, scope=None, guardrails_only=False):
    """Remove only what this profile installed; returns the installs removed.

    Guardrails go with everything else unless `guardrails_only`, which keeps
    the hooks and MCP entries and removes just the profile."""
    from agentclient import claude_guardrails
    home = Path(home)
    if not (home / RECEIPT).exists():return []
    removed = []
    with (home / 'claude-code.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = _receipt(home)
        guards = receipt.get('guardrails', {})
        keys = set(guards) | (set() if guardrails_only else set(receipt['installs']))
        for key in sorted(keys):
            owned = None if guardrails_only else receipt['installs'].get(key)
            guarded = guards.get(key)
            record = owned or guarded
            if scope and record['scope'] != scope:continue
            settings_path = Path(record['settings_file'])
            # Guardrails first, so an empty hooks key is dropped by whichever record created it.
            removers = [(claude_guardrails.remove, guarded)] if guarded else []
            if owned:removers.append((_remove_install_hooks, owned))
            _strip_settings(settings_path, removers)
            entry = {'scope': record['scope'], 'settings_file': str(settings_path)}
            if guarded:
                guards.pop(key);entry['guardrails'] = guarded.get('profile')
            if owned:
                mcp_path = Path(owned['mcp_file'])
                state = _read_json(mcp_path)
                if state is not None:
                    for path, value in owned.get('mcp', {}).items():
                        servers = ((state.get('projects') or {}).get(path) or {}).get('mcpServers')
                        if isinstance(servers, dict) and servers.get(SERVER) == value:servers.pop(SERVER)
                    _write_json(mcp_path, state)
                receipt['installs'].pop(key);entry['mcp_file'] = str(mcp_path)
            removed.append(entry)
        if 'guardrails' in receipt and not receipt['guardrails']:receipt.pop('guardrails')
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
