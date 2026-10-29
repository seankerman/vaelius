"""Origin labels and session taint: what a captured event touched restricts its sharing.

Table-driven over realistic Claude Code and Codex hook payloads and Codex rollout
records, against a synthetic home directory with a project repository, another
project, a sibling checkout of the same project and personal files outside.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agentclient import origin
from agentclient.general_contract import validate_event, validate_origin_label

SCOPE = 'project = "demo"\nsharing = "team"\n'


class Tree:
    """A synthetic home folder with the current project (`demo`), another checkout of it,
    other projects (team, private, capture off), a repository without a scope file,
    personal folders, credentials, tool caches, and a temporary scratch folder."""

    def __init__(self, test):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.repo = self.home / 'src/repo'
        self.scratch = self.tmp / 'scratch'
        for path in (self.repo / '.git', self.repo / 'src', self.home / 'Documents', self.home / '.ssh',
                     self.home / 'src/repo-copy/.git', self.home / 'src/other/.git', self.home / 'src/plain/.git',
                     self.home / 'src/quiet/.git', self.home / 'src/off/.git', self.repo / 'vendor/nested/.git',
                     self.home / 'shared-fixtures', self.scratch):
            path.mkdir(parents=True, exist_ok=True)
        (self.repo / '.vaelius').write_text(SCOPE)
        (self.home / 'src/repo-copy/.vaelius').write_text(SCOPE)
        (self.home / 'src/other/.vaelius').write_text('project = "other"\nsharing = "team"\n')
        (self.home / 'src/quiet/.vaelius').write_text('project = "quiet"\n')
        (self.home / 'src/off/.vaelius').write_text('project = "off-project"\ncapture = "off"\n')
        (self.repo / 'vendor/nested/.vaelius').write_text('project = "nested"\n')
        for path in (self.repo / 'README.md', self.repo / 'src/app.py', self.home / 'notes.txt', self.home / 'n.ipynb',
                     self.home / 'Documents/plan.md', self.home / '.ssh/config', self.home / 'src/other/x.py',
                     self.home / 'src/plain/y.py', self.home / 'src/quiet/q.py', self.home / 'src/off/o.py',
                     self.home / 'src/repo-copy/z.py', self.repo / 'vendor/nested/n.py',
                     self.home / 'shared-fixtures/f.csv', self.scratch / 'out.json', self.scratch / 'deploy.pem',
                     self.home / '.cache/pip/http/x', self.home / 'venvs/app/lib/python3.12/site-packages/m.py',
                     self.home / 'tools/node_modules/pkg/index.js', self.home / '.claude/plugins/p/skills/s.md',
                     self.home / '.aws/credentials'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic\n')
        os.symlink(self.home / 'notes.txt', self.repo / 'notes-link.md')
        os.symlink(self.home / 'Documents', self.repo / 'docs-link')
        os.symlink(self.repo / 'README.md', self.home / 'readme-alias.md')
        test.enterContext(mock.patch.dict(os.environ, {'HOME': str(self.home)}))

    def scope(self, **extra):
        return dict({'project': 'demo', 'root': str(self.repo), 'scope_file': str(self.repo / '.vaelius')}, **extra)

    def context(self, **extra):
        return origin.Context(self.scope(**extra), str(self.repo / 'src'), {})


def claude(tool, tool_input, response=None, cwd=None, kind='PostToolUse'):
    return {'hook_event_name': kind, 'session_id': 's1', 'tool_name': tool, 'tool_input': tool_input,
            'tool_response': {} if response is None else response, 'tool_use_id': 'toolu_1',
            'permission_mode': 'default', 'transcript_path': '/synthetic/transcript.jsonl', **({'cwd': cwd} if cwd else {})}


class ClassifierTableTest(unittest.TestCase):
    """Each row: description, event, expected ceiling, expected reason (None when unrestricted)."""

    def setUp(self):
        self.tree = Tree(self)

    def rows(self):
        t = self.tree
        repo, home, tmp = str(t.repo), str(t.home), str(t.scratch)
        src = repo + '/src'
        return [
            # Claude Code file tools.
            ('read in repo', claude('Read', {'file_path': repo + '/README.md'}, cwd=src), 'organization', None, []),
            ('read relative to cwd', claude('Read', {'file_path': 'app.py'}, cwd=src), 'organization', None, []),
            ('read home file', claude('Read', {'file_path': home + '/notes.txt'}, cwd=src), 'private', 'outside_repo', []),
            ('read ../.. escape', claude('Read', {'file_path': src + '/../../../notes.txt'}, cwd=src), 'private', 'outside_repo', []),
            ('read symlink into home', claude('Read', {'file_path': repo + '/notes-link.md'}, cwd=src), 'private', 'outside_repo', []),
            ('read through dir symlink', claude('Read', {'file_path': repo + '/docs-link/plan.md'}, cwd=src),
             'private', 'personal_content', []),
            ('read home alias of repo file', claude('Read', {'file_path': home + '/readme-alias.md'}, cwd=src), 'organization', None, []),
            ('read tilde path', claude('Read', {'file_path': '~/notes.txt'}, cwd=src), 'private', 'outside_repo', []),
            ('read other user home', claude('Read', {'file_path': '~someone/notes.txt'}, cwd=src), 'private', 'outside_repo', []),
            ('read missing home file', claude('Read', {'file_path': home + '/missing.txt'}, cwd=src), 'organization', None, []),
            ('read missing file in repo', claude('Read', {'file_path': repo + '/new/file.py'}, cwd=src), 'organization', None, []),
            # Allowed locations: temp, tool caches, packages, plugin folders, read-only system paths.
            ('read temp scratch', claude('Read', {'file_path': tmp + '/out.json'}, cwd=src), 'organization', None, []),
            ('read tool cache', claude('Read', {'file_path': home + '/.cache/pip/http/x'}, cwd=src), 'organization', None, []),
            ('read site-packages', claude('Read', {'file_path': home + '/venvs/app/lib/python3.12/site-packages/m.py'}, cwd=src),
             'organization', None, []),
            ('read node_modules', claude('Read', {'file_path': home + '/tools/node_modules/pkg/index.js'}, cwd=src),
             'organization', None, []),
            ('read claude plugin', claude('Read', {'file_path': home + '/.claude/plugins/p/skills/s.md'}, cwd=src),
             'organization', None, []),
            ('read system header', claude('Read', {'file_path': '/usr/include/stdio.h'}, cwd=src), 'organization', None, []),
            ('read /etc', claude('Read', {'file_path': '/etc/hosts'}, cwd=src), 'organization', None, []),
            # Always private: personal folders and credentials, even in temp or under an allowlist.
            ('read Documents', claude('Read', {'file_path': home + '/Documents/plan.md'}, cwd=src), 'private', 'personal_content', []),
            ('write Downloads (new file)', claude('Write', {'file_path': home + '/Downloads/out.md', 'content': 'x'}, cwd=src),
             'private', 'personal_content', []),
            ('read iCloud drive', claude('Read', {'file_path': home + '/Library/Mobile Documents/com~apple~CloudDocs/a.txt'}, cwd=src),
             'private', 'personal_content', []),
            ('read Messages', claude('Read', {'file_path': home + '/Library/Messages/chat.db'}, cwd=src), 'private', 'personal_content', []),
            ('read ssh config', claude('Read', {'file_path': home + '/.ssh/config'}, cwd=src, kind='PostToolUseFailure'),
             'private', 'credential_file', []),
            ('read aws credentials', claude('Read', {'file_path': '~/.aws/credentials'}, cwd=src), 'private', 'credential_file', []),
            ('read key in temp', claude('Read', {'file_path': tmp + '/deploy.pem'}, cwd=src), 'private', 'credential_file', []),
            # Other repositories.
            ('read other project', claude('Read', {'file_path': home + '/src/other/x.py'}, cwd=src), 'team', 'other_project', ['other']),
            ('read private other project', claude('Read', {'file_path': home + '/src/quiet/q.py'}, cwd=src),
             'private', 'other_project', ['quiet']),
            ('read unscoped repo', claude('Read', {'file_path': home + '/src/plain/y.py'}, cwd=src), 'private', 'other_project', []),
            ('read repo whose scope is off', claude('Read', {'file_path': home + '/src/off/o.py'}, cwd=src), 'private', 'other_project', []),
            ('read sibling checkout same project', claude('Read', {'file_path': home + '/src/repo-copy/z.py'}, cwd=src),
             'organization', None, []),
            ('read nested other-project repo', claude('Read', {'file_path': repo + '/vendor/nested/n.py'}, cwd=src),
             'private', 'other_project', ['nested']),
            ('edit in repo', claude('Edit', {'file_path': repo + '/src/app.py', 'old_string': 'a', 'new_string': 'b'}, cwd=src),
             'organization', None, []),
            ('notebook outside', claude('NotebookEdit', {'notebook_path': home + '/n.ipynb', 'new_source': ''}, cwd=src),
             'private', 'outside_repo', []),
            ('glob in repo', claude('Glob', {'pattern': '**/*.py'}, {'filenames': [src + '/app.py']}, cwd=src), 'organization', None, []),
            ('glob absolute outside', claude('Glob', {'pattern': home + '/Documents/**/*.md'}, cwd=src), 'private', 'personal_content', []),
            ('glob result through symlink', claude('Glob', {'pattern': '**/*.md', 'path': repo},
                                                   {'filenames': [repo + '/docs-link/plan.md']}, cwd=src),
             'private', 'personal_content', []),
            ('grep with path outside', claude('Grep', {'pattern': 'token', 'path': home}, cwd=src), 'private', 'outside_repo', []),
            ('grep in repo', claude('Grep', {'pattern': 'def ', 'path': repo, 'glob': '*.py'}, cwd=src), 'organization', None, []),
            # Web.
            ('web fetch public', claude('WebFetch', {'url': 'https://docs.example.org/page', 'prompt': 'p'}, cwd=src), 'organization', None, []),
            ('web search', claude('WebSearch', {'query': 'q'}, cwd=src), 'organization', None, []),
            ('web fetch localhost', claude('WebFetch', {'url': 'http://localhost:3000/admin', 'prompt': 'p'}, cwd=src),
             'private', 'private_network', []),
            ('web fetch metadata ip', claude('WebFetch', {'url': 'http://169.254.169.254/latest', 'prompt': 'p'}, cwd=src),
             'private', 'private_network', []),
            ('web fetch intranet name', claude('WebFetch', {'url': 'https://wiki.corp/page', 'prompt': 'p'}, cwd=src),
             'private', 'private_network', []),
            ('web fetch file url', claude('WebFetch', {'url': 'file://' + home + '/notes.txt', 'prompt': 'p'}, cwd=src),
             'private', 'outside_repo', []),
            # MCP.
            ('mcp mail', claude('mcp__mail__search_messages', {'query': 'x'}, cwd=src), 'private', 'mcp_server', []),
            ('mcp plugin chat', claude('mcp__plugin_team_chat__read_channel', {}, cwd=src), 'private', 'mcp_server', []),
            ('mcp resource read', claude('ReadMcpResourceTool', {'server': 'docs', 'uri': 'x://1'}, cwd=src), 'private', 'mcp_server', []),
            ('vaelius memory, same-project team item', claude('mcp__vaelius__memory_search', {'query': 'x'},
                [{'type': 'text', 'text': json.dumps({'results': [{'id': 'd1', 'provenance': {'author': 'a'},
                                                                   'origin': {'project': 'demo', 'sharing': 'team'}}]})}],
                cwd=src), 'team', 'recalled_memory', []),
            ('vaelius memory, other-project team item', claude('mcp__vaelius__memory_search', {'query': 'x'},
                {'results': [{'id': 'd4', 'provenance': {}, 'origin': {'project': 'other', 'sharing': 'team'}}]},
                cwd=src), 'team', 'recalled_memory', ['other']),
            ('vaelius memory, private item', claude('mcp__vaelius__memory_search', {'query': 'x'},
                {'results': [{'id': 'd2', 'provenance': {}, 'origin': {'project': 'secret-proj', 'sharing': 'private'}}]},
                cwd=src), 'private', 'recalled_memory', []),
            ('vaelius memory, organization item', claude('mcp__vaelius__memory_search', {'query': 'x'},
                {'results': [{'id': 'd3', 'provenance': {}, 'origin': {'project': 'x', 'sharing': 'organization'}}]},
                cwd=src), 'organization', None, []),
            # Neutral and unknown tools.
            ('todo', claude('TodoWrite', {'todos': []}, cwd=src), 'organization', None, []),
            ('subagent', claude('Task', {'prompt': 'p', 'subagent_type': 'general'}, {'content': 'summary'}, cwd=src),
             'organization', None, []),
            ('unknown tool', claude('FrobnicateTool', {'x': 1}, cwd=src), 'private', 'unknown_tool', []),
            # Shell: only the paths a command names matter.
            ('bash in repo', claude('Bash', {'command': 'ls -la && git status && pytest -q src'}, cwd=src), 'organization', None, []),
            ('bash cat $HOME', claude('Bash', {'command': 'cat $HOME/notes.txt'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('bash cat ${HOME}', claude('Bash', {'command': 'cat "${HOME}/Documents/plan.md"'}, cwd=src), 'private', 'personal_content', []),
            ('bash cd ~ && cat', claude('Bash', {'command': 'cd ~ && cat notes.txt'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('bash bare cd', claude('Bash', {'command': 'cd; cat notes.txt'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('bash cd up then read', claude('Bash', {'command': 'cd ../.. && cat ../notes.txt'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('bash newline cd', claude('Bash', {'command': 'cd ' + home + '\ncat notes.txt'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('bash cd temp is fine', claude('Bash', {'command': 'cd ' + tmp + ' && cat out.json'}, cwd=src), 'organization', None, []),
            ('bash process substitution', claude('Bash', {'command': 'diff <(cat ~/notes.txt) README.md'}, cwd=repo),
             'private', 'shell_outside_repo', []),
            ('bash process substitution in repo', claude('Bash', {'command': 'diff <(git show HEAD:README.md) README.md'}, cwd=repo),
             'organization', None, []),
            ('bash command substitution', claude('Bash', {'command': 'cat $(echo ~/notes.txt)'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('bash backticks', claude('Bash', {'command': 'wc -l `ls ' + home + '/Documents`'}, cwd=src), 'private', 'personal_content', []),
            ('bash unknown variable names nothing', claude('Bash', {'command': 'cat "$NOTES_FILE"'}, cwd=src), 'organization', None, []),
            ('bash absolute home path', claude('Bash', {'command': 'head -n 3 ' + home + '/notes.txt'}, cwd=src),
             'private', 'shell_outside_repo', []),
            ('bash symlink by bare name', claude('Bash', {'command': 'cat notes-link.md'}, cwd=repo), 'private', 'shell_outside_repo', []),
            ('bash redirect from outside', claude('Bash', {'command': 'wc -l < ' + home + '/notes.txt'}, cwd=src),
             'private', 'shell_outside_repo', []),
            ('bash -c nested', claude('Bash', {'command': "bash -c 'cat ~/notes.txt'"}, cwd=src), 'private', 'shell_outside_repo', []),
            ('eval', claude('Bash', {'command': 'eval "cat ' + home + '/notes.txt"'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('python inline indirect home', claude('Bash', {'command': 'python3 -c "import os;print(os.path.expanduser(1))"'}, cwd=src),
             'private', 'shell_outside_repo', []),
            ('python inline absolute', claude('Bash', {'command': 'python3 -c "print(open(\'' + home + '/notes.txt\').read())"'}, cwd=src),
             'private', 'shell_outside_repo', []),
            ('git -C other project', claude('Bash', {'command': 'git -C ../../other log -3'}, cwd=src), 'team', 'other_project', ['other']),
            ('find from root', claude('Bash', {'command': 'find / -name "*.txt" 2>/dev/null'}, cwd=src), 'private', 'shell_outside_repo', []),
            ('copy credentials', claude('Bash', {'command': 'cp ~/.aws/credentials ' + tmp + '/c'}, cwd=src), 'private', 'credential_file', []),
            ('heredoc body is data', claude('Bash', {'command': 'cat > out.txt <<EOF\n' + home + '/notes.txt is a word here\nEOF\nls'}, cwd=src),
             'private', 'shell_outside_repo', []),
            ('unbalanced quote still scanned', claude('Bash', {'command': 'cat "' + home + '/notes.txt'}, cwd=src),
             'private', 'shell_outside_repo', []),
            # Codex hook shapes.
            ('codex Bash', {'hook_event_name': 'PostToolUse', 'session_id': 's', 'turn_id': 't', 'tool_name': 'Bash',
                            'tool_use_id': 'c1', 'cwd': src, 'tool_input': {'command': 'cat ~/notes.txt'}, 'tool_response': 'x'},
             'private', 'shell_outside_repo', []),
            ('codex apply_patch in repo', {'hook_event_name': 'PostToolUse', 'session_id': 's', 'tool_name': 'apply_patch', 'cwd': repo,
                                           'tool_input': {'command': '*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** End Patch\n'}},
             'organization', None, []),
            ('codex apply_patch outside', {'hook_event_name': 'PostToolUse', 'session_id': 's', 'tool_name': 'apply_patch', 'cwd': repo,
                                           'tool_input': {'command': '*** Begin Patch\n*** Update File: ../../notes.txt\n@@\n-a\n+b\n*** End Patch\n'}},
             'private', 'outside_repo', []),
            ('codex mcp', {'hook_event_name': 'PostToolUse', 'session_id': 's', 'tool_name': 'mcp__drive__fetch_document', 'cwd': repo,
                           'tool_input': {'id': 'x'}, 'tool_response': {'content': []}}, 'private', 'mcp_server', []),
            # Codex rollout shapes (desktop capture).
            ('rollout exec_command', {'hook_event_name': 'PostToolUse', 'tool_name': 'exec_command',
                                      'tool_input': json.dumps({'cmd': 'cat notes.txt', 'workdir': home})},
             'private', 'shell_outside_repo', []),
            ('rollout shell argv', {'hook_event_name': 'PostToolUse', 'tool_name': 'shell',
                                    'tool_input': json.dumps({'command': ['bash', '-lc', 'ls src'], 'workdir': repo})},
             'organization', None, []),
            ('rollout CommandExecution', {'hook_event_name': 'PostToolUse', 'tool_name': 'CommandExecution', 'cwd': src,
                                          'tool_input': {'command': 'cat ' + home + '/Documents/plan.md'}},
             'private', 'personal_content', []),
            ('rollout FileChange', {'hook_event_name': 'PostToolUse', 'tool_name': 'FileChange', 'cwd': repo,
                                    'tool_input': {'changes': [{'path': repo + '/src/app.py', 'kind': 'update'}]}},
             'organization', None, []),
            ('rollout McpToolCall', {'hook_event_name': 'PostToolUse', 'tool_name': 'search', 'mcp_server': 'mail', 'cwd': repo,
                                     'tool_input': {}}, 'private', 'mcp_server', []),
            ('rollout extension', {'hook_event_name': 'PostToolUse', 'tool_name': 'Extension:browser', 'cwd': repo,
                                   'tool_input': {}}, 'private', 'unknown_tool', []),
        ]

    # A realistic cloud and GitHub heavy session: none of these name an outside path.
    UNTAINTED = [
        'aws sts get-caller-identity --profile dev',
        'aws s3 ls s3://example-bucket/prefix/ --recursive | head',
        'aws logs tail /aws/lambda/ingest-fn --since 1h --profile dev',
        "aws cloudformation describe-stacks --stack-name app-dev --query 'Stacks[0].Outputs' --output table",
        'export AWS_PROFILE=dev && aws ecs list-services --cluster app',
        'echo $AWS_PROFILE $?',
        'gh pr create --title "Add origin labels" --body "Summary of the change"',
        'gh api repos/example/app/pulls/3/comments --jq ".[].body"',
        'gh api /repos/example/app/actions/runs?per_page=5',
        'gh run watch 123456 --exit-status',
        'gh issue view 12 --comments',
        'git fetch origin && git rebase origin/main',
        'git log --oneline -20 -- src/',
        'git push -u origin feature/origin-labels',
        'cdk deploy AppStack --profile dev --require-approval never',
        'npm test && npm run lint',
        'pytest -q tests/ -k origin',
        'uv run pytest -x',
        'kubectl get pods -n app -o wide',
        'curl -s https://api.github.com/repos/example/app | jq .stargazers_count',
        'curl -s http://localhost:8080/health',
        'docker build -t app:dev . && docker run --rm app:dev pytest',
        'python -m pip install -r requirements.txt',
        'terraform plan -out /tmp/plan.out',
        'cat $TMPDIR/out.json | jq .',
        'make build 2>&1 | tail -20',
        'env | grep AWS_',
        'ssh build-box "cat /var/log/app.log | tail"',
        'for f in src/*.py; do wc -l "$f"; done',
        'diff <(git show HEAD~1:src/app.py) src/app.py',
    ]
    def test_table(self):
        context = self.tree.context()
        for name, event, ceiling, reason, projects in self.rows():
            with self.subTest(name):
                label = origin.classify(event, context)
                self.assertEqual((label['ceiling'], label['reason'], label['projects']), (ceiling, reason, projects), label)

    def test_cloud_and_github_heavy_commands_stay_untainted(self):
        context = self.tree.context()
        for command in self.UNTAINTED:
            with self.subTest(command):
                label = origin.classify(claude('Bash', {'command': command}, cwd=str(self.tree.repo)), context)
                self.assertEqual((label['ceiling'], label['projects']), ('organization', []), label)

    def test_outside_path_beyond_home_and_temp_is_private(self):
        context = self.tree.context()
        context.system = context.temp = []  # as if the synthetic tree were not under a temporary folder
        target = self.tree.tmp / 'volume/data.csv'
        target.parent.mkdir()
        target.write_text('x')
        self.assertEqual(origin.classify(claude('Read', {'file_path': str(target)}), context)['ceiling'], 'private')

    def test_non_tool_events_are_neutral(self):
        context = self.tree.context()
        for kind in ('UserPromptSubmit', 'Stop', 'StopFailure', 'PostCompact', 'SessionStart'):
            self.assertEqual(origin.classify({'hook_event_name': kind, 'prompt': '~/notes.txt'}, context)['ceiling'],
                             'organization')

    def test_without_a_current_project_nothing_is_this_project(self):
        context = origin.Context(None, str(self.tree.repo), {})
        label = origin.classify(claude('Read', {'file_path': str(self.tree.repo / 'README.md')}), context)
        self.assertEqual((label['ceiling'], label['projects']), ('team', ['demo']))
        label = origin.classify(claude('Read', {'file_path': str(self.tree.home / 'src/plain/y.py')}), context)
        self.assertEqual(label['ceiling'], 'private')

    def test_hint_is_coarse(self):
        label = origin.classify(claude('Read', {'file_path': str(self.tree.home / 'Documents/plan.md')}), self.tree.context())
        self.assertEqual(label['hint'], '~/Documents/...')


class AllowListTest(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self)

    def test_allowed_paths_relative_absolute_and_home(self):
        home = self.tree.home
        for entry in ('../../shared-fixtures', str(home / 'shared-fixtures'), '~/shared-fixtures'):
            with self.subTest(entry):
                context = self.tree.context(allowed_paths=[entry])
                label = origin.classify(claude('Read', {'file_path': str(home / 'shared-fixtures/f.csv')}), context)
                self.assertEqual((label['ceiling'], label['kinds']), ('organization', ['allowed_path']))
                # Only that folder: the rest of home stays private.
                label = origin.classify(claude('Read', {'file_path': str(home / 'notes.txt')}), context)
                self.assertEqual(label['ceiling'], 'private')

    def test_too_broad_entries_are_ignored(self):
        for entry in ('~', '~/', '/', str(self.tree.home.parent), '~/.ssh', '~/.aws/creds', '~/Library/Mail', '../../..', '', 7):
            with self.subTest(entry):
                context = self.tree.context(allowed_paths=[entry])
                self.assertEqual(context.allowed_paths, [])
                label = origin.classify(claude('Read', {'file_path': str(self.tree.home / '.ssh/config')}), context)
                self.assertEqual(label['ceiling'], 'private')

    def test_allowed_mcp_servers(self):
        context = self.tree.context(allowed_mcp_servers=['docs', 'issue-tracker'])
        for tool, ceiling in (('mcp__docs__search', 'organization'), ('mcp__issue_tracker__get', 'organization'),
                              ('mcp__plugin_acme_docs__search', 'organization'), ('mcp__mail__search', 'private'),
                              ('mcp__docsmail__search', 'private')):
            with self.subTest(tool):
                self.assertEqual(origin.classify(claude(tool, {}), context)['ceiling'], ceiling)


class RecalledMemoryTest(unittest.TestCase):
    def test_item_labels(self):
        cases = [({'origin': {'project': 'demo', 'sharing': 'organization'}}, 'organization', []),
                 ({'origin': {'project': 'demo', 'sharing': 'team'}}, 'team', []),
                 ({'origin': {'project': 'other', 'sharing': 'team'}}, 'team', ['other']),
                 ({'origin': {'project': 'other', 'sharing': 'team', 'projects': ['third']}}, 'team', ['other', 'third']),
                 ({'origin': {'project': 'other', 'sharing': 'organization', 'projects': ['third']}},
                  'organization', ['other', 'third']),
                 ({'origin': {'project': 'demo', 'sharing': 'private'}}, 'private', []),
                 ({'origin': {'project': '../bad', 'sharing': 'team'}}, 'private', []),
                 ({'origin': {'project': None, 'sharing': 'team'}}, 'private', []),
                 ({'origin': {'project': None, 'sharing': 'organization'}}, 'organization', []),
                 ({'id': 'unlabelled'}, 'private', []),
                 ({'provenance': {'source_type': 'preference'}}, 'organization', [])]
        for item, ceiling, projects in cases:
            with self.subTest(item):
                label = origin.item_label(item, 'demo')
                self.assertEqual((label['ceiling'], label['projects']), (ceiling, projects))

    def test_injected_cards_most_restrictive_wins_and_unlabelled_card_is_private(self):
        team = {'id': 'a', 'origin': {'project': 'demo', 'sharing': 'team'}}
        other = {'id': 'c', 'origin': {'project': 'other', 'sharing': 'team'}}
        self.assertEqual(origin.recalled([team], 'demo', cards=True)['ceiling'], 'team')
        both = origin.recalled([team, other], 'demo', cards=True)
        self.assertEqual((both['ceiling'], both['projects']), ('team', ['other']))
        self.assertEqual(origin.recalled([team, {'id': 'b'}], 'demo', cards=True)['ceiling'], 'private')
        self.assertEqual(origin.recalled([], 'demo', cards=True)['ceiling'], 'organization')


class SessionTaintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def taint(self):
        return origin.SessionOrigins(self.tmp)

    def test_sticky_from_tainting_event_on_and_never_before(self):
        outside = origin.Label('private', ('outside_repo',), 'outside_repo', '~/...')
        before = self.taint().apply('s1', origin.Label('organization', ('project',)))
        self.assertEqual((before['ceiling'], origin.wire(before)), ('organization', None))
        self.assertFalse((self.tmp / origin.STATE).exists())  # nothing recorded for an untainted session
        hit = self.taint().apply('s1', outside)
        self.assertEqual((hit['ceiling'], hit['reason'], hit['inherited'], hit['newly_restricted']),
                         ('private', 'outside_repo', False, True))
        # A new process (fresh object) sees the same session state.
        for label in (origin.NEUTRAL, origin.Label('organization', ('project',)), origin.Label('organization', ('web',))):
            later = self.taint().apply('s1', label)
            self.assertEqual((later['ceiling'], later['reason'], later['inherited'], later['newly_restricted']),
                             ('private', 'outside_repo', True, False))
        # Another restrictive event keeps the first reason for the session and its own for itself.
        again = self.taint().apply('s1', origin.Label('private', ('mcp',), 'mcp_server', 'mail'))
        self.assertEqual((again['reason'], again['inherited'], again['newly_restricted']), ('mcp_server', False, False))
        self.assertEqual(self.taint().get('s1')['reason'], 'outside_repo')
        # A different session (for example after /clear) starts clean.
        self.assertEqual(self.taint().apply('s2', origin.NEUTRAL)['ceiling'], 'organization')
        self.taint().forget('s1')
        self.assertEqual(self.taint().apply('s1', origin.NEUTRAL)['ceiling'], 'organization')

    def test_most_restrictive_wins_and_never_widens(self):
        self.taint().apply('s', origin.Label('team', ('recalled_memory',), 'recalled_memory'))
        self.assertEqual(self.taint().apply('s', origin.NEUTRAL)['ceiling'], 'team')
        self.taint().apply('s', origin.Label('private', ('outside_repo',), 'outside_repo'))
        self.taint().apply('s', origin.Label('team', ('recalled_memory',), 'recalled_memory'))
        self.assertEqual(self.taint().apply('s', origin.NEUTRAL)['ceiling'], 'private')

    def test_rollout_call_output_inherits_its_call(self):
        taint = self.taint()
        taint.apply('s', origin.Label('private', ('shell',), 'shell_outside_repo'), call_id='c1')
        taint.forget('s')
        taint.apply('s', origin.Label('private', ('shell',), 'shell_outside_repo'), call_id='c1')
        out = taint.apply('s', origin.NEUTRAL, call_id='c1', call_output=True)
        self.assertEqual((out['ceiling'], out['reason']), ('private', 'shell_outside_repo'))
        clean = self.taint()
        clean.apply('t', origin.Label('organization', ('project',)), call_id='c2')
        self.assertEqual(clean.apply('t', origin.NEUTRAL, call_id='c2', call_output=True)['ceiling'], 'organization')
        # An output whose call was never seen fails closed.
        unknown = clean.apply('u', origin.NEUTRAL, call_id='missing', call_output=True)
        self.assertEqual((unknown['ceiling'], unknown['reason']), ('private', 'unknown_call'))

    def test_state_survives_separate_processes(self):
        code = ('import sys;from agentclient import origin;'
                'origin.SessionOrigins(sys.argv[1]).apply("s",origin.Label("private",("mcp",),"mcp_server","mail"))')
        subprocess.run([sys.executable, '-c', code, str(self.tmp)], check=True,
                       env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
        self.assertEqual(self.taint().apply('s', origin.NEUTRAL)['ceiling'], 'private')
        self.assertEqual(oct((self.tmp / origin.STATE).stat().st_mode & 0o777), '0o600')
        # Only categories and a coarse hint are stored, keyed by a session hash.
        stored = json.loads((self.tmp / origin.STATE).read_text())
        self.assertNotIn('s', stored)

    def test_status_line(self):
        self.taint().apply('s', origin.Label('private', ('outside_repo',), 'outside_repo', '~/...'))
        line = origin.status_line(self.taint().get('s'), now=True)
        self.assertEqual(line, 'vaelius: this session read outside the repo (~/...) \u2014 capture is now private')
        self.assertIsNone(origin.status_line(None))


class WireContractTest(unittest.TestCase):
    def event(self, **extra):
        from agentclient.enterprise_capture import normalize_capture
        return normalize_capture({'hook_event_name': 'Stop', 'session_id': 's', 'event_id': 'e1',
                                  'last_assistant_message': 'done'}, 'demo', 'conn', **extra)

    def test_label_is_sent_and_lowers_the_request(self):
        label = origin.wire({'ceiling': 'private', 'kinds': ['outside_repo'], 'reason': 'outside_repo',
                             'inherited': True, 'hint': '~/...'})
        self.assertEqual(label, {'ceiling': 'private', 'kinds': ['outside_repo'], 'reason': 'outside_repo', 'projects': [],
                                 'inherited': True})
        event = self.event(sharing='team', origin_label=label)
        self.assertEqual(event['origin_label'], label)
        self.assertNotIn('sharing', event)
        self.assertNotIn('~', json.dumps(event['origin_label']))
        team = dict(label, ceiling='team', reason='recalled_memory', kinds=['recalled_memory'])
        self.assertEqual(self.event(sharing='team', origin_label=team)['sharing'], 'team')

    def test_ceiling_lowers_both_requests_on_the_ladder(self):
        # Only the sharing request travels on the wire now: the access model
        # drops the originals ladder (see docs/ACCESS_MODEL.md), so the client
        # never sends `originals` whatever the scope file or origin label asks.
        team = {'ceiling': 'team', 'kinds': ['recalled_memory'], 'reason': 'recalled_memory', 'projects': [],
                'inherited': False}
        event = self.event(sharing='organization', originals='organization', origin_label=team)
        self.assertEqual(event['sharing'], 'team')
        self.assertNotIn('originals', event)
        event = self.event(sharing='organization', originals='team', origin_label=dict(team, ceiling='private'))
        self.assertNotIn('sharing', event)
        self.assertNotIn('originals', event)
        projects = dict(team, ceiling='organization', reason='other_project', kinds=['other_project'], projects=['birch'])
        event = self.event(sharing='organization', origin_label=projects)
        self.assertEqual(event['sharing'], 'organization')  # the service intersects the project set

    def test_unrestricted_project_events_carry_no_label(self):
        self.assertIsNone(origin.wire({'ceiling': 'organization', 'kinds': ['project'], 'reason': None}))
        web = origin.wire({'ceiling': 'organization', 'kinds': ['web'], 'reason': None})
        self.assertEqual(web, {'ceiling': 'organization', 'kinds': ['web'], 'reason': '', 'projects': [], 'inherited': False})
        # Another project's content: the project set travels, never a path or a hint.
        shared = origin.wire({'ceiling': 'team', 'kinds': ['other_project'], 'reason': 'other_project',
                              'projects': ['other', 'demo'], 'hint': 'other'}, project='demo')
        self.assertEqual(shared, {'ceiling': 'team', 'kinds': ['other_project'], 'reason': 'other_project',
                                  'projects': ['other'], 'inherited': False})
        self.assertIsNone(origin.wire({'ceiling': 'organization', 'kinds': ['project'], 'projects': ['demo']}, project='demo'))
        self.assertNotIn('origin_label', self.event())

    def test_contract_rejects_malformed_labels(self):
        good = {'ceiling': 'private', 'kinds': ['mcp'], 'reason': 'mcp_server', 'projects': [], 'inherited': False}
        validate_origin_label(good)
        for bad in (dict(good, ceiling='public'), dict(good, kinds=['mail']), dict(good, kinds=['mcp', 'mcp']),
                    dict(good, reason='/Users/x'), dict(good, reason=''), dict(good, inherited='no'),
                    dict(good, extra=1), dict(good, projects=['../x']), dict(good, projects=['a', 'a']),
                    dict(good, projects=['p%d' % i for i in range(17)]),
                    {'ceiling': 'organization', 'kinds': [], 'reason': 'mcp_server', 'projects': [], 'inherited': False},
                    {'ceiling': 'team', 'kinds': [], 'reason': '', 'projects': ['other'], 'inherited': False},
                    'private'):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    validate_origin_label(bad)
        event = self.event(origin_label=good)
        event['origin_label'] = dict(good, reason='matched ~/notes.txt')
        with self.assertRaises(ValueError):
            validate_event(event)


class Backend:
    def __init__(self, cards=None):
        self.calls = []
        self.cards = cards or []

    def request(self, path, value=None):
        self.calls.append(path)
        if path.endswith('capture-notice'):
            raise OSError('service unavailable')
        if path.endswith('preferences'):
            return {'values': {}, 'preferences': []}
        if path.endswith('search'):
            return {'answerable': bool(self.cards), 'support': 'complete', 'results': self.cards}
        return {'status': 'recorded'}


class Outbox:
    events = []

    def __init__(self, home):
        pass

    def queue_diagnostics(self, config):
        pass

    def put(self, event):
        Outbox.events.append(event)

    def drain(self, *args, **kwargs):
        return {}

    def close(self):
        pass


class HookTaintTest(unittest.TestCase):
    """End to end through the hook handler: labels, stickiness, notices, request lowering."""

    def setUp(self):
        self.tree = Tree(self)
        self.home = self.tree.tmp / 'profile'
        self.home.mkdir()
        self.config = {'projects': {}, 'sessions': {}, 'knowledge_backend': {
            'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2', 'capture_owner': 'hooks',
            'api_version': 'cloud-local-1', 'connection_id': 'synthetic-connection'}}
        (self.home / 'config.json').write_text(json.dumps(self.config))
        Outbox.events = []

    def hook(self, kind, session='s1', cwd=None, cards=None, **event):
        from agentclient.hooks import handle
        with mock.patch('agentclient.transport.enterprise_client', return_value=Backend(cards)), \
                mock.patch('agentclient.capture_outbox.Outbox', Outbox):
            return handle(self.home, dict({'hook_event_name': kind, 'cwd': str(cwd or self.tree.repo / 'src'),
                                           'session_id': session}, **event), host='claude-code')

    def tool(self, path, **extra):
        return self.hook('PostToolUse', tool_name='Read', tool_input={'file_path': str(path)},
                         tool_response={'type': 'text', 'file': {'filePath': str(path), 'content': 'synthetic'}},
                         tool_use_id='toolu_' + str(len(Outbox.events)), **extra)

    def last(self):
        return Outbox.events[-1]

    def test_inside_then_outside_then_everything_after_is_private(self):
        self.hook('UserPromptSubmit', prompt='summarise the readme')
        self.assertEqual(self.last()['sharing'], 'team')
        result = self.tool(self.tree.repo / 'README.md')
        self.assertEqual((self.last().get('sharing'), self.last().get('origin_label')), ('team', None))
        self.assertNotIn('systemMessage', result)
        result = self.tool(self.tree.home / 'notes.txt')
        tainting = self.last()
        self.assertNotIn('sharing', tainting)
        self.assertEqual(tainting['origin_label'], {'ceiling': 'private', 'kinds': ['outside_repo'],
                                                    'reason': 'outside_repo', 'projects': [], 'inherited': False})
        self.assertEqual(result['systemMessage'],
                         'vaelius: this session read outside the repo (~/...) \u2014 capture is now private')
        self.assertNotIn(str(self.tree.home), json.dumps(tainting['origin_label']))
        # Later tool results, prompts and assistant text inherit the restriction.
        self.tool(self.tree.repo / 'README.md')
        self.hook('UserPromptSubmit', prompt='and now?')
        self.hook('Stop', last_assistant_message='The notes say ...')
        for event in Outbox.events[3:]:
            self.assertNotIn('sharing', event)
            self.assertEqual((event['origin_label']['ceiling'], event['origin_label']['reason'],
                              event['origin_label']['inherited']), ('private', 'outside_repo', True))
        # Events before the tainting one were already captured as team.
        self.assertEqual([e.get('sharing') for e in Outbox.events[:2]], ['team', 'team'])
        # Compaction keeps the restriction: the summary is written from this context.
        self.hook('PostCompact', trigger='auto')
        self.assertEqual(self.last()['origin_label']['ceiling'], 'private')
        # Resuming shows why, once, at session start.
        start = self.hook('SessionStart', source='resume')
        self.assertIn('vaelius: this session read outside the repo (~/...) \u2014 capture is private', start['systemMessage'])
        # /clear starts a new session ID: clean again.
        self.hook('SessionEnd', reason='clear')
        self.hook('UserPromptSubmit', session='s2', prompt='fresh start')
        self.assertEqual((self.last()['sharing'], self.last().get('origin_label')), ('team', None))

    def test_session_that_read_outside_any_project_stays_restricted_inside_one(self):
        self.hook('PostToolUse', cwd=self.tree.home, tool_name='Bash', tool_input={'command': 'cat notes.txt'},
                  tool_response={'stdout': 'synthetic'})
        self.assertEqual(Outbox.events, [])  # not captured: no project there
        self.hook('UserPromptSubmit', prompt='now in the repo')
        self.assertNotIn('sharing', self.last())
        self.assertEqual(self.last()['origin_label']['reason'], 'shell_outside_repo')

    def test_reads_while_paused_still_restrict_after_resume(self):
        (self.home / 'config.json').write_text(json.dumps(dict(self.config, paused=True)))
        self.tool(self.tree.home / 'Documents/plan.md')
        self.assertEqual(Outbox.events, [])
        (self.home / 'config.json').write_text(json.dumps(self.config))
        self.hook('Stop', last_assistant_message='summary of the plan')
        self.assertEqual(self.last()['origin_label']['ceiling'], 'private')

    def test_injected_memory_label_restricts_what_follows(self):
        private_card = {'id': 'd1', 'revision': 'r1', 'title': 't', 'lesson': 'l',
                        'provenance': {'author': 'someone', 'project': 'secret'},
                        'origin': {'project': 'secret', 'sharing': 'private'}}
        result = self.hook('UserPromptSubmit', prompt='what did we decide?', cards=[private_card])
        self.assertIn('additionalContext', result['hookSpecificOutput'])
        self.assertEqual(self.last()['sharing'], 'team')  # the prompt itself precedes the recall
        self.assertIn('recalled memory with narrower sharing', result['systemMessage'])
        self.hook('Stop', last_assistant_message='we decided ...')
        self.assertEqual((self.last()['origin_label']['ceiling'], self.last()['origin_label']['reason']),
                         ('private', 'recalled_memory'))

    def test_same_project_team_memory_does_not_narrow_a_team_project(self):
        card = {'id': 'd1', 'revision': 'r1', 'title': 't', 'lesson': 'l', 'provenance': {'author': 'a'},
                'origin': {'project': 'demo', 'sharing': 'team'}}
        result = self.hook('UserPromptSubmit', prompt='what did we decide?', cards=[card])
        self.assertNotIn('vaelius: this session', result.get('systemMessage', ''))
        self.hook('Stop', last_assistant_message='we decided ...')
        self.assertEqual((self.last()['sharing'], self.last().get('origin_label')), ('team', None))

    def test_same_project_team_memory_narrows_an_organization_request(self):
        (self.tree.repo / '.vaelius').write_text('project = "demo"\nsharing = "organization"\n')
        card = {'id': 'd1', 'revision': 'r1', 'title': 't', 'lesson': 'l', 'provenance': {'author': 'a'},
                'origin': {'project': 'demo', 'sharing': 'team'}}
        self.hook('UserPromptSubmit', prompt='what did we decide?', cards=[card])
        self.hook('Stop', last_assistant_message='we decided ...')
        self.assertEqual(self.last()['origin_label']['ceiling'], 'team')

    def test_session_start_names_the_in_scope_folders(self):
        result = self.hook('SessionStart', source='startup')
        line = 'vaelius: in-scope folders: ~/src/repo, ../other, ../quiet, ../repo-copy (reads elsewhere make this session private)'
        self.assertIn(line, result['hookSpecificOutput']['additionalContext'])
        self.assertIn(line, result['systemMessage'])
        self.assertNotIn('../off', line)  # capture off
        self.assertNotIn('../plain', line)  # no scope file
        self.assertNotIn(str(self.tree.home), json.dumps(result))
        for word in ('REQUIRED', 'must', 'verbatim', 'print'):
            self.assertNotIn(word, line)

    def test_session_start_line_can_be_turned_off(self):
        (self.home / 'config.json').write_text(json.dumps(dict(self.config, scope_context_notice=False)))
        result = self.hook('SessionStart', source='startup')
        self.assertNotIn('in-scope folders', json.dumps(result))

    def test_scope_file_allowlists_apply_through_the_hook(self):
        (self.tree.repo / '.vaelius').write_text(
            SCOPE + '[capture]\nmode = "on"\nallowed_paths = ["../../shared-fixtures"]\nallowed_mcp_servers = ["docs"]\n')
        self.tool(self.tree.home / 'shared-fixtures/f.csv')
        self.hook('PostToolUse', tool_name='mcp__docs__search', tool_input={'q': 'x'}, tool_response={})
        self.hook('Stop', last_assistant_message='done')
        self.assertEqual([e.get('sharing') for e in Outbox.events], ['team', 'team', 'team'])
        self.hook('PostToolUse', tool_name='mcp__mail__search', tool_input={'q': 'x'}, tool_response={})
        self.assertEqual(self.last()['origin_label']['reason'], 'mcp_server')

    def test_cloud_and_github_session_through_the_hook_stays_team(self):
        self.hook('UserPromptSubmit', prompt='deploy the stack and open a PR')
        for command in ClassifierTableTest.UNTAINTED:
            result = self.hook('PostToolUse', cwd=self.tree.repo, tool_name='Bash', tool_input={'command': command},
                               tool_response={'stdout': 'synthetic output', 'stderr': '', 'interrupted': False})
            self.assertNotIn('systemMessage', result, command)
        self.hook('Stop', last_assistant_message='Deployed and opened the PR.')
        self.assertEqual({e.get('sharing') for e in Outbox.events}, {'team'})
        self.assertEqual([e for e in Outbox.events if 'origin_label' in e], [])
        self.assertFalse((self.home / origin.STATE).exists() and origin.SessionOrigins(self.home).get('s1')['ceiling'] != 'organization')

    def test_reading_another_scoped_repo_tags_both_projects(self):
        self.hook('UserPromptSubmit', prompt='compare with the other service')
        result = self.tool(self.tree.home / 'src/other/x.py')
        label = self.last()['origin_label']
        self.assertEqual((label['ceiling'], label['projects'], label['reason']), ('team', ['other'], 'other_project'))
        self.assertEqual(self.last()['sharing'], 'team')  # still a team request: the service intersects readers
        self.assertIn('vaelius: this session spans projects demo, other', result['systemMessage'])
        self.hook('Stop', last_assistant_message='The two services differ in ...')
        self.assertEqual((self.last()['origin_label']['projects'], self.last()['origin_label']['inherited']), (['other'], True))
        # A repository without a scope file still makes the session private.
        self.tool(self.tree.home / 'src/plain/y.py')
        self.assertEqual(self.last()['origin_label']['ceiling'], 'private')

    def test_moving_to_another_project_carries_the_first_one_along(self):
        self.tool(self.tree.repo / 'README.md')
        self.assertNotIn('origin_label', self.last())
        result = self.hook('UserPromptSubmit', cwd=self.tree.home / 'src/other', prompt='now this repo')
        event = self.last()
        self.assertEqual((event['project'], event['origin_label']['projects']), ('other', ['demo']))
        self.assertIn('spans projects demo, other', result['systemMessage'])

    def test_compaction_does_not_reset_and_clear_does(self):
        self.tool(self.tree.home / 'notes.txt')
        self.hook('PostCompact', trigger='manual')
        self.hook('SessionStart', source='compact')
        self.hook('Stop', last_assistant_message='after compaction')
        self.assertEqual(self.last()['origin_label']['ceiling'], 'private')
        self.hook('SessionEnd', reason='clear')
        self.hook('Stop', last_assistant_message='same id after clear (hypothetical)')
        self.assertNotIn('origin_label', self.last())

    def test_classifier_error_fails_closed(self):
        with mock.patch('agentclient.origin.classify', side_effect=RuntimeError('boom')):
            self.tool(self.tree.repo / 'README.md')
        self.assertEqual(self.last()['origin_label']['ceiling'], 'private')
        self.assertNotIn('sharing', self.last())


class DesktopRolloutTest(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self)
        self.home = self.tree.tmp / 'profile'
        self.home.mkdir()

    def test_call_and_separate_output_share_a_label(self):
        from agentclient.enterprise_desktop import convert, origin_labeller
        labeller = origin_labeller(self.home, {}, self.tree.scope(), str(self.tree.repo), 'rollout-1')
        call = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c1',
                                                     'arguments': json.dumps({'cmd': 'cat ~/notes.txt'})}}
        output = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'c1', 'output': 'synthetic'}}
        first = convert(call, 'rollout-1', 'demo', 'conn', order=1, sharing='team', origin=labeller)
        second = convert(output, 'rollout-1', 'demo', 'conn', order=2, sharing='team', origin=labeller)
        self.assertEqual((first['origin_label']['reason'], first['origin_label']['inherited']), ('shell_outside_repo', False))
        self.assertEqual(second['origin_label']['ceiling'], 'private')
        self.assertNotIn('sharing', second)

    def test_in_repo_call_output_stays_shared(self):
        from agentclient.enterprise_desktop import convert, origin_labeller
        labeller = origin_labeller(self.home, {}, self.tree.scope(), str(self.tree.repo), 'rollout-2')
        call = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'c1',
                                                     'arguments': json.dumps({'cmd': 'ls src', 'workdir': str(self.tree.repo)})}}
        output = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'c1', 'output': 'app.py'}}
        convert(call, 'rollout-2', 'demo', 'conn', order=1, sharing='team', origin=labeller)
        second = convert(output, 'rollout-2', 'demo', 'conn', order=2, sharing='team', origin=labeller)
        self.assertEqual((second.get('sharing'), second.get('origin_label')), ('team', None))

    def test_mcp_item_server_is_classified(self):
        from agentclient.enterprise_desktop import convert, origin_labeller
        labeller = origin_labeller(self.home, {}, self.tree.scope(), str(self.tree.repo), 'rollout-3')
        item = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 'rollout-3', 'item': {
            'id': 'i1', 'type': 'McpToolCall', 'server': 'mail', 'tool': 'search', 'arguments': {}, 'result': {}}}}
        event = convert(item, 'rollout-3', 'demo', 'conn', sharing='team', origin=labeller)
        self.assertEqual(event['origin_label']['reason'], 'mcp_server')


class ScopeFileExceptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / '.git').mkdir()

    def parse(self, text):
        from agentclient.scope_file import parse
        (self.tmp / '.vaelius').write_text(text)
        return parse(self.tmp / '.vaelius')

    def test_capture_table_and_dotted_keys(self):
        found = self.parse('project = "demo"\n[capture]\nmode = "read-only"\nallowed_paths = ["../data"]\n'
                           'allowed_mcp_servers = ["docs"]\n')
        self.assertEqual((found['capture'], found['allowed_paths'], found['allowed_mcp_servers']),
                         ('read-only', ['../data'], ['docs']))
        found = self.parse('project = "demo"\ncapture.allowed_paths = ["~/datasets"]\n')
        self.assertEqual((found['capture'], found['allowed_paths']), ('on', ['~/datasets']))
        self.assertEqual(self.parse('project = "demo"\n')['allowed_paths'], [])

    def test_invalid_exceptions_fail_closed(self):
        from agentclient.scope_file import discover
        for text in ('capture.allowed_paths = "~/x"\n', 'capture.allowed_paths = [1]\n',
                     'capture.allowed_mcp_servers = ["a b"]\n', 'capture.unknown = 1\n',
                     'capture.mode = "sometimes"\n'):
            with self.subTest(text):
                (self.tmp / '.vaelius').write_text('project = "demo"\n' + text)
                found = discover(self.tmp)
                self.assertEqual(found['capture'], 'off')
                self.assertIn('error', found)


if __name__ == '__main__':
    unittest.main()


class ScopedFolderTest(unittest.TestCase):
    """Scoped-folder allowlist: repositories under a stand-in `~/Documents`, ignored scope
    files at home and personal folders, nested overrides, same-project siblings and
    Claude Code's own state under `~/.claude`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = self.home = self.tmp / 'home'
        ws = self.ws = home / 'Documents/ws'
        self.repo = ws / 'repo'
        files = {
            home / '.vaelius': 'project = "everything"\nsharing = "organization"\n',  # ignored: home
            home / 'Documents/.vaelius': 'project = "everything"\nsharing = "organization"\n',  # ignored: personal root
            self.repo / '.vaelius': 'project = "same"\nsharing = "team"\n',
            ws / 'sib/.vaelius': 'project = "same"\nsharing = "private"\n',
            ws / 'diff/.vaelius': 'project = "other"\nsharing = "team"\n',
            ws / 'my_lib.v2/.vaelius': 'project = "same"\n',  # `_` and `.` both encode as `-`
            home / 'work/.vaelius': 'project = "same"\nsharing = "team"\n',  # not a Git repository
            home / 'work/nested/.vaelius': 'project = "inner"\nsharing = "team"\n',
        }
        for path in (self.repo / '.git', ws / 'sib/.git', ws / 'diff/.git', ws / 'plain/.git', home / 'src/plain/.git', home / 'work/repo-a/.git',
                     self.repo / 'src', home / 'loose-dir'):
            path.mkdir(parents=True, exist_ok=True)
        for path in (self.repo / 'src/app.py', self.repo / '.env', self.repo / '.env.example', self.repo / 'certs/dev.pem',
                     ws / 'sib/s.py', ws / 'diff/d.py', ws / 'plain/p.py', home / 'src/plain/p.py', ws / 'loose.md', home / 'Documents/taxes.pdf',
                     home / 'notes.txt', home / 'work/repo-a/a.py', home / 'work/readme.md', home / 'work/nested/n.py',
                     home / 'Desktop/todo.txt', home / 'loose-dir/x.txt', home / '.claude/CLAUDE.md',
                     home / '.claude/reference/boxes.md', home / '.claude/plans/p.md', home / '.claude/settings.json',
                     home / '.claude/settings.local.json', home / '.claude/history.jsonl', home / '.aws/credentials'):
            files.setdefault(path, 'synthetic\n')
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        projects = home / '.claude/projects'
        for folder in (self.repo / 'src', ws / 'sib', ws / 'diff', ws / 'my_lib.v2', home / 'loose-dir', home / 'gone/away'):
            for leaf in ('tool-results/r1.txt', 'tasks/t1.output', 'memory/MEMORY.md', 'session.jsonl'):
                target = projects / origin.slug(folder) / leaf
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('synthetic\n')
        self.enterContext(mock.patch.dict(os.environ, {'HOME': str(home)}))
        scope = {'project': 'same', 'sharing': 'team', 'root': str(self.repo), 'scope_file': str(self.repo / '.vaelius')}
        self.context = origin.Context(scope, str(self.repo / 'src'), {})

    def test_slug_matches_claude_code(self):
        self.assertEqual(origin.slug('/srv/a.b/Documents/my_repo/.claude worktrees/x'),
                         '-srv-a-b-Documents-my-repo--claude-worktrees-x')

    def test_rules(self):
        home, ws, repo = self.home, self.ws, self.repo
        state = home / '.claude/projects'
        rows = [
            # Scoped folders under a personal folder behave normally.
            ('repo under Documents', repo / 'src/app.py', 'organization', None, []),
            ('same-project sibling repo', ws / 'sib/s.py', 'organization', None, []),
            ('different-project repo', ws / 'diff/d.py', 'team', 'other_project', ['other']),
            ('unscoped repo next door', ws / 'plain/p.py', 'private', 'personal_content', []),
            ('unscoped repo elsewhere', home / 'src/plain/p.py', 'private', 'other_project', []),
            # Ignored scope files at home and directly at personal folders.
            ('home file despite ~/.vaelius', home / 'notes.txt', 'private', 'outside_repo', []),
            ('unknown home folder', home / 'loose-dir/x.txt', 'private', 'outside_repo', []),
            ('Documents despite ~/Documents/.vaelius', home / 'Documents/taxes.pdf', 'private', 'personal_content', []),
            ('loose file in a Documents workspace', ws / 'loose.md', 'private', 'personal_content', []),
            ('Desktop', home / 'Desktop/todo.txt', 'private', 'personal_content', []),
            # Non-Git scoped folder covers its subtree, including repos without a scope file; nested overrides.
            ('non-git scoped folder', home / 'work/readme.md', 'organization', None, []),
            ('repo inside scoped folder', home / 'work/repo-a/a.py', 'organization', None, []),
            ('nested override', home / 'work/nested/n.py', 'team', 'other_project', ['inner']),
            # Credentials stay private, even inside the project.
            ('.env in repo', repo / '.env', 'private', 'credential_file', []),
            ('key in repo', repo / 'certs/dev.pem', 'private', 'credential_file', []),
            ('.env template in repo', repo / '.env.example', 'organization', None, []),
            ('aws credentials', home / '.aws/credentials', 'private', 'credential_file', []),
            # Claude Code state and configuration.
            ('own tool-results', state / origin.slug(repo / 'src') / 'tool-results/r1.txt', 'organization', None, []),
            ('own task output', state / origin.slug(repo / 'src') / 'tasks/t1.output', 'organization', None, []),
            ('own auto-memory', state / origin.slug(repo / 'src') / 'memory/MEMORY.md', 'organization', None, []),
            ('same-project sibling state', state / origin.slug(ws / 'sib') / 'tool-results/r1.txt', 'organization', None, []),
            ('state of a folder named with _ and .', state / origin.slug(ws / 'my_lib.v2') / 'memory/MEMORY.md',
             'organization', None, []),
            ('other-project state', state / origin.slug(ws / 'diff') / 'memory/MEMORY.md', 'team', 'other_project', ['other']),
            ('unscoped folder state', state / origin.slug(home / 'loose-dir') / 'session.jsonl', 'private', 'outside_repo', []),
            ('deleted folder state', state / origin.slug(home / 'gone/away') / 'session.jsonl', 'private', 'outside_repo', []),
            ('CLAUDE.md', home / '.claude/CLAUDE.md', 'organization', None, []),
            ('reference', home / '.claude/reference/boxes.md', 'organization', None, []),
            ('plans', home / '.claude/plans/p.md', 'organization', None, []),
            ('settings', home / '.claude/settings.json', 'organization', None, []),
            ('local settings', home / '.claude/settings.local.json', 'organization', None, []),
            ('other ~/.claude files', home / '.claude/history.jsonl', 'private', 'outside_repo', []),
        ]
        for description, path, ceiling, reason, projects in rows:
            with self.subTest(description):
                label = origin.classify(claude('Read', {'file_path': str(path)}, cwd=str(repo / 'src')), self.context)
                self.assertEqual((label['ceiling'], label['reason'], label['projects']), (ceiling, reason, projects))

    def test_transcript_path_marks_the_session_state_folder(self):
        # A working directory outside the root (the agent changed directory) is not this session's folder.
        moved = origin.Context({'project': 'same', 'sharing': 'team', 'root': str(self.repo)}, str(self.home / 'loose-dir'), {})
        target = self.home / '.claude/projects' / origin.slug(self.home / 'loose-dir') / 'tool-results/r1.txt'
        self.assertEqual(moved.place(str(target))['ceiling'], 'private')
        # Without a project, nothing is this session's own state.
        unscoped = origin.Context(None, str(self.home / 'loose-dir'), {})
        self.assertEqual(unscoped.place(str(target))['ceiling'], 'private')
        other = self.home / '.claude/projects' / origin.slug(self.ws / 'plain') / 'x'
        other.parent.mkdir(parents=True)
        other.write_text('x')
        scope = {'project': 'same', 'root': str(self.repo)}
        event = claude('Read', {'file_path': str(other)}, cwd=str(self.repo))
        event['transcript_path'] = '/elsewhere/session.jsonl'  # not under ~/.claude/projects: ignored
        self.assertEqual(origin.classify(event, origin.Context(scope, str(self.repo), {}))['ceiling'], 'private')
        event['transcript_path'] = str(other.parent / 'session.jsonl')
        self.assertEqual(origin.classify(event, origin.Context(scope, str(self.repo), {}))['ceiling'], 'organization')

    def test_scope_of_reports_root_project_sharing_and_category(self):
        rows = [
            (self.repo / 'src/app.py', 'project', str(self.repo), 'same', 'team'),
            (self.ws / 'sib/s.py', 'project', str(self.ws / 'sib'), 'same', 'private'),
            (self.ws / 'diff/d.py', 'scoped', str(self.ws / 'diff'), 'other', 'team'),
            (self.home / 'work/repo-a/a.py', 'project', str(self.home / 'work'), 'same', 'team'),
            (self.home / '.claude/projects' / origin.slug(self.ws / 'diff') / 'memory/MEMORY.md', 'claude_state',
             str(self.ws / 'diff'), 'other', 'team'),
            (self.home / '.claude/CLAUDE.md', 'claude_config', None, None, None),
            (self.home / 'Documents/taxes.pdf', 'personal', None, None, None),
            (self.repo / '.env', 'credential', None, None, None),
            (self.home / 'notes.txt', 'unknown', None, None, None),
            (self.home / 'nope.txt', 'missing', None, None, None),
            (self.ws / 'plain/p.py', 'personal', None, None, None),
            (self.home / 'src/plain/p.py', 'unscoped_repo', str(self.home / 'src/plain'), None, None),
            (Path('/usr/bin/env'), 'system', None, None, None),
        ]
        for path, category, root, project, sharing in rows:
            with self.subTest(str(path)):
                info = self.context.scope_of(str(path))
                self.assertEqual((info['category'], info['root'], info['project'], info['sharing']),
                                 (category, root, project, sharing))
        temp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, temp, True)
        (temp / 'scratch.txt').write_text('x')
        if not origin._within(temp, self.home):
            self.assertEqual(self.context.scope_of(str(temp / 'scratch.txt'))['category'], 'temp')

    def test_session_moving_between_same_project_repos_stays_quiet(self):
        taint = origin.SessionOrigins(self.tmp / 'profile')
        for path in (self.repo / 'src/app.py', self.ws / 'sib/s.py',
                     self.home / '.claude/projects' / origin.slug(self.repo / 'src') / 'tool-results/r1.txt'):
            label = origin.classify(claude('Read', {'file_path': str(path)}, cwd=str(self.repo / 'src')), self.context)
            effective = taint.apply('s', label, project='same')
            self.assertEqual((effective['ceiling'], effective['projects'], effective['newly_restricted']),
                             ('organization', [], False))
        self.assertIsNone(origin.status_line(taint.get('s')))
        label = origin.classify(claude('Read', {'file_path': str(self.ws / 'diff/d.py')}, cwd=str(self.repo)), self.context)
        effective = taint.apply('s', label, project='same')
        self.assertEqual((effective['ceiling'], effective['projects'], effective['newly_restricted']), ('team', ['other'], True))

    def test_scope_file_at_home_governs_nothing(self):
        context = origin.Context({'project': 'everything', 'root': str(self.home), 'scope_file': str(self.home / '.vaelius')},
                                 str(self.home), {})
        self.assertIsNone(context.root)
        self.assertEqual(context.place(str(self.home / 'notes.txt'))['ceiling'], 'private')
        self.assertIsNone(origin.in_scope_line({'project': 'everything', 'root': str(self.home)}, str(self.home), {}))

    def test_in_scope_line_is_short_and_lists_scoped_siblings(self):
        line = origin.in_scope_line({'project': 'same', 'root': str(self.repo)}, str(self.repo / 'src'), {})
        self.assertEqual(line, 'vaelius: in-scope folders: ~/Documents/ws/repo, ../diff, ../my_lib.v2, ../sib '
                               '(reads elsewhere make this session private)')
        for name in 'abcdefghijklmnopqrstuvwxyz':
            (self.ws / ('sibling-with-a-long-name-' + name)).mkdir()
            (self.ws / ('sibling-with-a-long-name-' + name) / '.vaelius').write_text('project = "same"\n')
        line = origin.in_scope_line({'project': 'same', 'root': str(self.repo)}, str(self.repo / 'src'), {})
        self.assertLessEqual(len(line), origin.SCOPE_LINE_MAX)
        self.assertTrue(line.startswith('vaelius: in-scope folders: ~/Documents/ws/repo, '))

    def test_same_project_recall_is_unchanged_and_narrower_recall_restricts(self):
        team = {'id': 'd', 'provenance': {}, 'origin': {'project': 'same', 'sharing': 'team'}}
        self.assertEqual(origin.recalled([team], 'same', cards=True, sharing='team')['ceiling'], 'organization')
        self.assertEqual(origin.recalled([team], 'same', cards=True, sharing='private')['ceiling'], 'organization')
        self.assertEqual(origin.recalled([team], 'same', cards=True, sharing='organization')['ceiling'], 'team')
        mine = {'id': 'd', 'provenance': {}, 'origin': {'project': 'same', 'sharing': 'private'}}
        self.assertEqual(origin.recalled([mine], 'same', cards=True, sharing='private')['ceiling'], 'private')
        other = {'id': 'd', 'provenance': {}, 'origin': {'project': 'other', 'sharing': 'team'}}
        self.assertEqual(origin.recalled([other], 'same', cards=True, sharing='team')['projects'], ['other'])
