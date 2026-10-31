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
            'narrower': [] if 'narrow-b' in narrowed or private else [
                {'id': 'narrow-b', 'name': 'narrow-b', 'hidden_items': 12,
                 'missing_readers': {'count': 4, 'names': ['carol', 'dave', 'contractors (group)'], 'more': 1}}],
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
                                                  'sharing': 'organization', 'read': ['team-a']}))
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
                                                         'sharing': 'organization', 'read': ['team-a']},
                         'recall carries the label, with the default read list (the current workspace)')
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
        self.assertEqual(reason, "vaelius: denied — ~/src/b: narrow-b isn't visible to carol, dave, contractors "
                                 "(group) and 1 more (in this session's audience, team-a). The person can run "
                                 "'vaelius-client session use narrow-b' for this session, or ask narrow-b's admin "
                                 "to add them.")
        self.assertNotIn('\n', reason)
        self.assertIn('cannot read or which does not exist', self.read(self.src / 'ghost/notes.md'))
        self.assertIn('scope file Vaelius cannot read', self.read(self.src / 'bad/notes.md'))
        self.assertIn('personal folder', self.read(self.home_dir / 'Documents/plan.md'), 'unchanged')
        self.assertEqual(self.read(self.home_dir / 'dotfile'),
                         "vaelius: ~/dotfile isn't in a Vaelius folder (no .vaelius). Add one, or run "
                         "'vaelius-client session use private' to read it in a private session.",
                         'unscoped: denied with one line, never ask')
        self.assertIn("~/src/plain/readme.md isn't in a Vaelius folder", self.read(self.src / 'plain/readme.md'),
                      'unscoped repository: denied')

    def test_narrower_denial_names_who_is_missing_from_the_cached_descriptor(self):
        entry = self.label()
        summary = session_label.narrower(entry, 'narrow-b')
        cases = (({'count': 1, 'names': ['carol'], 'more': 0}, 'carol'),
                 ({'count': 2, 'names': ['carol', 'dave'], 'more': 0}, 'carol and dave'),
                 ({'count': 9, 'names': ['carol', 'dave', 'erin'], 'more': 6}, 'carol, dave, erin and 6 more'),
                 ({'count': 3, 'names': [], 'more': 3}, '3 people'),
                 ({'count': 1, 'names': [], 'more': 1}, '1 person'),
                 ({}, ''))
        for missing, expected in cases:
            with self.subTest(expected):
                self.assertEqual(session_label.who(dict(summary, missing_readers=missing)), expected)
        # An older service sends no summary: the earlier denial, still one line and still a deny.
        old = dict(entry['descriptor'])
        old.pop('narrower')
        session_label.SessionLabels(self.home).update('s1', descriptor=old)
        reason = self.read(self.src / 'b/notes.md')
        self.assertIn('is visible to fewer people than this session (team-a)', reason)
        self.assertNotIn('\n', reason)

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
        self.assertIn("isn't in a Vaelius folder", self.check('Grep', {'pattern': 'x', 'path': str(self.src)}),
                      'a search rooted outside every scoped folder is as unknown: denied')
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


