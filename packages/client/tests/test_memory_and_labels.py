"""Auto memory off, remembered items, import-memory and the MCP connection's label.

Synthetic folders under a temporary directory; the service is a stub. Nothing reads
or writes a real Claude Code configuration or home folder.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agentclient import claude_memory, origin, remembered, session_label
import test_claude_code
from test_session_labels import LabelFixture


class AutoMemoryInstallTests(test_claude_code.Profile):
    def setUp(self):
        super().setUp()
        self.claude = Path(self.tmp.name).resolve() / 'claude-config'
        self.claude.mkdir()
        self.settings = self.claude / 'settings.json'
        self.settings.write_text('{}')

    def install(self, **options):
        from agentclient.claude_code import install
        return install(self.home, claude_config_dir=self.claude, **options)

    def local(self):
        return self.project / '.claude' / 'settings.local.json'

    def test_install_switches_it_off_per_project_and_uninstall_restores(self):
        from agentclient.claude_code import uninstall
        result = self.install()
        self.assertEqual(json.loads(self.local().read_text()), {'autoMemoryEnabled': False})
        self.assertEqual(result['auto_memory_off'], [str(self.local())])
        self.assertNotIn('autoMemoryEnabled', json.loads(self.settings.read_text()), 'per repository, not per user')
        self.install()  # idempotent
        self.assertEqual(json.loads(self.local().read_text()), {'autoMemoryEnabled': False})
        uninstall(self.home)
        self.assertFalse((self.project / '.claude').exists(), 'the folder it created is gone again')

    def test_prior_values_come_back_and_a_later_choice_is_kept(self):
        from agentclient.claude_code import uninstall
        self.local().parent.mkdir()
        self.local().write_text(json.dumps({'autoMemoryEnabled': True, 'model': 'synthetic'}))
        self.install()
        self.assertEqual(json.loads(self.local().read_text()), {'autoMemoryEnabled': False, 'model': 'synthetic'})
        uninstall(self.home)
        self.assertEqual(json.loads(self.local().read_text()), {'autoMemoryEnabled': True, 'model': 'synthetic'})
        self.install()
        self.local().write_text(json.dumps({'autoMemoryEnabled': True}))  # the person turns it back on
        self.assertEqual(self.install()['auto_memory_left_on'], [str(self.project)])
        self.assertEqual(json.loads(self.local().read_text()), {'autoMemoryEnabled': True})
        uninstall(self.home)
        self.assertEqual(json.loads(self.local().read_text()), {'autoMemoryEnabled': True})

    def test_project_scope_shares_the_file_with_the_hooks(self):
        from agentclient.claude_code import uninstall
        self.install(scope='project', project_root=self.project)
        value = json.loads(self.local().read_text())
        self.assertIs(value['autoMemoryEnabled'], False)
        self.assertIn('hooks', value)
        uninstall(self.home, scope='project')
        self.assertFalse(self.local().exists())


class AutoMemoryStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / 'repo'
        (self.repo / '.claude').mkdir(parents=True)
        self.env = {'CLAUDE_CONFIG_DIR': str(self.tmp / 'config')}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, True)

    def test_status_line_only_when_it_is_on(self):
        with mock.patch.dict(claude_memory.__dict__), mock.patch('agentclient.claude_guardrails.MANAGED_DIRS', {}):
            self.assertIn('auto memory is on', claude_memory.status_line(self.repo, self.env))
            (self.repo / '.claude/settings.local.json').write_text('{"autoMemoryEnabled": false}')
            self.assertIsNone(claude_memory.status_line(self.repo, self.env))
            # The environment variable wins over the key, in either direction.
            self.assertTrue(claude_memory.enabled(self.repo, dict(self.env, CLAUDE_CODE_DISABLE_AUTO_MEMORY='0')))
            (self.repo / '.claude/settings.local.json').unlink()
            self.assertFalse(claude_memory.enabled(self.repo, dict(self.env, CLAUDE_CODE_DISABLE_AUTO_MEMORY='1')))

    def test_managed_templates_switch_it_off(self):
        folder = Path(__file__).resolve().parents[3] / 'integrations/claude-code/guardrails'
        for name in ('managed-settings.json', 'managed-settings-strict.json'):
            self.assertIs(json.loads((folder / name).read_text())['autoMemoryEnabled'], False, name)


class LabelRecordTests(LabelFixture):
    def headers(self):
        from agentclient import mcp_headers
        backend = mock.Mock(token='synthetic-token')
        with mock.patch('agentclient.transport.enterprise_client', return_value=backend), \
                mock.patch('agentclient.transport.require_backend', return_value={}), \
                mock.patch('agentclient.transport.backend_endpoint', return_value='https://service.test'):
            value = json.loads(mcp_headers.headers(self.home, project_dir=str(self.src / 'a'))['X-Vaelius-Session-Label'])
        self.assertEqual(value.pop('read'), ['team-a'], 'the default read list: the current workspace')
        return value

    def use(self, target):
        with mock.patch('agentclient.transport.enterprise_client', return_value=self.service):
            return session_label.use(self.home, self.config, target, cwd=str(self.src / 'a'))

    def test_the_header_follows_the_session_last_active_in_the_folder(self):
        self.assertEqual(self.headers(), {'workspace': 'team-a'}, 'no record: the folder label')
        self.start()
        self.assertEqual(self.headers(), {'workspace': 'team-a'})
        path = session_label.mcp_label_path(self.home, self.src / 'a')
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.use('narrow-b')
        self.assertEqual(self.headers(), {'workspace': 'team-a', 'narrowed': ['narrow-b']})
        self.use('private')
        self.assertEqual(self.headers(), {'workspace': 'team-a', 'narrowed': ['narrow-b'], 'private': True})
        self.hook('SessionEnd', reason='prompt_input_exit')
        self.assertFalse(path.exists(), 'the session that wrote it ended')
        self.assertEqual(self.headers(), {'workspace': 'team-a'})

    def test_the_taint_backstop_makes_the_record_private(self):
        self.start()
        self.hook('PostToolUse', tool_name='Read', tool_input={'file_path': str(self.home_dir / 'dotfile')},
                  tool_response={})
        self.hook('UserPromptSubmit', prompt='next')
        self.assertIs(self.headers().get('private'), True)

    def test_preferences_and_always_load_items_follow_the_session_label(self):
        card = {'id': 'mem-1', 'revision': 'r1', 'title': 'Style', 'lesson': 'Answer briefly.', 'always_load': True,
                'provenance': {'author': 'you', 'wording': 'original'}, 'origin': {'project': 'team-a', 'sharing': 'private'}}
        answer = self.service.request

        def request(path, value=None):
            if path.endswith('preferences'):
                self.service.calls.append((path, value))
                return {'values': {}, 'preferences': [], 'remembered': [card]}
            return answer(path, value)
        self.service.request = request
        result = self.start()
        sent = [v for p, v in self.service.calls if p.endswith('preferences')][-1]
        self.assertEqual(sent['session_label'], {'workspace': 'team-a', 'narrowed': [], 'private': False,
                                                 'sharing': 'organization', 'read': ['team-a']})
        self.assertIn('Answer briefly.', result['hookSpecificOutput']['additionalContext'])
        # Delivered private memory makes what follows private, so an echo stays with its owner.
        self.assertEqual(origin.SessionOrigins(self.home).get('s1')['ceiling'], 'private')

    def test_a_stale_or_missing_record_falls_back(self):
        self.start()
        self.use('private')
        path = session_label.mcp_label_path(self.home, self.src / 'a')
        value = json.loads(path.read_text())
        path.write_text(json.dumps(dict(value, updated=time.time() - 13 * 3600)))
        self.assertEqual(self.headers(), {'workspace': 'team-a'})
        path.write_text('not json')
        self.assertEqual(self.headers(), {'workspace': 'team-a'})


class NeverWiderTests(unittest.TestCase):
    """The record may only narrow the folder's label: never another workspace, never
    a wider sharing request, never dropping private or a narrowed workspace."""

    def check(self, folder, recorded):
        label = session_label.compose(folder, recorded, now=100.0)
        self.assertEqual(label['workspace'], folder['workspace'])
        self.assertEqual(label.get('sharing'), folder.get('sharing'))
        self.assertTrue(set(folder.get('narrowed', [])) <= set(label.get('narrowed', [])))
        self.assertTrue(label.get('private', False) or not folder.get('private', False))
        return label

    def test_never_wider(self):
        records = [None, {'workspace': 'other', 'private': True, 'narrowed': ['x'], 'updated': 99.0},
                   {'workspace': 'team-a', 'sharing': 'organization', 'private': False, 'narrowed': [],
                    'updated': 99.0},
                   {'workspace': 'team-a', 'private': 'yes', 'narrowed': ['x y', 7], 'updated': 99.0},
                   {'workspace': 'team-a', 'private': True, 'narrowed': ['narrow-b'], 'updated': 99.0}]
        folders = [{'workspace': 'team-a'}, {'workspace': 'team-a', 'sharing': 'private'},
                   {'workspace': 'team-a', 'narrowed': ['c'], 'private': True}]
        for folder in folders:
            for recorded in records:
                with self.subTest(folder=folder, recorded=recorded):
                    self.check(folder, recorded)
        self.assertEqual(session_label.compose({'workspace': 'team-a'}, records[1], now=100.0), {'workspace': 'team-a'},
                         'a record for another workspace is ignored')
        self.assertEqual(session_label.compose({'workspace': 'team-a'}, records[4], now=100.0),
                         {'workspace': 'team-a', 'narrowed': ['narrow-b'], 'private': True})


class Service:
    def __init__(self):
        self.calls = []

    def request(self, path, value=None):
        self.calls.append((path, value))
        return {'disposition': 'accepted', 'document_id': 'd' + str(len(self.calls))}


class ImportMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / 'src' / 'maple'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / '.vaelius').write_text('workspace = "maple"\n')
        self.claude = self.tmp / 'claude-config'
        from agentclient.origin import slug
        self.folder = self.claude / 'projects' / slug(str(self.repo)) / 'memory'
        self.folder.mkdir(parents=True)
        entries = {'user_role.md': ('user', 'Prefers short answers with the command first.'),
                   'feedback_tests.md': ('feedback', 'Run the focused test before the full suite.'),
                   'project_release.md': ('project', 'Releases are cut on Thursdays.\n' * 3),
                   'reference_dash.md': ('reference', 'The deploy dashboard lists the canary hosts.')}
        for name, (kind, body) in entries.items():
            (self.folder / name).write_text('---\nname: ' + name[:-3] + '\ndescription: synthetic\ntype: ' + kind
                                            + '\n---\n\n' + body)
        (self.folder / 'MEMORY.md').write_text('- [User role](user_role.md)\n')
        self.before = {p.name: p.read_text() for p in self.folder.iterdir()}
        self.home = self.tmp / 'profile'
        self.home.mkdir()
        self.config = {'projects': {str(self.repo): 'maple'}, 'sessions': {}}
        self.enterContext(mock.patch.dict(os.environ, {'HOME': str(self.tmp / 'home')}))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, True)

    def test_dry_run_prints_counts_only(self):
        service = Service()
        with mock.patch('agentclient.remembered._client', return_value=service):
            result = remembered.import_memory(self.config, self.home, self.folder)
        self.assertEqual(service.calls, [])
        self.assertEqual(result['counts'], {'entries': 4, 'always_load': 2, 'truncated': 0,
                                            'by_type': {'user': 1, 'feedback': 1, 'project': 1, 'reference': 1}})
        self.assertEqual((result['workspace'], result['personal']), ('maple', False))
        text = json.dumps(result)
        for body in ('short answers', 'focused test', 'Thursdays', 'canary'):
            self.assertNotIn(body, text)

    def test_apply_writes_every_entry_and_never_touches_the_folder(self):
        service = Service()
        with mock.patch('agentclient.remembered._client', return_value=service):
            result = remembered.import_memory(self.config, self.home, self.folder, apply=True)
        self.assertEqual(result['written'], {'accepted': 4, 'duplicate': 0})
        sent = {value['title']: value for _, value in service.calls}
        self.assertEqual({k: v['always_load'] for k, v in sent.items()},
                         {'user_role': True, 'feedback_tests': True, 'project_release': False,
                          'reference_dash': False})
        self.assertTrue(all(v['workspace'] == 'maple' and v['personal'] is False and v['confirm'] is True
                            for v in sent.values()))
        self.assertEqual({p.name: p.read_text() for p in self.folder.iterdir()}, self.before)

    def test_remember_add_uses_this_folders_workspace_and_applies_directly(self):
        service = Service()
        with mock.patch('agentclient.remembered._client', return_value=service):
            with mock.patch('os.getcwd', return_value=str(self.repo)):
                remembered.add(self.config, 'Prefer pytest.', always_load=True)
            with mock.patch('agentclient.remembered.current_workspace', return_value=None), \
                    self.assertRaises(SystemExit):
                remembered.add(self.config, 'Somewhere else.')
        path, value = service.calls[-1]
        self.assertEqual(path, '/enterprise/v3/remember')
        self.assertEqual({k: value[k] for k in ('text', 'workspace', 'always_load', 'personal', 'confirm')},
                         {'text': 'Prefer pytest.', 'workspace': 'maple', 'always_load': True, 'personal': False,
                          'confirm': True})

    def test_an_unmapped_folder_becomes_personal_in_the_named_workspace(self):
        other = self.claude / 'projects' / '-elsewhere' / 'memory'
        other.mkdir(parents=True)
        (other / 'user_x.md').write_text('---\ntype: user\n---\nAlways answer in metric units.')
        service = Service()
        with mock.patch('agentclient.remembered._client', return_value=service):
            self.assertTrue(remembered.import_memory(self.config, self.home, other)['personal'])
            with self.assertRaises(SystemExit):
                remembered.import_memory(self.config, self.home, other, apply=True)
            remembered.import_memory(self.config, self.home, other, workspace='maple', apply=True)
        value = service.calls[-1][1]
        self.assertEqual((value['workspace'], value['personal'], value['always_load']), ('maple', True, True))


if __name__ == '__main__':
    unittest.main()
