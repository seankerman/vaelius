"""Installed host commands run the installed client, never modules from the project folder.

Claude Code and Codex run hook and header-helper commands through a shell with
the project folder as the working directory. Each test plants an `agentclient`
package in that folder whose modules write a marker file, runs the installed
command string the way the host does, and checks the marker is never written and
the real client answered. Synthetic profile, fictional service URL, temporary
host configuration folders; no real Claude Code or Codex settings are read.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tokenize
import tomllib
import unittest
from unittest.mock import patch

import agentclient

PLUGIN_WRAPPER = Path(__file__).resolve().parents[3] / 'integrations/claude-code/scripts/vaelius-claude-code'
SERVICE = 'http://127.0.0.1:65530'
PLANTED = 'import os\nopen({marker!r}, "a").write("{name}\\n")\nprint(\'{{"planted": "{name}"}}\')\n'


def start_event(cwd):
    return json.dumps({'session_id': 'isolation-session', 'cwd': str(cwd), 'hook_event_name': 'SessionStart',
                       'source': 'startup', 'transcript_path': '/fictional/session.jsonl'})


class Isolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / 'profile';self.home.mkdir(mode=0o700)
        self.project = base / 'cloned-repo';self.project.mkdir()
        self.claude = base / 'claude-config';self.claude.mkdir()
        self.codex = base / 'codex-home';self.codex.mkdir()
        self.user_home = base / 'user-home';self.user_home.mkdir()
        self.marker = base / 'planted-ran'
        package = self.project / 'agentclient';package.mkdir()
        (package / '__init__.py').write_text('')
        for name in ('cli', 'mcp_headers'):
            (package / (name + '.py')).write_text(PLANTED.format(marker=str(self.marker), name=name))
        token = self.home / 'credential';token.write_text('synthetic-access-token\n');token.chmod(0o600)
        config = {'paused': False, 'projects': {str(self.project): 'demo'}, 'sessions': {},
                  'knowledge_backend': {'mode': 'enterprise_local', 'api_version': 'cloud-local-1',
                      'capture_version': 'enterprise-local-2', 'capture_owner': 'transcript', 'url': SERVICE,
                      'credential_file': str(token), 'connection_id': 'isolation-synthetic'}}
        (self.home / 'config.json').write_text(json.dumps(config));(self.home / 'config.json').chmod(0o600)
        # The SessionStart guardrail check reads Claude Code settings: only the temporary ones.
        for item in (patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude)}),
                     patch('agentclient.claude_guardrails.MANAGED_DIRS', {})):
            item.start();self.addCleanup(item.stop)
        self.env = dict(os.environ, HOME=str(self.user_home), CLAUDE_CONFIG_DIR=str(self.claude),
                        CLAUDE_PROJECT_DIR=str(self.project), CLAUDE_CODE_MCP_SERVER_URL=SERVICE + '/mcp')
        for name in ('PYTHONSAFEPATH', 'VAELIUS_CLIENT', 'VAELIUS_CLIENT_HOME'):self.env.pop(name, None)

    def run_host(self, command, stdin=''):
        """As Claude Code and Codex do: a shell command run in the project folder."""
        return subprocess.run(command, shell=True, cwd=self.project, env=self.env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def assert_real_client(self, run):
        self.assertFalse(self.marker.exists(), 'project module ran: ' + (self.marker.read_text() if self.marker.exists() else ''))
        self.assertNotIn('planted', run.stdout)
        return run

    def assert_real_hook(self, command):
        run = self.assert_real_client(self.run_host(command, start_event(self.project)))
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        # The real client reports the enrolled project's capture status; the service is unreachable.
        self.assertIn('Vaelius', result.get('systemMessage', ''))
        self.assertNotIn('older client', result['systemMessage'])
        return result

    def assert_real_headers(self, command):
        run = self.assert_real_client(self.run_host(command))
        self.assertEqual(run.returncode, 0, run.stderr)
        headers = json.loads(run.stdout)
        self.assertEqual(headers['Authorization'], 'Bearer synthetic-access-token')
        self.assertEqual(headers['X-AgentNetwork-Project'], 'demo')

    def claude_install(self, **options):
        from agentclient.claude_code import install
        return install(self.home, claude_config_dir=self.claude, **options)

    def claude_commands(self, settings_file):
        settings = json.loads(Path(settings_file).read_text())
        hooks = [h['command'] for groups in settings['hooks'].values() for g in groups for h in g['hooks']]
        state = json.loads((self.claude / '.claude.json').read_text())
        helper = state['projects'][str(self.project)]['mcpServers']['vaelius']['headersHelper']
        return hooks, helper

    def codex_commands(self):
        hooks = json.loads((self.codex / 'hooks.json').read_text())['hooks']
        commands = {h['command'] for groups in hooks.values() for g in groups for h in g['hooks']}
        helper = tomllib.loads((self.codex / 'config.toml').read_text())['mcp_servers']['agentnetwork_memory']['http_headers_helper']
        return commands, helper


class PlantedPackageTests(Isolation):
    def test_planted_package_runs_without_the_flag(self):
        # Control: without -P the planted package does replace the client, so the tests below can fail.
        run = self.run_host(shlex.join([sys.executable, '-m', 'agentclient.cli', '--home', str(self.home), 'hook']),
                            start_event(self.project))
        self.assertEqual(json.loads(run.stdout), {'planted': 'cli'})
        self.assertTrue(self.marker.exists())

    def test_claude_code_user_scope_hook_and_header_helper(self):
        self.claude_install(scope='user')
        hooks, helper = self.claude_commands(self.claude / 'settings.json')
        from agentclient.claude_code import hook_command
        self.assertIn(hook_command(self.home), hooks)
        for command in set(hooks):
            if 'guardrail-check' not in command:self.assert_real_hook(command)
        self.assert_real_headers(helper)

    def test_claude_code_project_scope_hook_and_header_helper(self):
        self.claude_install(scope='project', project_root=self.project)
        hooks, helper = self.claude_commands(self.project / '.claude' / 'settings.local.json')
        for command in set(hooks):
            if 'guardrail-check' not in command:self.assert_real_hook(command)
        self.assert_real_headers(helper)

    def test_claude_code_guardrail_check(self):
        self.claude_install(scope='user', guardrails='standard')
        hooks, _ = self.claude_commands(self.claude / 'settings.json')
        [command] = {c for c in hooks if 'guardrail-check' in c}
        event = json.dumps({'hook_event_name': 'PreToolUse', 'cwd': str(self.project), 'tool_name': 'Read',
                            'tool_input': {'file_path': str(self.project / 'README.md')}})
        run = self.assert_real_client(self.run_host(command, event))
        self.assertEqual(run.returncode, 0, run.stderr)
        json.loads(run.stdout)

    def test_codex_hook_and_header_helper(self):
        from agentclient.install import install
        install(self.home, self.codex, {}, {})
        commands, helper = self.codex_commands()
        [command] = commands
        self.assert_real_hook(command)
        self.assert_real_headers(helper)

    def test_plugin_wrapper_runs_the_console_script(self):
        # The plugin runs the `vaelius-client` console script, whose sys.path[0] is its own
        # folder, not the working directory. Use the installed script beside this
        # interpreter, else a launcher shaped exactly like the one pip writes.
        launcher = Path(sys.executable).parent / 'vaelius-client'
        if not launcher.exists():
            launcher = Path(self.tmp.name) / 'bin' / 'vaelius-client';launcher.parent.mkdir()
            launcher.write_text('#!' + sys.executable + '\nimport re\nimport sys\nfrom agentclient.cli import vaelius_main\n'
                                "if __name__ == '__main__':\n    sys.exit(vaelius_main())\n")
            launcher.chmod(0o755)
        self.env.update(VAELIUS_CLIENT=str(launcher), VAELIUS_CLIENT_HOME=str(self.home))
        wrapper = shlex.quote(str(PLUGIN_WRAPPER))
        self.assert_real_hook(wrapper + ' hook')
        self.assert_real_headers(wrapper + ' mcp-headers --project-dir ' + shlex.quote(str(self.project)))


class GeneratedCommandTests(Isolation):
    def assert_isolated(self, command):
        parts = shlex.split(command)
        self.assertEqual(parts[0], sys.executable)
        self.assertEqual(parts[1:3], ['-P', '-m'], command)

    def test_every_installed_python_command_carries_the_flag(self):
        from agentclient.install import install
        self.claude_install(scope='user', guardrails='strict')
        hooks, helper = self.claude_commands(self.claude / 'settings.json')
        self.claude_install(scope='project', project_root=self.project)
        project_hooks, _ = self.claude_commands(self.project / '.claude' / 'settings.local.json')
        install(self.home, self.codex, {}, {})
        codex, codex_helper = self.codex_commands()
        commands = [c for c in hooks + project_hooks + list(codex) if 'agentclient' in c] + [helper, codex_helper]
        self.assertGreaterEqual(len(set(commands)), 5)
        for command in commands:self.assert_isolated(command)

    def test_only_the_shared_builder_spells_module_flag(self):
        # A new command builder must use host_command.module_command, not its own '-m'.
        found = []
        for path in sorted(Path(agentclient.__file__).parent.glob('*.py')):
            with path.open('rb') as stream:
                for token in tokenize.tokenize(stream.readline):
                    if token.type == tokenize.STRING and token.string.strip('\'"') in ('-m', ' -m '):
                        found.append(path.name)
        self.assertEqual(sorted(set(found)), ['host_command.py'])


class MigrationTests(Isolation):
    """Commands written before the flag: re-running install upgrades only owned entries."""
    def legacy(self):
        return patch('agentclient.host_command.ISOLATION', ())

    def test_claude_code_reinstall_upgrades_owned_entries_only(self):
        settings = self.claude / 'settings.json'
        settings.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'other-tool stop'}]}]}}))
        for scope, options in (('user', {}), ('project', {'project_root': self.project})):
            with self.legacy():self.claude_install(scope=scope, guardrails='none', **options)
        receipt = json.loads((self.home / 'claude-code-installation.json').read_text())
        self.assertTrue(all(' -m ' in r['command'] and ' -P ' not in r['command'] for r in receipt['installs'].values()))
        for scope, options in (('user', {}), ('project', {'project_root': self.project})):
            self.claude_install(scope=scope, guardrails='none', **options)
        for path in (settings, self.project / '.claude' / 'settings.local.json'):
            hooks, helper = self.claude_commands(path)
            ours = [c for c in hooks if 'agentclient' in c]
            self.assertTrue(ours)
            for command in ours + [helper]:self.assertEqual(shlex.split(command)[1:3], ['-P', '-m'])
        self.assertIn('other-tool stop', self.claude_commands(settings)[0])
        receipt = json.loads((self.home / 'claude-code-installation.json').read_text())
        self.assertTrue(all(' -P -m ' in r['command'] for r in receipt['installs'].values()))

    def test_codex_reinstall_upgrades_hook_and_helper(self):
        from agentclient.install import install
        unrelated = {'type': 'command', 'command': 'unrelated-hook', 'timeout': 9}
        (self.codex / 'hooks.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [unrelated]}]}}))
        with self.legacy():install(self.home, self.codex, {}, {})
        commands, helper = self.codex_commands()
        [old] = commands - {'unrelated-hook'}
        self.assertNotIn('-P', shlex.split(old) + shlex.split(helper))
        install(self.home, self.codex, {}, {})
        commands, helper = self.codex_commands()
        self.assertEqual(commands - {'unrelated-hook'}, {shlex.join([sys.executable, '-P', '-m', 'agentclient.cli', '--home', str(self.home), 'hook'])})
        self.assertIn('unrelated-hook', commands)
        self.assertEqual(shlex.split(helper)[1:3], ['-P', '-m'])
        # A later interpreter change still replaces the (now flagged) recorded command.
        with patch('agentclient.host_command.sys.executable', '/new/python'):install(self.home, self.codex, {}, {})
        commands, _ = self.codex_commands()
        self.assertEqual(commands - {'unrelated-hook'}, {shlex.join(['/new/python', '-P', '-m', 'agentclient.cli', '--home', str(self.home), 'hook'])})

    def run_hook(self, command, kind='SessionStart', cwd=None):
        event = json.loads(start_event(self.other))
        event['hook_event_name'] = kind
        return subprocess.run(command, shell=True, cwd=cwd or self.other, env=self.env, input=json.dumps(event),
                              capture_output=True, text=True, timeout=60)

    def setUp(self):
        super().setUp()
        self.other = Path(self.tmp.name).resolve() / 'unrelated';self.other.mkdir()

    def test_legacy_hook_tells_the_person_to_reinstall(self):
        from agentclient.install import install
        with self.legacy():
            self.claude_install(scope='user', guardrails='none')
            install(self.home, self.codex, {}, {})
        hooks, _ = self.claude_commands(self.claude / 'settings.json')
        [claude] = set(hooks)
        [codex] = self.codex_commands()[0]
        for command, how in ((claude, 'install --claude-code'), (codex, 'install --codex-home')):
            start = json.loads(self.run_hook(command).stdout)
            self.assertIn('older client', start['systemMessage'])
            self.assertIn(shlex.join(['vaelius-client', '--home', str(self.home)]) + ' ' + how, start['systemMessage'])
            # Once per session, never as model context.
            self.assertEqual(json.loads(self.run_hook(command, 'Stop').stdout), {})
            self.assertNotIn('hookSpecificOutput', start)
        # Project scope names its root.
        with self.legacy():self.claude_install(scope='project', project_root=self.project, guardrails='none')
        message = json.loads(self.run_hook(claude).stdout)['systemMessage']
        self.assertIn('--scope project --project-root ' + shlex.quote(str(self.project)), message)

    def test_upgraded_or_uninstalled_hook_is_quiet(self):
        self.claude_install(scope='user', guardrails='none')
        [command] = set(self.claude_commands(self.claude / 'settings.json')[0])
        self.assertEqual(json.loads(self.run_hook(command).stdout), {})
        # Run by hand without the flag but with no legacy install: nothing to fix, so quiet.
        bare = shlex.join([sys.executable, '-m', 'agentclient.cli', '--home', str(self.home), 'hook', '--host', 'claude-code'])
        self.assertEqual(json.loads(self.run_hook(bare).stdout), {})


if __name__ == '__main__':
    unittest.main()