class RecallNoteTest(LabelFixture):
    def test_hidden_memories_are_announced_once_per_session_to_the_person_only(self):
        # Only workspaces in the folder's `read` list are reported (default: the current one).
        self.start(session='s0')
        self.assertNotIn('hidden', self.hook('UserPromptSubmit', session='s0', prompt='x').get('systemMessage', ''),
                         'narrow-b is outside the default read list')
        (self.src / 'a/.vaelius').write_text('workspace = "team-a"\nread = ["*"]\n')
        self.start()
        first = self.hook('UserPromptSubmit', prompt='first')
        line = ("vaelius: 12 memories from narrow-b hidden (not visible to carol, dave, contractors (group) and 1 "
                "more); 'vaelius-client session use narrow-b' to include them")
        self.assertIn(line, first.get('systemMessage', ''))
        self.assertNotIn('narrow-b', json.dumps(first.get('hookSpecificOutput', {})), 'never model-facing')
        second = self.hook('UserPromptSubmit', prompt='second')
        self.assertNotIn('hidden', second.get('systemMessage', ''), 'once per session')
        self.start(session='s2')
        self.assertIn(line, self.hook('UserPromptSubmit', session='s2', prompt='other').get('systemMessage', ''))

    def test_no_line_when_nothing_is_hidden_and_several_workspaces_are_summarized(self):
        entry = {'descriptor': {'narrower': [{'id': 'b', 'name': 'b', 'hidden_items': 0,
                                              'missing_readers': {'count': 1, 'names': ['carol'], 'more': 0}}]}}
        self.assertIsNone(session_label.hidden_line(entry))
        entry['descriptor']['narrower'] += [{'id': 'c', 'name': 'c', 'hidden_items': 1, 'missing_readers': {}},
                                            {'id': 'd', 'name': 'd', 'hidden_items': 5,
                                             'missing_readers': {'count': 2, 'names': [], 'more': 2}}]
        self.assertEqual(session_label.hidden_line(entry),
                         "vaelius: 5 memories from d hidden (not visible to 2 people); also 1 from c; "
                         "'vaelius-client session use <workspace>' to include them")


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
        self.assertEqual(json.loads(labelled['X-Vaelius-Session-Label']), {'workspace': 'team-a', 'read': ['team-a']})
        self.assertNotIn('X-Vaelius-Read-Only', labelled)
        self.assertNotIn('X-Vaelius-Session-Label', quiet, 'read-only: no label rule')
        self.assertEqual(quiet['X-Vaelius-Read-Only'], '1', 'a read-only folder: the service refuses writes')


class UnscopedReadTest(LabelFixture):
    """Part 1 of the bundle: an existing path outside every scoped folder is denied, not narrowed."""

    def test_unscoped_reads_are_denied_with_one_line_unless_private_or_read_only(self):
        self.start()
        dotfile = self.home_dir / 'dotfile'
        reason = self.read(dotfile)
        self.assertEqual(reason, session_label.unscoped_reason(dotfile, str(self.home_dir)))
        self.assertNotIn('\n', reason)
        self.assertIn("vaelius-client session use private", reason)
        self.assertTrue(self.check('Bash', {'command': 'cat ' + str(dotfile)}), 'Bash paths too')
        # Allowed categories keep no restriction.
        self.assertIsNone(self.read(self.src / 'a/notes.md'), 'own workspace')
        self.assertIsNone(self.read(self.src / 'c/notes.md'), 'a wider workspace')
        self.assertIsNone(self.read(Path(tempfile.gettempdir()) / 'vaelius-missing-or-temp'), 'temp')
        self.assertIsNone(self.read(Path('/usr/bin/env')), 'system')
        cache = self.home_dir / '.cache/tool/data'
        cache.parent.mkdir(parents=True)
        cache.write_text('synthetic\n')
        self.assertIsNone(self.read(cache), 'tooling and caches')
        self.assertIsNone(self.read(self.home_dir / 'not-yet-created.md'), 'a missing path holds nothing')
        self.assertIn('personal folder', self.read(self.home_dir / 'Documents/plan.md'), 'still denied')
        # The taint backstop (a read the hook could not see) or `use private` reopens it.
        session_label.SessionLabels(self.home).update('s1', private=True)
        self.assertIsNone(self.read(dotfile), 'a private session may read it')
        session_label.SessionLabels(self.home).update('s1', private=False, read_only=True)
        self.assertIsNone(self.read(dotfile), 'a read-only chat captures nothing: no label rule')

    def test_unlabelled_and_read_only_folder_sessions_are_unaffected(self):
        self.start(cwd=self.src / 'quiet')
        self.assertIsNone(self.read(self.home_dir / 'dotfile', cwd=self.src / 'quiet'), 'capture = read-only')
        self.assertIsNone(self.read(self.home_dir / 'dotfile', session='never-started'), 'no label')


