"""Python commands that installers write into agent host settings files.

Claude Code and Codex run hook and header-helper commands with the project
folder as the working directory. Started as `python -m`, Python would put that
folder first on `sys.path`, so a project containing an `agentclient` package
would run in place of this client, with this profile's credentials. `-P` keeps
the working directory off `sys.path`. Every command an installer writes is built
by `module_command`, so none can leave the flag out.

`-I` and `-s` are not used: they drop user site-packages, where a
`pip install --user` client lives, so the command would stop importing it.
Environment variables reach the hook from the host, not from project files.
"""
import json
from pathlib import Path
import shlex
import sys

ISOLATION = ('-P',)


def module_command(module, *args):
    """Shell command running `module` from the installed client only."""
    return shlex.join((sys.executable, *written_forms(module, *args)[0]))


def written_forms(module, *args):
    """Arguments after the interpreter that installers have written: current, then pre-`-P`."""
    tail = ['-m', module, *args]
    return [*ISOLATION, *tail], tail


def lacks_isolation(command):
    """Whether a written `python -m` command misses the isolation flags."""
    try:parts = shlex.split(command) if isinstance(command, str) else []
    except ValueError:return False
    if '-m' not in parts[1:]:return False
    return not set(ISOLATION) <= set(parts[1:parts.index('-m', 1)])


def started_without_isolation():
    """True when this process is `python -m ...` with the working directory on sys.path."""
    if sys.flags.safe_path:return False
    for arg in getattr(sys, 'orig_argv', [])[1:]:
        if arg == '-m':return True
        if not arg.startswith('-'):return False
    return False


def _reinstall_commands(home, host):
    """`vaelius-client` commands that rewrite this profile's unisolated installs for `host`."""
    home = Path(home);prefix = ['vaelius-client', '--home', str(home), 'install']
    found = []
    try:
        if host == 'claude-code' and (home / 'claude-code-installation.json').exists():
            installs = json.loads((home / 'claude-code-installation.json').read_text()).get('installs', {})
            for owned in installs.values():
                if not lacks_isolation(owned.get('command')):continue
                extra = ['--scope', 'project', '--project-root', owned['project_root']] if owned.get('scope') == 'project' else []
                found.append(shlex.join(prefix + ['--claude-code', *extra]))
        elif host == 'codex' and (home / 'installation.json').exists():
            owned = json.loads((home / 'installation.json').read_text())
            if lacks_isolation(owned.get('command')):
                found.append(shlex.join(prefix + ['--codex-home', str(Path(owned['hooks_file']).parent)]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    return sorted(set(found))


def upgrade_notice(home, host, event):
    """One SessionStart line for the person when this hook was installed without `-P`.

    Only the legitimate client can say this: if a project folder already shadows
    the client, its own code runs instead and nothing is shown."""
    if not isinstance(event, dict) or event.get('hook_event_name') != 'SessionStart':return None
    if not started_without_isolation():return None
    commands = _reinstall_commands(home, host)
    if not commands:return None
    return ('Vaelius: this hook was installed by an older client that lets a project folder replace the '
            'client\'s Python modules. Re-run `' + '` and `'.join(commands) + '` to update it.')
