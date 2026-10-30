"""Claude Code guardrail profiles: installer merge, ownership, removal, the PreToolUse check and status.

Fictional home folders and settings files in a temporary directory; no real
Claude Code configuration, home folder or service is read.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_claude_code import Profile, payload, Backend, PLUGIN

GUARDRAILS = PLUGIN / 'guardrails'


class GuardrailFixture(Profile):
    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name).resolve()
        self.claude = base / 'claude-config';self.claude.mkdir()
        self.settings = self.claude / 'settings.json'
        self.state = self.claude / '.claude.json'
        self.settings.write_text(json.dumps({'model': 'synthetic',
            'permissions': {'allow': ['Bash(ls)'], 'deny': ['Read(./.env)']},
            'sandbox': {'enabled': False, 'filesystem': {'allowWrite': ['/tmp/build']}},
            'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'guard'}]}]}},
            indent=2) + '\n')
        self.state.write_text(json.dumps({'projects': {str(self.project): {'mcpServers': {}}}}, indent=2) + '\n')
        self.original = {p: p.read_text() for p in (self.settings, self.state)}

    def install(self, **options):
        from agentclient.claude_code import install
        return install(self.home, claude_config_dir=self.claude, **options)

    def uninstall(self, **options):
        from agentclient.claude_code import uninstall
        return uninstall(self.home, **options)

    def read(self, path=None):
        return json.loads((path or self.settings).read_text())

    def receipt(self):
        return json.loads((self.home / 'claude-code-installation.json').read_text())


class ProfileTests(unittest.TestCase):
    def render(self, name):
        from agentclient.claude_guardrails import render
        return render(name, hook_command='vaelius-client guardrail-check --profile ' + name,
                      settings_file='/path/to/project/.claude/settings.local.json')

    def test_reference_files_match_the_installer(self):
        from agentclient.claude_guardrails import PROFILES
        for name in PROFILES:
            self.assertEqual(json.loads((GUARDRAILS / (name + '.json')).read_text()), self.render(name), name)
        managed = json.loads((GUARDRAILS / 'managed-settings.json').read_text())
        # The managed template also switches Claude Code auto memory off (agentclient.claude_memory).
        self.assertIs(managed.pop('autoMemoryEnabled'), False)
        self.assertEqual(managed, self.render('standard'))

    def test_standard_is_a_denylist_without_sandbox(self):
        from agentclient.claude_guardrails import MATCHER
        value = self.render('standard')
        self.assertEqual(set(value), {'permissions', 'hooks'})
        deny = value['permissions']['deny']
        for path in ('~/.aws/credentials', '~/.ssh/id_*', '~/.netrc', '~/.config/gh/hosts.yml', '~/.docker/config.json'):
            self.assertIn('Read(' + path + ')', deny);self.assertIn('Edit(' + path + ')', deny)
        # Tools read their own configuration: ~/.aws/config, ~/.ssh/config and ~/.gitconfig stay open.
        for path in ('~/.aws/config', '~/.ssh/config', '~/.gitconfig', '~/.aws/**', '~/.ssh/**'):
            self.assertNotIn('Read(' + path + ')', deny)
        self.assertNotIn('allow', value['permissions'])
        self.assertEqual(value['hooks']['PreToolUse'][0]['matcher'], MATCHER)
        # Letters and | only: Claude Code compares an exact list of tool names, never mcp__ tools.
        self.assertRegex(MATCHER, r'^[A-Za-z|]+$')
        self.assertEqual(set(MATCHER.split('|')), {'Read', 'Grep', 'Glob', 'LSP', 'Edit', 'Write', 'MultiEdit',
                                                   'NotebookEdit', 'Bash'})

    def test_agent_configuration_asks_and_agent_credentials_are_denied(self):
        for name in ('standard', 'strict'):
            permissions = self.render(name)['permissions']
            ask, deny = permissions['ask'], permissions['deny']
            for path in ('~/.claude/CLAUDE.md', '~/.claude/settings.json', '~/.claude/settings.local.json',
                         '~/.claude/skills/**', '~/.claude/agents/**', '~/.claude/commands/**', '~/.claude/hooks/**',
                         '~/.claude/output-styles/**', '~/.claude/reference/**', '~/.claude.json',
                         '~/.codex/config.toml', '~/.codex/hooks.json', '~/.codex/AGENTS.md'):
                self.assertIn('Edit(' + path + ')', ask, (name, path))
            # Plan mode writes ~/.claude/plans; no rule may cover it, and nothing broad.
            for rule in ask:
                self.assertTrue(rule.startswith('Edit(~/'), rule)
                self.assertNotIn('plans', rule);self.assertNotIn(rule, ('Edit(~/.claude/**)', 'Edit(~/.codex/**)'))
            self.assertFalse([r for r in ask + deny if r.startswith('Write(')])
            for path in ('~/.claude/.credentials.json', '~/.claude.json', '~/.codex/auth.json'):
                self.assertIn('Read(' + path + ')', deny, (name, path))
            self.assertNotIn('Edit(~/.claude.json)', deny)
        for template in ('managed-settings.json', 'managed-settings-strict.json'):
            permissions = json.loads((GUARDRAILS / template).read_text())['permissions']
            self.assertEqual(permissions['ask'], self.render('standard')['permissions']['ask'], template)
            self.assertIn('Read(~/.claude.json)', permissions['deny'], template)

    def test_strict_confines_to_the_repository(self):
        value = self.render('strict')
        self.assertIs(value['permissions']['blockReadsOutsideWorkingDirectories'], True)
        self.assertEqual({k: value['sandbox'][k] for k in ('enabled', 'failIfUnavailable', 'allowUnsandboxedCommands')},
                         {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False})
        self.assertIn('Edit(//path/to/project/.claude/settings.local.json)', value['permissions']['deny'])
        allowed = value['sandbox']['filesystem']['allowRead'] + value['sandbox']['filesystem']['allowWrite']
        for entry in allowed:
            # Specific cache folders only; never a parent that also holds credentials.
            self.assertNotIn(entry.rstrip('/'), ('~', '~/.cache', '~/.cargo', '~/.m2', '~/.gradle', '~/.aws', '~/.ssh', '~/.config'))
        self.assertTrue(set(value['sandbox']['filesystem']['allowWrite']) <= set(value['sandbox']['filesystem']['allowRead']))

    def test_no_profile_restricts_mcp_servers(self):
        from agentclient.claude_guardrails import PROFILES
        for name in PROFILES:
            text = json.dumps(self.render(name))
            self.assertNotIn('mcp__', text);self.assertNotIn('McpServers', text)

    def test_managed_template_locks_bypass_only_in_strict(self):
        strict = json.loads((GUARDRAILS / 'managed-settings-strict.json').read_text())
        self.assertEqual(strict['permissions']['disableBypassPermissionsMode'], 'disable')
        self.assertIs(strict['permissions']['blockReadsOutsideWorkingDirectories'], True)
        # allowManagedReadPathsOnly would stop the read fence from reaching sandboxed commands.
        self.assertNotIn('allowManagedReadPathsOnly', strict['sandbox']['filesystem'])
        for text in (GUARDRAILS / 'managed-settings.json').read_text(), json.dumps(strict):
            self.assertIn('vaelius-client guardrail-check', text)


class InstallerTests(GuardrailFixture):
    def test_standard_install_merges_and_is_idempotent(self):
        result = self.install(guardrails='standard', guardrails_only=True)
        self.assertEqual(result['guardrails'], 'standard')
        first = self.settings.read_text()
        self.install(guardrails='standard', guardrails_only=True)
        self.install(guardrails='default', guardrails_only=True)
        self.assertEqual(self.settings.read_text(), first)
        settings = self.read();before = json.loads(self.original[self.settings])
        self.assertEqual(settings['model'], 'synthetic')
        self.assertEqual(settings['permissions']['allow'], ['Bash(ls)'])
        self.assertEqual(settings['permissions']['deny'][0], 'Read(./.env)')
        self.assertIn('Read(~/.aws/credentials)', settings['permissions']['deny'])
        self.assertEqual(settings['sandbox'], before['sandbox'])
        self.assertEqual(settings['hooks']['PreToolUse'][0], before['hooks']['PreToolUse'][0])
        self.assertEqual(len(settings['hooks']['PreToolUse']), 2)
        # Guardrails-only touches neither the capture hooks nor the MCP registration.
        self.assertEqual(self.state.read_text(), self.original[self.state])
        self.assertEqual(self.receipt()['installs'], {})
        backups = list((self.home / 'claude-code-backups').iterdir())
        self.assertEqual([b.read_text() for b in backups], [self.original[self.settings]])

    def test_ask_rules_merge_with_the_persons_own_and_come_out_exactly(self):
        settings = self.read()
        settings['permissions']['ask'] = ['Bash(git push *)', 'Edit(~/.claude/CLAUDE.md)']
        self.settings.write_text(json.dumps(settings, indent=2) + '\n');original = self.settings.read_text()
        self.install(guardrails='standard', guardrails_only=True)
        ask = self.read()['permissions']['ask']
        self.assertEqual(ask[:2], ['Bash(git push *)', 'Edit(~/.claude/CLAUDE.md)'])
        self.assertIn('Edit(~/.codex/config.toml)', ask);self.assertEqual(len(ask), len(set(ask)))
        recorded = self.receipt()['guardrails']
        owned = next(iter(recorded.values()))['lists']['permissions.ask']
        # The person's identical rule stays theirs: it is not recorded, so removal keeps it.
        self.assertNotIn('Edit(~/.claude/CLAUDE.md)', owned)
        self.install(guardrails='standard', guardrails_only=True)
        self.assertEqual(self.read()['permissions']['ask'], ask)
        self.uninstall()
        self.assertEqual(self.settings.read_text(), original)

    def test_uninstall_restores_the_original_exactly(self):
        for profile in ('standard', 'strict'):
            self.install(guardrails=profile, guardrails_only=True)
            self.assertEqual(len(self.uninstall()), 1)
            self.assertEqual(self.settings.read_text(), self.original[self.settings], profile)
            self.assertNotIn('guardrails', self.receipt())

    def test_strict_records_and_restores_values_it_changed(self):
        self.install(guardrails='strict', guardrails_only=True)
        settings = self.read()
        self.assertIs(settings['sandbox']['enabled'], True)
        self.assertEqual(settings['sandbox']['filesystem']['allowWrite'][0], '/tmp/build')
        [record] = self.receipt()['guardrails'].values()
        self.assertEqual(record['scalars']['sandbox.enabled'], {'had': True, 'previous': False, 'value': True})
        self.uninstall()
        self.assertIs(self.read()['sandbox']['enabled'], False)

    def test_a_value_already_set_stays_with_its_owner(self):
        settings = self.read();settings['permissions']['blockReadsOutsideWorkingDirectories'] = True
        settings['permissions']['deny'].append('Read(~/.netrc)')
        self.settings.write_text(json.dumps(settings))
        self.install(guardrails='strict', guardrails_only=True)
        [record] = self.receipt()['guardrails'].values()
        self.assertNotIn('permissions.blockReadsOutsideWorkingDirectories', record['scalars'])
        self.assertNotIn('Read(~/.netrc)', record['lists']['permissions.deny'])
        self.assertEqual(self.read()['permissions']['deny'].count('Read(~/.netrc)'), 1)
        self.uninstall()
        after = self.read()
        self.assertIs(after['permissions']['blockReadsOutsideWorkingDirectories'], True)
        self.assertIn('Read(~/.netrc)', after['permissions']['deny'])

    def test_uninstall_keeps_what_someone_changed_after_install(self):
        self.install(guardrails='strict', guardrails_only=True)
        settings = self.read()
        settings['sandbox']['failIfUnavailable'] = False
        settings['permissions']['deny'].append('Read(~/private-notes/**)')
        settings['theme'] = 'dark'
        self.settings.write_text(json.dumps(settings))
        self.uninstall()
        after = self.read()
        self.assertIs(after['sandbox']['failIfUnavailable'], False)
        self.assertEqual(after['permissions']['deny'], ['Read(./.env)', 'Read(~/private-notes/**)'])
        self.assertEqual(after['theme'], 'dark')
        self.assertNotIn('guardrail-check', json.dumps(after))

    def test_switching_profiles_leaves_only_the_new_one(self):
        self.install(guardrails='strict', guardrails_only=True)
        self.install(guardrails='standard', guardrails_only=True)
        switched = self.settings.read_text()
        self.install(guardrails='standard', guardrails_only=True)
        self.assertEqual(self.settings.read_text(), switched)
        settings = json.loads(switched)
        self.assertNotIn('blockReadsOutsideWorkingDirectories', settings['permissions'])
        self.assertEqual(settings['sandbox'], json.loads(self.original[self.settings])['sandbox'])
        commands = [h['command'] for g in settings['hooks']['PreToolUse'] for h in g['hooks']]
        self.assertEqual(sum('guardrail-check' in c for c in commands), 1)
        self.assertTrue(any(c.endswith('--profile standard') for c in commands))
        self.assertFalse(any('settings.local.json' in r or './.claude/settings.json' in r for r in settings['permissions']['deny']))
        self.install(guardrails='none', guardrails_only=True)
        self.assertEqual(self.settings.read_text(), self.original[self.settings])

    def test_none_without_a_profile_touches_nothing(self):
        compact = json.dumps(json.loads(self.original[self.settings]))
        self.settings.write_text(compact)
        self.install(guardrails='none', guardrails_only=True)
        self.assertEqual(self.settings.read_text(), compact)
        missing = self.project / '.claude' / 'settings.local.json'
        self.install(scope='project', project_root=self.project, guardrails='none', guardrails_only=True)
        self.assertFalse(missing.exists())

    def test_default_keeps_the_installed_profile(self):
        self.install(guardrails='strict', guardrails_only=True)
        strict = self.settings.read_text()
        self.install(guardrails='default', guardrails_only=True)
        self.assertEqual(self.settings.read_text(), strict)

    def test_with_capture_hooks_and_partial_removal(self):
        from agentclient.claude_code import hook_command
        self.install(guardrails='standard')
        settings = self.read()
        self.assertIn(hook_command(self.home), json.dumps(settings))
        self.assertIn('guardrail-check', json.dumps(settings))
        self.assertIn('vaelius', self.read(self.state)['projects'][str(self.project)]['mcpServers'])
        self.uninstall(guardrails_only=True)
        settings = self.read()
        self.assertIn(hook_command(self.home), json.dumps(settings))
        self.assertNotIn('guardrail-check', json.dumps(settings))
        self.assertNotIn('Read(~/.aws/credentials)', settings['permissions']['deny'])
        self.uninstall()
        self.assertEqual(self.settings.read_text(), self.original[self.settings])
        self.assertEqual(self.read(self.state), json.loads(self.original[self.state]))

    def test_project_scope_files_are_created_and_removed_in_either_order(self):
        from agentclient.claude_code import settings_install_covers
        local = self.project / '.claude' / 'settings.local.json'
        for first, second in (({'guardrails': 'strict', 'guardrails_only': True}, {}),
                              ({}, {'guardrails': 'strict', 'guardrails_only': True})):
            self.install(scope='project', project_root=self.project, **first)
            self.install(scope='project', project_root=self.project, **second)
            settings = self.read(local)
            self.assertIn('Edit(' + '//' + str(local).lstrip('/') + ')', settings['permissions']['deny'])
            self.assertTrue(settings_install_covers(self.home, str(self.project)))
            self.uninstall(scope='project')
            self.assertFalse(local.exists())
        # Guardrails alone are not a capture installation.
        self.install(scope='project', project_root=self.other, guardrails='standard', guardrails_only=True)
        self.assertFalse(settings_install_covers(self.home, str(self.other)))
        self.assertTrue((self.other / '.claude' / 'settings.local.json').exists())
        self.uninstall(guardrails_only=True)
        self.assertFalse((self.other / '.claude').joinpath('settings.local.json').exists())

    def test_files_and_keys_created_by_one_record_go_with_the_last(self):
        settings = self.read();settings.pop('hooks')
        self.settings.write_text(json.dumps(settings, indent=2) + '\n')
        original = self.settings.read_text()
        self.install(guardrails='standard', guardrails_only=True)
        self.install()
        self.uninstall(guardrails_only=True)
        self.uninstall()
        self.assertEqual(self.settings.read_text(), original)
        local = self.project / '.claude' / 'settings.local.json'
        self.install(scope='project', project_root=self.project, guardrails='strict', guardrails_only=True)
        self.install(scope='project', project_root=self.project)
        self.uninstall(guardrails_only=True)
        self.assertTrue(local.exists())
        self.uninstall()
        self.assertFalse(local.exists())

    def test_refusals_change_nothing(self):
        broken = self.read();broken['sandbox'] = 'on'
        self.settings.write_text(json.dumps(broken))
        snapshot = self.settings.read_text()
        with self.assertRaises(ValueError):
            self.install(guardrails='strict', guardrails_only=True)
        with self.assertRaises(ValueError):
            self.install(guardrails='balanced', guardrails_only=True)
        with self.assertRaises(ValueError):
            self.install(guardrails_only=True)
        self.assertEqual(self.settings.read_text(), snapshot)
        self.assertFalse((self.home / 'claude-code-installation.json').exists())

    def test_cli_installs_standard_by_default_and_removes_it(self):
        from agentclient.cli import main
        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):main(['--home', str(self.home), *args])
            return json.loads(out.getvalue())
        self.assertEqual(run('install', '--claude-code', '--claude-config-dir', str(self.claude))['guardrails'], 'standard')
        self.assertIn('guardrail-check', self.settings.read_text())
        run('install', '--claude-code', '--guardrails', 'strict', '--claude-config-dir', str(self.claude))
        self.assertIs(self.read()['sandbox']['enabled'], True)
        self.assertEqual(run('uninstall', '--guardrails-only')['removed'][0]['guardrails'], 'strict')
        self.assertNotIn('guardrail-check', self.settings.read_text())
        run('install', '--claude-code', '--guardrails', 'none', '--claude-config-dir', str(self.claude))
        self.assertNotIn('guardrail-check', self.settings.read_text())
        run('uninstall', '--claude-code')
        self.assertEqual(self.settings.read_text(), self.original[self.settings])


class CheckTests(unittest.TestCase):
    """The PreToolUse check against a fictional home folder."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.homedir = base / 'home'
        for folder in ('Documents/work/repo', 'Downloads', 'code/repo', '.aws', '.ssh', '.claude/plans',
                       '.claude/projects/p/memory'):
            (self.homedir / folder).mkdir(parents=True)
        self.repo = self.homedir / 'code' / 'repo'
        self.docs_repo = self.homedir / 'Documents' / 'work' / 'repo'
        isolation = patch.dict(os.environ, {'HOME': str(self.homedir), 'CLAUDE_CONFIG_DIR': str(self.homedir / '.claude')})
        isolation.start();self.addCleanup(isolation.stop)
        for name in ('CLAUDE_PROJECT_DIR', 'VAELIUS_GUARDRAIL_ALLOW'):os.environ.pop(name, None)

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, tool, profile='standard', cwd=None, **values):
        from agentclient.claude_guardrails import check
        event = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': values, 'cwd': str(cwd or self.repo)}
        return (check(event, profile).get('hookSpecificOutput') or {}).get('permissionDecision')

    def test_personal_folders_are_refused_to_file_tools(self):
        h = self.homedir
        for profile in ('standard', 'strict'):
            self.assertEqual(self.decide('Read', profile, file_path=str(h / 'Documents/notes.txt')), 'deny')
            self.assertEqual(self.decide('Read', profile, file_path='~/Downloads/statement.pdf'), 'deny')
            self.assertEqual(self.decide('Edit', profile, file_path=str(h / 'Desktop/todo.md')), 'deny')
            self.assertEqual(self.decide('Grep', profile, path=str(h / 'Library/Mail')), 'deny')
            self.assertEqual(self.decide('Glob', profile, pattern='~/Pictures/**/*.jpg'), 'deny')
            self.assertEqual(self.decide('Glob', profile, pattern='~/Doc*/**/*.md'), 'deny')
            self.assertEqual(self.decide('Glob', profile, path=str(h), pattern='**/*.pdf'), 'deny')
            self.assertEqual(self.decide('Grep', profile, path='~', pattern='password'), 'deny')
            self.assertEqual(self.decide('Grep', profile, cwd=h, pattern='password'), 'deny')
            self.assertIsNone(self.decide('Grep', profile, pattern='password'))
            self.assertEqual(self.decide('Read', profile, file_path=str(h / 'Library/Mobile Documents/x.txt')), 'deny')
            self.assertEqual(self.decide('Read', profile, file_path='../../Documents/notes.txt'), 'deny')
            self.assertEqual(self.decide('Read', profile, file_path=str(h / '.aws/credentials')), 'deny')
            self.assertEqual(self.decide('Read', profile, file_path=str(h / '.ssh/id_ed25519')), 'deny')
            self.assertIsNone(self.decide('Read', profile, file_path=str(self.repo / 'src/app.py')))

    def test_tool_configuration_stays_readable_under_standard(self):
        for path in ('.aws/config', '.ssh/config', '.ssh/known_hosts', '.gitconfig', '.config/gh/config.yml'):
            self.assertIsNone(self.decide('Read', file_path=str(self.homedir / path)), path)

    def test_project_inside_a_personal_folder_keeps_working(self):
        inside = str(self.docs_repo / 'README.md')
        self.assertIsNone(self.decide('Read', cwd=self.docs_repo, file_path=inside))
        self.assertIsNone(self.decide('Bash', cwd=self.docs_repo, command='cat README.md && ls src/'))
        self.assertEqual(self.decide('Read', cwd=self.docs_repo, file_path=str(self.homedir / 'Documents/taxes.pdf')), 'deny')
        # A session started in the home folder contains Documents but does not open it.
        self.assertEqual(self.decide('Bash', cwd=self.homedir, command='cd Documents && ls'), 'deny')

    def test_bash_reads_of_personal_folders_are_refused(self):
        h = str(self.homedir)
        (self.homedir / 'Documents/notes.txt').write_text('x');(self.homedir / 'Downloads/a.csv').write_text('x')
        for command in ('cat ~/Documents/notes.txt', 'head -5 "$HOME/Downloads/report.csv"',
                        'ls ${HOME}/Desktop', 'grep -r secret ~/Documents', 'tar czf /tmp/x.tgz ~/Pictures',
                        'cp ' + h + '/Documents/a.txt .', 'ls ~/Documents/*.pdf',
                        "python3 -c \"print(open('" + h + "/Downloads/x').read())\"", 'cat --file=~/Music/list',
                        'cat ~/Doc*/notes.txt', 'cd ~ && cat D?wnloads/*', 'cd && cat Documents/notes.txt',
                        'cd ~/code && cat ../Downloads/a.csv', 'ls ~/Desk*', 'cat ~/[DM]usic/list',
                        'grep -rn token ~', 'rg token ~/', 'find ~ -name "*.pdf"', 'ls -R ~', 'du -sh ~'):
            self.assertEqual(self.decide('Bash', command=command), 'deny', command)

    def test_normal_engineering_commands_are_untouched(self):
        for command in ('aws s3 ls', 'aws sts get-caller-identity --profile dev', 'git status', 'git push origin main',
                        'gh pr list', 'npm ci', 'uv sync', 'cdk diff', 'ssh -i ~/.ssh/id_ed25519 git@example.invalid',
                        'cat ~/.aws/config', 'ls ~', 'ls -la ~', 'curl https://example.invalid/a/b', "sed -i 's/a/b/' src/x.py",
                        'grep -rn TODO .', 'find . -name "*.py"', 'rg token src', 'git rm -r build', 'ls -ltr ~',
                        'sort -r results.txt'):
            self.assertIsNone(self.decide('Bash', command=command), command)
        self.assertIsNone(self.decide('WebFetch', url='https://example.invalid'))
        self.assertIsNone(self.decide('mcp__github__get_issue', issue=1))

    def test_strict_refuses_edits_outside_the_project_but_not_claude_own_files(self):
        h = self.homedir
        self.assertEqual(self.decide('Write', 'strict', file_path='/tmp/elsewhere.txt'), 'deny')
        self.assertEqual(self.decide('Edit', 'strict', file_path=str(h / '.zshrc')), 'deny')
        self.assertEqual(self.decide('NotebookEdit', 'strict', notebook_path=str(h / 'code/other/n.ipynb')), 'deny')
        self.assertEqual(self.decide('Edit', 'strict', file_path=str(h / '.claude/settings.json')), 'deny')
        self.assertIsNone(self.decide('Write', 'strict', file_path=str(self.repo / 'notes/new.md')))
        self.assertIsNone(self.decide('Write', 'strict', file_path=str(h / '.claude/plans/plan.md')))
        self.assertIsNone(self.decide('Write', 'strict', file_path=str(h / '.claude/projects/p/memory/MEMORY.md')))
        self.assertIsNone(self.decide('Write', 'standard', file_path='/tmp/elsewhere.txt'))

    def test_bash_writes_to_agent_configuration_ask(self):
        h = str(self.homedir)
        (self.homedir / '.claude/skills/s').mkdir(parents=True);(self.homedir / '.claude/agents').mkdir()
        for command in ('echo "- prefer uv" >> ~/.claude/CLAUDE.md', 'echo x > $HOME/.claude/settings.json',
                        'printf x | tee -a ~/.claude/settings.local.json', "sed -i '' 's/a/b/' ~/.claude/skills/s/SKILL.md",
                        'perl -pi -e s/a/b/ ' + h + '/.claude/hooks/h.sh', 'cp reviewer.md ~/.claude/agents/',
                        'cp reviewer.md -t ~/.claude/commands', 'mv new.toml ~/.codex/config.toml',
                        'cd ~/.codex && cat > hooks.json', 'cat <<EOF > ~/.claude/output-styles/x.md\nhi\nEOF',
                        'bash -c "echo x >> ~/.codex/AGENTS.md"', 'ln -sf /tmp/x ~/.claude/reference/r.md',
                        'rm ~/.claude/CLAUDE.md', 'mv ~/.claude/CLAUDE.md /tmp/old.md',
                        "python3 -c \"open('" + h + "/.claude/CLAUDE.md', 'a').write('x')\""):
            self.assertEqual(self.decide('Bash', command=command), 'ask', command)
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.homedir / 'moved');os.environ['CODEX_HOME'] = str(self.homedir / 'cx')
        self.addCleanup(os.environ.pop, 'CODEX_HOME', None)
        self.assertEqual(self.decide('Bash', command='echo x >> ~/moved/CLAUDE.md'), 'ask')
        self.assertEqual(self.decide('Bash', command='echo x >> ~/cx/config.toml'), 'ask')
        # Reads, plan files, the project's own files and other targets are left alone.
        for command in ('cat ~/.claude/CLAUDE.md', 'ls ~/.claude/skills', 'grep -n model ~/.claude/settings.json',
                        'cp ~/.claude/CLAUDE.md /tmp/copy.md', 'echo plan > ~/.claude/plans/p.md', 'echo x > notes.md',
                        'cat ~/.codex/config.toml | head', 'diff ~/.claude/agents/a.md agents/a.md'):
            self.assertIsNone(self.decide('Bash', command=command), command)
        # File tools are left to the profile's ask rules, so there is one prompt, not two.
        self.assertIsNone(self.decide('Edit', file_path=h + '/.claude/CLAUDE.md'))

    def test_agent_credential_files_are_never_read(self):
        h = str(self.homedir)
        os.environ['CODEX_HOME'] = h + '/cx';self.addCleanup(os.environ.pop, 'CODEX_HOME', None)
        for tool, values in (('Read', {'file_path': h + '/.claude.json'}), ('Read', {'file_path': '~/.claude/.credentials.json'}),
                             ('Read', {'file_path': h + '/.codex/auth.json'}), ('Read', {'file_path': h + '/cx/auth.json'}),
                             ('Grep', {'path': h + '/.claude', 'pattern': 'token'}), ('Edit', {'file_path': h + '/.claude.json'}),
                             ('Bash', {'command': 'jq .mcpServers ~/.claude.json'}), ('Bash', {'command': 'cat ~/.codex/auth.json'}),
                             ('Bash', {'command': 'grep -r token ~/.claude'}), ('Bash', {'command': 'cp ~/.claude.json x.json'}),
                             ('Bash', {'command': 'echo {} > ~/.claude.json'})):
            for profile in ('standard', 'strict'):
                self.assertEqual(self.decide(tool, profile, **values), 'deny', (tool, values))
        # VAELIUS_GUARDRAIL_ALLOW never opens them.
        os.environ['VAELIUS_GUARDRAIL_ALLOW'] = h
        self.assertEqual(self.decide('Read', file_path=h + '/.claude.json'), 'deny')
        self.assertIsNone(self.decide('Read', file_path=h + '/.claude/CLAUDE.md'))
        self.assertIsNone(self.decide('Grep', path=h + '/.claude/skills', pattern='x'))

    def test_symlink_out_of_the_project_is_followed(self):
        (self.homedir / 'Documents/diary.txt').write_text('x')
        (self.repo / 'link.txt').symlink_to(self.homedir / 'Documents/diary.txt')
        self.assertEqual(self.decide('Read', file_path=str(self.repo / 'link.txt')), 'deny')
        (self.repo / 'out').symlink_to('/tmp')
        self.assertEqual(self.decide('Write', 'strict', file_path=str(self.repo / 'out/x.txt')), 'deny')

    def test_person_can_open_a_folder_for_one_session(self):
        target = str(self.homedir / 'Downloads/spec.pdf')
        self.assertEqual(self.decide('Read', file_path=target), 'deny')
        os.environ['VAELIUS_GUARDRAIL_ALLOW'] = str(self.homedir / 'Downloads')
        self.assertIsNone(self.decide('Read', file_path=target))
        # It never opens credential files.
        os.environ['VAELIUS_GUARDRAIL_ALLOW'] = str(self.homedir)
        self.assertEqual(self.decide('Read', file_path=str(self.homedir / '.aws/credentials')), 'deny')

    def test_command_line_check(self):
        from agentclient.claude_code import guardrail_command
        def run(profile, stdin):
            return subprocess.run(guardrail_command(Path(self.tmp.name) / 'profile', profile), shell=True, input=stdin,
                                  capture_output=True, text=True, timeout=60, cwd=self.repo)
        event = {'tool_name': 'Read', 'tool_input': {'file_path': str(self.homedir / 'Documents/a.txt')}, 'cwd': str(self.repo)}
        result = run('standard', json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(json.loads(run('strict', json.dumps(dict(event, tool_name='Bash', tool_input={'command': 'ls'}))).stdout), {})
        # A check that cannot run: strict blocks the call (exit 2), standard reports it and lets work go on.
        self.assertEqual(run('strict', 'not json').returncode, 2)
        self.assertEqual(run('standard', 'not json').returncode, 1)
        # It needs no profile folder and creates none.
        self.assertFalse((Path(self.tmp.name) / 'profile').exists())

    def test_project_modules_are_never_imported(self):
        from agentclient.claude_code import guardrail_command
        planted = self.repo / 'agentclient';planted.mkdir()
        (planted / '__init__.py').write_text('raise SystemExit(7)\n')
        result = subprocess.run(guardrail_command(Path(self.tmp.name) / 'profile', 'standard'), shell=True, cwd=self.repo,
                                input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {}))


