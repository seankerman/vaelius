"""Fixed session labels, client side (docs/ACCESS_MODEL.md section 7, PR B).

A synthetic home folder with workspace folders `team-a` (the session's), `org-c`
(an organization workspace whose audience contains team-a's), `narrow-b` (fewer
people), `ghost` (a workspace the service does not know) and a scope file that does
not parse. The service is a stub: these tests cover the label, the descriptor cache,
the PreToolUse decision, explicit narrowing and the taint backstop.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest
from unittest import mock

from agentclient import claude_guardrails, origin, session_label


def descriptor(narrowed=(), private=False, *, now=None, role='writer', epoch=7):
    now = time.time() if now is None else now
    contains = [{'id': 'team-a', 'name': 'team-a'}, {'id': 'org-c', 'name': 'org-c'}]
    if 'narrow-b' in narrowed:
        contains.append({'id': 'narrow-b', 'name': 'narrow-b'})
    return {'workspace': {'id': 'team-a', 'name': 'team-a'}, 'epoch': epoch, 'role': role,
            'item_default': 'shared', 'contributions_default': 'shared',
            'session': {'workspace': 'team-a', 'narrowed': list(narrowed), 'private': private},
            'contains_session_audience': contains,
            'readable': [{'id': n, 'name': n} for n in ('narrow-b', 'org-c', 'team-a')],
            'repos': [], 'fetched_at': int(now), 'refresh_after': int(now + 900), 'expires_at': int(now + 86400)}


class Service:
    """The descriptor route and search, recording every request."""

    def __init__(self, test):
        self.test = test
        self.calls = []
        self.refuse = set()
        self.epoch = 7
        self.role = 'writer'

    def request(self, path, value=None):
        self.calls.append((path, value))
        if path == '/enterprise/v3/workspaces/descriptor':
            if set(value.get('narrowed', [])) & self.refuse:
                from urllib.error import HTTPError
                raise HTTPError('https://service.test' + path, 403, 'denied', {}, None)
            return descriptor(value.get('narrowed', []), value.get('private', False), role=self.role, epoch=self.epoch)
        if path.endswith('preferences'):
            return {'values': {}, 'preferences': []}
        if path.endswith('search'):
            return {'answerable': False, 'results': [], 'session_epoch': self.epoch}
        if path.endswith('capture-notice'):
            raise OSError('unavailable')
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


class LabelFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home_dir = self.tmp / 'home'
        self.src = self.home_dir / 'src'
        scopes = {'a': 'workspace = "team-a"\n', 'a-docs': 'workspace = "team-a"\n', 'b': 'workspace = "narrow-b"\n',
                  'c': 'workspace = "org-c"\n', 'ghost': 'workspace = "ghost"\n', 'bad': 'workspace = [\n',
                  'quiet': 'workspace = "team-a"\ncapture = "read-only"\n'}
        for name, text in scopes.items():
            (self.src / name / '.git').mkdir(parents=True)
            (self.src / name / '.vaelius').write_text(text)
            (self.src / name / 'notes.md').write_text('synthetic\n')
        (self.src / 'plain/.git').mkdir(parents=True)
        (self.src / 'plain/readme.md').write_text('synthetic\n')
        (self.home_dir / 'Documents').mkdir()
        (self.home_dir / 'Documents/plan.md').write_text('synthetic\n')
        (self.home_dir / 'dotfile').write_text('synthetic\n')
        self.enterContext(mock.patch.dict(os.environ, {'HOME': str(self.home_dir)}))
        for name in session_label.SESSION_ENV:
            self.enterContext(mock.patch.dict(os.environ, {name: ''}))
        self.home = self.tmp / 'profile'
        self.home.mkdir()
        self.config = {'projects': {}, 'sessions': {}, 'knowledge_backend': {
            'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2', 'capture_owner': 'hooks',
            'api_version': 'cloud-local-1', 'connection_id': 'synthetic-connection'}}
        (self.home / 'config.json').write_text(json.dumps(self.config))
        self.service = Service(self)
        Outbox.events = []

    def hook(self, kind, session='s1', cwd=None, **event):
        from agentclient.hooks import handle
        with mock.patch('agentclient.transport.enterprise_client', return_value=self.service), \
                mock.patch('agentclient.capture_outbox.Outbox', Outbox):
            return handle(self.home, dict({'hook_event_name': kind, 'cwd': str(cwd or self.src / 'a'),
                                           'session_id': session}, **event), host='claude-code')

    def start(self, session='s1', cwd=None):
        return self.hook('SessionStart', session=session, cwd=cwd, source='startup')

    def check(self, tool, tool_input, session='s1', cwd=None):
        event = {'hook_event_name': 'PreToolUse', 'session_id': session, 'cwd': str(cwd or self.src / 'a'),
                 'tool_name': tool, 'tool_input': tool_input}
        result = claude_guardrails.check(event, 'standard', env={}, home=self.home)
        output = result.get('hookSpecificOutput', {})
        self.assertIn(output.get('permissionDecision'), (None, 'deny'), 'never ask')
        return output.get('permissionDecisionReason')

    def read(self, path, **extra):
        return self.check('Read', {'file_path': str(path)}, **extra)

    def label(self, session='s1'):
        return session_label.SessionLabels(self.home).get(session)


class SessionLabelTest(LabelFixture):
    def test_label_is_taken_at_session_start_from_the_starting_folder(self):
        self.start()
        entry = self.label()
        self.assertEqual((entry['workspace'], entry['narrowed'], entry['private']), ('team-a', [], False))
        self.assertEqual(entry['descriptor']['session']['workspace'], 'team-a')
        self.assertEqual(self.service.calls[0], ('/enterprise/v3/workspaces/descriptor',
                                                 {'workspace': 'team-a', 'narrowed': [], 'private': False,
                                                  'sharing': 'organization'}))
        cached = json.loads((self.home / 'workspaces/team-a.json').read_text())
        self.assertEqual(cached['workspace']['id'], 'team-a', 'cached in the client profile')
        self.assertFalse((self.src / 'a/.vaelius').read_text().count('contains'), 'never in the scope file')
        self.assertEqual(oct((self.home / 'session-labels.json').stat().st_mode & 0o777), '0o600')
        # The label is fixed: a prompt from another folder does not change it.
        self.hook('UserPromptSubmit', cwd=self.src / 'c', prompt='hello')
        self.assertEqual(self.label()['workspace'], 'team-a')

    def test_read_only_off_and_unscoped_sessions_get_no_label(self):
        for session, cwd in (('ro', self.src / 'quiet'), ('plain', self.src / 'plain'), ('home', self.home_dir)):
            with self.subTest(cwd=cwd.name):
                self.start(session, cwd)
                self.assertIsNone(self.label(session))
                self.assertIsNone(self.read(self.src / 'b/notes.md', session=session, cwd=cwd),
                                  'no label rule without a label')
        self.assertIn('personal folder', self.read(self.home_dir / 'Documents/plan.md', session='home',
                                                   cwd=self.src / 'plain'), 'the profile still applies')

    def test_descriptor_refreshes_after_refresh_after_and_on_a_newer_epoch(self):
        self.start()
        fetched = len(self.service.calls)
        self.hook('UserPromptSubmit', prompt='hello')
        self.assertEqual([p for p, _ in self.service.calls[fetched:]].count('/enterprise/v3/workspaces/descriptor'), 0,
                         'fresh: no refresh on a prompt')
        session_label.SessionLabels(self.home).update('s1', descriptor=dict(self.label()['descriptor'],
                                                                             refresh_after=time.time() - 1))
        self.hook('UserPromptSubmit', prompt='hello again')
        self.assertIn('/enterprise/v3/workspaces/descriptor', [p for p, _ in self.service.calls[fetched:]])
        self.assertGreater(self.label()['descriptor']['refresh_after'], time.time())
        searched = [v for p, v in self.service.calls if p == '/enterprise/v3/search']
        self.assertEqual(searched[-1]['session_label'], {'workspace': 'team-a', 'narrowed': [], 'private': False,
                                                         'sharing': 'organization'}, 'recall carries the label')
        self.service.epoch = 9
        before = len(self.service.calls)
        self.hook('UserPromptSubmit', prompt='what changed?')
        self.assertEqual(self.label()['descriptor']['epoch'], 9, 'a newer epoch in a search answer refreshes it')
        self.assertIn('/enterprise/v3/workspaces/descriptor', [p for p, _ in self.service.calls[before:]])

    def test_a_role_downgrade_turns_capture_off_with_a_status_line(self):
        self.start()
        self.hook('UserPromptSubmit', prompt='captured')
        self.assertEqual(len(Outbox.events), 1)
        self.service.role = 'reader'
        self.service.epoch = 8
        self.hook('UserPromptSubmit', prompt='refresh me')
        result = self.hook('UserPromptSubmit', prompt='not captured')
        self.assertEqual(len(Outbox.events), 2, 'the prompt that saw the newer epoch was the last capture')
        self.assertIn('vaelius: you are now a reader in team-a — capture is off', result.get('systemMessage', ''))


class HookDecisionTest(LabelFixture):
    def setUp(self):
        super().setUp()
        self.start()

    def test_same_superset_narrower_organization_personal_and_unknown(self):
        self.assertIsNone(self.read(self.src / 'a/notes.md'), 'own folder')
        self.assertIsNone(self.read(self.src / 'a-docs/notes.md'), 'another folder of the same workspace')
        self.assertIsNone(self.read(self.src / 'c/notes.md'), 'an organization workspace contains the session')
        reason = self.read(self.src / 'b/notes.md')
        self.assertEqual(reason, 'vaelius: denied — ~/src/b (workspace "narrow-b") is visible to fewer people than '
                                 'this session (team-a). Start a session there, or ask the person to run '
                                 '`vaelius-client session use narrow-b`; capture is then limited to people in both.')
        self.assertNotIn('\n', reason)
        self.assertIn('cannot read or which does not exist', self.read(self.src / 'ghost/notes.md'))
        self.assertIn('scope file Vaelius cannot read', self.read(self.src / 'bad/notes.md'))
        self.assertIn('personal folder', self.read(self.home_dir / 'Documents/plan.md'), 'unchanged')
        self.assertIsNone(self.read(self.home_dir / 'dotfile'), 'unscoped: allowed, the backstop applies')
        self.assertIsNone(self.read(self.src / 'plain/readme.md'), 'unscoped repository: allowed')

    def test_every_read_tool_and_shell_paths(self):
        b = str(self.src / 'b')
        self.assertTrue(self.check('Grep', {'pattern': 'x', 'path': b}))
        self.assertTrue(self.check('Glob', {'pattern': b + '/**/*.md'}))
        self.assertTrue(self.check('LSP', {'filePath': b + '/notes.md'}))
        self.assertTrue(self.check('Bash', {'command': 'cat ' + b + '/notes.md'}))
        self.assertTrue(self.check('Bash', {'command': 'cd ../b && grep -r token .'}))
        self.assertTrue(self.check('Bash', {'command': "python -c \"open('" + b + "/notes.md')\""}))
        self.assertIsNone(self.check('Bash', {'command': 'cat notes.md && ls ../c'}))
        self.assertIsNone(self.check('Bash', {'command': 'make test'}), 'names nothing: the backstop')
        self.assertIsNone(self.check('Grep', {'pattern': 'x', 'path': str(self.src)}),
                          'a search rooted outside every scoped folder is as unknown')
        self.assertIsNone(self.check('Edit', {'file_path': b + '/notes.md', 'old_string': 'a', 'new_string': 'b'}),
                          'edits are not policed by the label')

    def test_private_session_reads_every_readable_workspace(self):
        origin.SessionOrigins(self.home).apply('s1', origin.Label('private', ('outside_repo',), 'outside_repo'),
                                               project='team-a')
        self.assertIsNone(self.read(self.src / 'b/notes.md'), 'audience {me}: b contains it')
        self.assertTrue(self.read(self.src / 'ghost/notes.md'), 'still not an unreadable workspace')

    def test_an_expired_descriptor_keeps_only_the_sessions_own_workspaces(self):
        old = descriptor(now=time.time() - 2 * 86400)
        session_label.SessionLabels(self.home).update('s1', descriptor=old)
        self.assertIsNone(self.read(self.src / 'a-docs/notes.md'))
        self.assertIn('could not be refreshed', self.read(self.src / 'c/notes.md'))

    def test_the_hook_makes_no_network_call(self):
        def refuse(*args, **kwargs):
            raise AssertionError('network call from the PreToolUse hook')
        with mock.patch.object(socket, 'socket', refuse), mock.patch.object(socket, 'create_connection', refuse), \
                mock.patch('urllib.request.urlopen', refuse):
            self.assertTrue(self.read(self.src / 'b/notes.md'))
            self.assertIsNone(self.read(self.src / 'c/notes.md'))

    def test_cli_entry_point_reads_the_profile_without_creating_it(self):
        import io
        import sys
        from agentclient.cli import main
        event = {'hook_event_name': 'PreToolUse', 'session_id': 's1', 'cwd': str(self.src / 'a'),
                 'tool_name': 'Read', 'tool_input': {'file_path': str(self.src / 'b/notes.md')}}
        for home, denied in ((self.home, True), (self.tmp / 'no-profile', False)):
            stdin = mock.Mock(buffer=io.BytesIO(json.dumps(event).encode()))
            with mock.patch.object(sys, 'stdin', stdin), mock.patch('sys.stdout', new=io.StringIO()) as out:
                main(['--home', str(home), 'guardrail-check', '--profile', 'standard'])
            decision = json.loads(out.getvalue()).get('hookSpecificOutput', {}).get('permissionDecision')
            self.assertEqual(decision, 'deny' if denied else None)
        self.assertFalse((self.tmp / 'no-profile').exists())


class NarrowingTest(LabelFixture):
    def setUp(self):
        super().setUp()
        self.start()

    def use(self, target, cwd=None, **extra):
        with mock.patch('agentclient.transport.enterprise_client', return_value=self.service):
            return session_label.use(self.home, self.config, target, cwd=str(cwd or self.src / 'a'), **extra)

    def test_use_narrows_persists_shows_and_resets_on_clear(self):
        self.assertTrue(self.read(self.src / 'b/notes.md'))
        result = self.use('narrow-b')
        self.assertEqual(result['status'], 'vaelius: session uses team-a ∩ narrow-b — capture limited to people in both')
        self.assertEqual(self.service.calls[-1][1]['narrowed'], ['narrow-b'], 'the service checks the target')
        self.assertIsNone(self.read(self.src / 'b/notes.md'), 'B is now readable')
        start = self.hook('UserPromptSubmit', prompt='go on')
        self.assertIn('vaelius: session uses team-a ∩ narrow-b', start.get('systemMessage', ''), 'status line')
        self.assertEqual(Outbox.events[-1]['origin_label']['projects'], ['narrow-b'], 'capture follows it')
        self.hook('PostCompact', trigger='auto')
        resumed = self.hook('SessionStart', source='compact')
        self.assertEqual(self.label()['narrowed'], ['narrow-b'], 'compaction keeps it')
        self.assertIn('team-a ∩ narrow-b', resumed.get('systemMessage', ''))
        refreshed = [v for p, v in self.service.calls if p.endswith('descriptor')][-1]
        self.assertEqual(refreshed['narrowed'], ['narrow-b'], 'the refresh asks for the narrowed label')
        self.hook('SessionEnd', reason='clear')
        self.assertIsNone(self.label())
        self.start('s2')
        self.assertEqual(self.label('s2')['narrowed'], [], 'a new session starts over')
        self.assertTrue(self.read(self.src / 'b/notes.md', session='s2'))

    def test_use_by_folder_and_private(self):
        self.assertEqual(self.use(str(self.src / 'b'))['narrowed'], ['narrow-b'])
        result = self.use('private')
        self.assertTrue(result['private'])
        self.assertIsNone(self.read(self.src / 'b/notes.md'))
        self.hook('Stop', last_assistant_message='done')
        self.assertEqual(Outbox.events[-1]['origin_label']['ceiling'], 'private')

    def test_use_is_refused_without_a_label_or_without_access(self):
        self.start('plain', self.src / 'plain')
        with self.assertRaises(session_label.UseRefused):
            self.use('narrow-b', cwd=self.src / 'plain')
        with self.assertRaises(session_label.UseRefused):
            self.use('narrow-b', session='unknown-session')
        self.service.refuse.add('ghost')
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError):
            self.use('ghost')
        self.assertEqual(self.label()['narrowed'], [])

    def test_mcp_use_workspace_result_is_applied_by_the_hook(self):
        response = [{'type': 'text', 'text': json.dumps({'vaelius_session_use': {
            'target': 'narrow-b', 'private': False, 'workspace': 'team-a'}, 'status': 'x'})}]
        self.hook('PostToolUse', tool_name='mcp__vaelius__use_workspace', tool_input={'workspace': 'narrow-b'},
                  tool_response=response)
        self.assertEqual(self.label()['narrowed'], ['narrow-b'])
        self.assertIsNone(self.read(self.src / 'b/notes.md'))
        # Another server's result never narrows.
        self.hook('PostToolUse', tool_name='mcp__other__use_workspace', tool_input={'workspace': 'ghost'},
                  tool_response=[{'type': 'text', 'text': json.dumps({'vaelius_session_use': {'target': 'ghost'}})}])
        self.assertEqual(self.label()['narrowed'], ['narrow-b'])


class BackstopTest(LabelFixture):
    def setUp(self):
        super().setUp()
        self.start()

    def tool(self, path):
        self.hook('PostToolUse', tool_name='Read', tool_input={'file_path': str(path)},
                  tool_response={'type': 'text', 'file': {'filePath': str(path), 'content': 'synthetic'}},
                  tool_use_id='t' + str(len(Outbox.events)))
        return Outbox.events[-1].get('origin_label')

    def test_an_allowed_read_carries_its_workspace_and_never_narrows(self):
        label = self.tool(self.src / 'c/notes.md')
        self.assertEqual((label['ceiling'], label['projects']), ('organization', ['org-c']),
                         'carried, as PR A records it')
        self.assertNotEqual(origin.SessionOrigins(self.home).get('s1')['ceiling'], 'private')
        label = self.tool(self.src / 'a-docs/notes.md')
        self.assertEqual((label['ceiling'], label['projects']), ('organization', ['org-c']), 'still only carried')

    def test_reads_the_hook_cannot_classify_still_make_the_session_private(self):
        label = self.tool(self.home_dir / 'dotfile')
        self.assertEqual(label['ceiling'], 'private')
        self.hook('PostToolUse', tool_name='mcp__mail__search', tool_input={}, tool_response='synthetic')
        self.assertEqual(Outbox.events[-1]['origin_label']['ceiling'], 'private')

    def test_labelled_recall_carries_workspaces_without_narrowing(self):
        cards = [{'id': 'd1', 'revision': 'r1', 'title': 't', 'lesson': 'l', 'provenance': {'author': 'a'},
                  'origin': {'project': 'org-c', 'sharing': 'team'}}]
        label = origin.recalled(cards, 'team-a', cards=True, sharing='organization', labelled=True)
        self.assertEqual((label['ceiling'], label['projects']), ('organization', ['org-c']))
        self.assertEqual(origin.recalled(cards, 'team-a', cards=True, sharing='organization')['ceiling'], 'team',
                         'an unlabelled client keeps the #32 rule')


class HeaderTest(LabelFixture):
    def test_mcp_header_helper_sends_the_folder_label(self):
        from agentclient import mcp_headers
        backend = mock.Mock(token='synthetic-token')
        with mock.patch('agentclient.transport.enterprise_client', return_value=backend), \
                mock.patch('agentclient.transport.require_backend', return_value={}), \
                mock.patch('agentclient.transport.backend_endpoint', return_value='https://service.test'):
            labelled = mcp_headers.headers(self.home, project_dir=str(self.src / 'a'))
            quiet = mcp_headers.headers(self.home, project_dir=str(self.src / 'quiet'))
        self.assertEqual(json.loads(labelled['X-Vaelius-Session-Label']), {'workspace': 'team-a'})
        self.assertNotIn('X-Vaelius-Session-Label', quiet, 'read-only: no label rule')


if __name__ == '__main__':
    unittest.main()