class ReadOnlyChatTest(LabelFixture):
    """Part 4: read-only chats."""

    def register_calls(self):
        return [v for p, v in self.service.calls if p == '/enterprise/v3/sessions/read-only']

    def test_session_read_only_is_one_way_stops_capture_and_tells_the_service(self):
        from agentclient import mcp_headers
        self.start()
        self.hook('UserPromptSubmit', prompt='before')
        captured = len(Outbox.events)
        self.assertGreater(captured, 0)
        backend = mock.Mock(token='synthetic-token')
        with mock.patch('agentclient.transport.enterprise_client', return_value=backend), \
                mock.patch('agentclient.transport.require_backend', return_value={}), \
                mock.patch('agentclient.transport.backend_endpoint', return_value='https://service.test'):
            before = mcp_headers.headers(self.home, project_dir=str(self.src / 'a'))
        self.assertNotIn('X-Vaelius-Read-Only', before)
        with mock.patch('agentclient.transport.enterprise_client', return_value=self.service):
            result = session_label.read_only(self.home, self.config, cwd=str(self.src / 'a'))
        self.assertEqual(result['status'], session_label.READ_ONLY_STATUS + session_label.READ_ONLY_FORGET)
        self.assertIn("vaelius-client forget --session current", result['status'])
        self.assertEqual(self.register_calls(), [{'sessions': ['s1', before['X-AgentNetwork-Session']]}],
                         'the chat and its MCP connection are refused writes by the service')
        first = self.hook('UserPromptSubmit', prompt='after')
        self.assertIn(session_label.READ_ONLY_STATUS, first.get('systemMessage', ''), 'announced once')
        self.hook('PostToolUse', tool_name='Read', tool_input={'file_path': str(self.src / 'a/notes.md')},
                  tool_response={})
        self.assertEqual(len(Outbox.events), captured, 'nothing captured after the switch')
        self.hook('PostCompact', trigger='auto')
        self.assertNotIn('/enterprise/v1/boundary', [p for p, _ in self.service.calls], 'no boundary write')
        with mock.patch('agentclient.transport.enterprise_client', return_value=backend), \
                mock.patch('agentclient.transport.require_backend', return_value={}), \
                mock.patch('agentclient.transport.backend_endpoint', return_value='https://service.test'):
            after = mcp_headers.headers(self.home, project_dir=str(self.src / 'a'))
        self.assertEqual(after['X-Vaelius-Read-Only'], '1', 'a reconnect carries read-only')
        self.assertIs(json.loads(after['X-Vaelius-Session-Label'])['read_only'], True)
        self.assertTrue(session_label.was_read_only(self.home, 's1'))
        self.hook('SessionEnd', reason='clear')
        self.assertTrue(session_label.was_read_only(self.home, 's1'), 'recorded past /clear for push-session')
        self.start(session='s2')
        self.assertFalse(session_label.SessionLabels(self.home).get('s2').get('read_only'), 'a new chat writes')

    def test_the_mcp_tool_result_switches_the_chat(self):
        self.start()
        self.hook('PostToolUse', tool_name='mcp__plugin_vaelius_vaelius__read_only_session', tool_input={},
                  tool_response=[{'type': 'text', 'text': json.dumps({'vaelius_session_read_only': True})}])
        self.assertTrue(self.label().get('read_only'))
        self.assertEqual(self.register_calls()[-1]['sessions'][0], 's1')
        captured = len(Outbox.events)
        self.hook('UserPromptSubmit', prompt='after')
        self.assertEqual(len(Outbox.events), captured)

    def test_a_read_only_folder_registers_its_chat(self):
        self.start(session='q1', cwd=self.src / 'quiet')
        self.assertEqual(self.register_calls(), [{'sessions': ['q1']}])


if __name__ == '__main__':
    unittest.main()