class StatusLineTests(Profile):
    def start(self, **config):
        self.config.update(config);self.save()
        return self.hook('SessionStart', Backend()).get('systemMessage', '')

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True);path.write_text(json.dumps(value))

    def test_captured_project_without_guardrails_says_so_once(self):
        message = self.start()
        lines = [l for l in message.split('\n') if 'guardrails off' in l]
        self.assertEqual(len(lines), 1)
        self.assertIn('vaelius-client install --claude-code', lines[0])
        # Never model-facing.
        result = self.hook('SessionStart', Backend())
        self.assertNotIn('guardrails', json.dumps(result.get('hookSpecificOutput', {})))
        self.assertNotIn('guardrails off', self.hook('UserPromptSubmit', Backend()).get('systemMessage', ''))

    def test_installed_profile_silences_it(self):
        from agentclient.claude_code import install
        install(self.home, scope='project', project_root=self.project, guardrails='standard', guardrails_only=True)
        self.assertNotIn('guardrails off', self.start())

    def test_user_settings_and_an_equivalent_fence_count(self):
        self.write(self.claude_home / 'settings.json', {'hooks': {'PreToolUse': [{'matcher': 'Read', 'hooks': [
            {'type': 'command', 'command': '/x/python -m agentclient.cli guardrail-check --profile strict'}]}]}})
        self.assertNotIn('guardrails off', self.start())
        self.write(self.claude_home / 'settings.json', {'permissions': {'blockReadsOutsideWorkingDirectories': True},
                                                        'sandbox': {'enabled': True}})
        self.assertNotIn('guardrails off', self.start())
        # A higher-precedence file that turns the sandbox off wins.
        self.write(self.project / '.claude' / 'settings.local.json', {'sandbox': {'enabled': False}})
        self.assertIn('guardrails off', self.start())

    def test_quiet_when_not_captured_or_turned_off(self):
        self.assertNotIn('guardrails off', self.start(claude_code_guardrail_notice=False))
        self.config.pop('claude_code_guardrail_notice')
        self.assertEqual(self.hook('SessionStart', Backend(), cwd=self.other), {})
        self.assertNotIn('guardrails off', self.start(paused=True))

    def test_codex_never_gets_the_line(self):
        from agentclient.hooks import handle
        event = payload('SessionStart', self.project, source='startup')
        with patch('agentclient.transport.enterprise_client', return_value=Backend()):
            result = handle(self.home, event, host='codex')
        self.assertNotIn('guardrails', result.get('systemMessage', ''))


if __name__ == '__main__':
    unittest.main()
