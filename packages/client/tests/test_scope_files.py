"""Repository scope files: discovery, parsing and the capture/recall modes they request."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from agentclient import scope_file
from agentclient.scope_file import discover, parse, ScopeFileError


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME='Example', GIT_AUTHOR_EMAIL='example@example.invalid',
                            GIT_COMMITTER_NAME='Example', GIT_COMMITTER_EMAIL='example@example.invalid'))


class ScopeFileDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / 'repo'
        (self.repo / 'pkg/deep/er').mkdir(parents=True)
        (self.repo / '.git').mkdir()

    def write(self, path, text):
        Path(path).write_text(text)

    def test_nested_directory_finds_repository_root_file(self):
        self.write(self.repo / '.vaelius', 'project = "example-repo"\ncapture = "read-only"\nsharing = "team"\n')
        found = discover(self.repo / 'pkg/deep/er')
        self.assertEqual((found['project'], found['capture'], found['sharing']), ('example-repo', 'read-only', 'team'))
        self.assertEqual(found['root'], str(self.repo))

    def test_no_file_means_no_scope_and_walk_stops_at_git_root(self):
        self.assertIsNone(discover(self.repo / 'pkg'))
        # A file above the Git root does not govern the repository.
        self.write(self.tmp / '.vaelius', 'project = "outer"\n')
        self.assertIsNone(discover(self.repo / 'pkg'))

    def test_directory_with_scope_name_is_not_a_scope_file(self):
        (self.repo / '.vaelius').mkdir()
        self.assertIsNone(discover(self.repo / 'pkg'))

    def test_defaults_and_invalid_values_fail_closed(self):
        self.write(self.repo / '.vaelius', 'project = "example-repo"\n')
        self.assertEqual(discover(self.repo)['capture'], 'on')
        self.assertEqual(discover(self.repo)['sharing'], 'private')
        for text in ('project = "example-repo"\ncapture = "always"\n', 'sharing = "everyone"\n',
                     'project = "../escape"\n', 'project = example-repo\n', '\xff'):
            self.write(self.repo / '.vaelius', text)
            found = discover(self.repo)
            self.assertEqual(found['capture'], 'off', text)
            self.assertIn('error', found)

    def test_legacy_name_needs_explicit_configuration_and_maps_only_known_fields(self):
        self.write(self.repo / '.mindshare', 'mode: read-only\ntags: [alpha, beta]\nteam: example-team\nsearch:\n  tags:\n    scope: [x]\n')
        self.assertIsNone(discover(self.repo, scope_file.scope_names({})))
        names = scope_file.scope_names({'scope_files': ['.vaelius', '.mindshare']})
        found = discover(self.repo, names)
        self.assertEqual((found['project'], found['capture'], found['sharing']), (None, 'read-only', 'private'))
        self.write(self.repo / '.mindshare', 'mode: disabled\n')
        self.assertEqual(discover(self.repo, names)['capture'], 'off')
        # The primary name wins when both exist.
        self.write(self.repo / '.vaelius', 'project = "example-repo"\n')
        self.assertEqual(discover(self.repo, names)['project'], 'example-repo')

    def test_scope_files_option_is_validated(self):
        for bad in ([], ['../x'], ['a/b'], ['.git'], 'x', [1]):
            with self.assertRaises(ScopeFileError):scope_file.scope_names({'scope_files': bad})


@unittest.skipUnless(shutil.which('git'), 'git required')
class WorktreeScopeTest(unittest.TestCase):
    def test_linked_worktree_resolves_to_the_same_request(self):
        tmp = Path(tempfile.mkdtemp()).resolve();self.addCleanup(shutil.rmtree, tmp, True)
        main = tmp / 'main';main.mkdir();(main / 'src').mkdir()
        git('init', '-q', cwd=main)
        (main / 'src/a.txt').write_text('a\n');(main / '.vaelius').write_text('project = "example-repo"\nsharing = "team"\n')
        git('add', 'src/a.txt', cwd=main);git('commit', '-qm', 'init', cwd=main)
        # The file is committed in one worktree and only present in the main checkout here.
        git('worktree', 'add', '-q', str(tmp / 'wt'), cwd=main)
        found = discover(tmp / 'wt/src')
        self.assertEqual((found['project'], found['sharing']), ('example-repo', 'team'))
        self.assertEqual(found['root'], str(main))
        # A file checked into the worktree itself is used directly.
        (tmp / 'wt/.vaelius').write_text('project = "example-repo"\ncapture = "off"\n')
        self.assertEqual(discover(tmp / 'wt/src')['capture'], 'off')


class Backend:
    def __init__(self):
        self.calls = []

    def request(self, path, value=None):
        self.calls.append(path)
        if path.endswith('capture-notice'):
            raise OSError('service unavailable')
        if path.endswith('preferences'):
            return {'values': {}, 'preferences': []}
        if path.endswith('search'):
            return {'answerable': True, 'support': 'complete', 'results': [
                {'id': 'doc-1', 'revision': 'rev-1', 'title': 'Synthetic', 'lesson': 'Synthetic lesson.',
                 'evidence_status': 'discovery_only'}]}
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


class ScopeHookTest(unittest.TestCase):
    """The hook applies the scope file's capture mode and sharing request."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'profile';self.home.mkdir()
        self.repo = self.tmp / 'repo';(self.repo / '.git').mkdir(parents=True);(self.repo / 'src').mkdir()
        self.config = {'projects': {}, 'sessions': {}, 'knowledge_backend': {
            'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2', 'capture_owner': 'hooks',
            'api_version': 'cloud-local-1', 'connection_id': 'synthetic-connection'}}
        self.write()
        Outbox.events = []

    def write(self, **changes):
        (self.home / 'config.json').write_text(json.dumps(dict(self.config, **changes)))

    def scope(self, text, name='.vaelius'):
        (self.repo / name).write_text(text)

    def hook(self, kind='UserPromptSubmit', cwd=None, **event):
        from agentclient.hooks import handle
        backend = Backend()
        with mock.patch('agentclient.transport.enterprise_client', return_value=backend), \
                mock.patch('agentclient.capture_outbox.Outbox', Outbox):
            result = handle(self.home, dict({'hook_event_name': kind, 'cwd': str(cwd or self.repo / 'src'),
                                             'session_id': 's1', 'prompt': 'Where is the dataset?'}, **event))
        return result, backend

    def recalled(self, result):
        return 'additionalContext' in result.get('hookSpecificOutput', {})

    def test_no_scope_file_means_no_capture_and_no_recall(self):
        result, backend = self.hook()
        self.assertEqual((result, backend.calls, Outbox.events), ({}, [], []))

    def test_scope_file_enrolls_forward_only_captures_and_requests_sharing(self):
        self.scope('project = "example-repo"\nsharing = "team"\n')
        result, backend = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertEqual([e['project'] for e in Outbox.events], ['example-repo'])
        self.assertEqual(Outbox.events[0]['sharing'], 'team')
        enrolled = json.loads((self.home / 'scope-enrollments.json').read_text())
        self.assertEqual(enrolled[str(self.repo)]['project'], 'example-repo')
        self.assertEqual(enrolled[str(self.repo)]['status'], 'enrolled_forward_only')
        # Hooks never rewrite config.json for a scope-file enrollment.
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['projects'], {})

    def test_private_request_is_not_sent(self):
        self.scope('project = "example-repo"\n')
        self.hook()
        self.assertNotIn('sharing', Outbox.events[0])

    def test_read_only_recalls_but_does_not_capture(self):
        self.scope('project = "example-repo"\ncapture = "read-only"\nsharing = "team"\n')
        result, backend = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertIn('/enterprise/v3/search', backend.calls)
        self.assertEqual(Outbox.events, [])
        status, _ = self.hook('SessionStart')
        self.assertIn('read-only', status['systemMessage'])
        self.assertEqual(Outbox.events, [])

    def test_off_neither_captures_nor_recalls_even_when_explicitly_enrolled(self):
        self.scope('project = "example-repo"\ncapture = "off"\n')
        self.assertEqual(self.hook(), ({}, mock.ANY))
        self.assertEqual(Outbox.events, [])
        self.write(projects={str(self.repo): 'example-repo'})
        result, backend = self.hook()
        self.assertEqual((result, backend.calls, Outbox.events), ({}, [], []))
        # An invalid file fails closed in the same way.
        self.scope('project = "example-repo"\ncapture = "sometimes"\n')
        result, backend = self.hook()
        self.assertEqual((result, backend.calls, Outbox.events), ({}, [], []))

    def test_explicit_enrollment_keeps_working_and_wins_on_project_name(self):
        self.write(projects={str(self.repo): 'explicit-name'})
        self.hook()
        self.assertEqual(Outbox.events[-1]['project'], 'explicit-name')
        self.assertNotIn('sharing', Outbox.events[-1])
        # A file naming another project cannot redirect capture or request sharing.
        self.scope('project = "other-name"\nsharing = "team"\n')
        self.hook()
        self.assertEqual(Outbox.events[-1]['project'], 'explicit-name')
        self.assertNotIn('sharing', Outbox.events[-1])
        # A matching (or project-less legacy) file sets the mode and request.
        self.scope('project = "explicit-name"\nsharing = "team"\n')
        self.hook()
        self.assertEqual(Outbox.events[-1]['sharing'], 'team')

    def test_capture_disabled_and_pause_win_over_the_file(self):
        self.scope('project = "example-repo"\n')
        self.write(capture_disabled_projects=['example-repo'])
        self.assertEqual(self.hook()[0], {})
        self.write(paused=True)
        self.hook()
        self.assertEqual(Outbox.events, [])

    def test_legacy_scope_name_through_configuration(self):
        self.scope('mode: read-only\nteam: example-team\n', '.mindshare')
        self.write(projects={str(self.repo): 'example-repo'})
        self.hook()
        self.assertEqual(len(Outbox.events), 1)  # legacy name not configured: explicit capture as before
        self.write(projects={str(self.repo): 'example-repo'}, scope_files=['.vaelius', '.mindshare'])
        result, _ = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertEqual(len(Outbox.events), 1)  # read-only now applies

    def test_nested_directory_resolves_the_repository_scope(self):
        self.scope('project = "example-repo"\n')
        (self.repo / 'src/a/b').mkdir(parents=True)
        self.hook(cwd=self.repo / 'src/a/b')
        self.assertEqual(Outbox.events[0]['project'], 'example-repo')

    def test_scope_status_command(self):
        from agentclient.cli import main
        self.scope('project = "example-repo"\ncapture = "read-only"\n')
        import contextlib
        import io
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            main(['--home', str(self.home), 'scope-status', '--cwd', str(self.repo / 'src')])
        value = json.loads(stream.getvalue())
        self.assertEqual(value['scope']['project'], 'example-repo')
        self.assertEqual(value['scope']['capture'], 'read-only')


class DesktopScopeTest(unittest.TestCase):
    """Explicitly mapped desktop sessions honor their repository's scope file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'profile';self.home.mkdir()
        self.repo = self.tmp / 'repo';(self.repo / '.git').mkdir(parents=True)
        self.sessions = self.tmp / 'sessions';self.sessions.mkdir()
        self.session = 'synthetic-session'
        self.transcript = self.sessions / ('rollout-' + self.session + '.jsonl')
        self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': self.session, 'cwd': str(self.repo)}}) + '\n')
        self.config = {'sessions': {self.session: 'example-repo'},
                       'desktop_transcripts': {self.session: str(self.transcript)},
                       'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2',
                           'capture_owner': 'desktop', 'api_version': 'cloud-local-1',
                           'connection_id': 'synthetic-connection'}}
        Outbox.events = []

    def poll(self):
        from agentclient.enterprise_desktop import poll
        with mock.patch('agentclient.transport.enterprise_client', return_value=Backend()), \
                mock.patch('agentclient.capture_outbox.Outbox', Outbox):
            return poll(self.home, self.config, allowed_root=self.sessions)

    def append_prompt(self, ident):
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'timestamp': '2026-10-01T00:00:00Z', 'payload': {
                'type': 'item_completed', 'thread_id': self.session, 'turn_id': 't',
                'item': {'id': ident, 'type': 'UserMessage',
                         'content': [{'type': 'input_text', 'text': 'Synthetic prompt.'}]}}}) + '\n')

    def test_read_only_scope_records_a_gap_and_team_request_is_tagged(self):
        self.poll()  # forward-only: the first poll only sets the cursor
        (self.repo / '.vaelius').write_text('project = "example-repo"\ncapture = "read-only"\n')
        self.append_prompt('one');self.poll()
        self.assertEqual([e['disposition'] for e in Outbox.events], ['incomplete'])
        self.assertEqual(Outbox.events[0]['blocks'], [{'type': 'record_fields',
                                                       'value': {'capture_gap': 'scope_file_capture_read-only'}}])
        (self.repo / '.vaelius').write_text('project = "example-repo"\nsharing = "team"\n')
        self.append_prompt('two');self.poll()
        self.assertEqual(Outbox.events[-1]['blocks'][0]['value'], 'Synthetic prompt.')
        self.assertEqual(Outbox.events[-1]['sharing'], 'team')


class SharingContractTest(unittest.TestCase):
    def test_only_a_team_request_is_carried_and_nothing_else_is_accepted(self):
        from agentclient.enterprise_capture import normalize_capture
        from agentclient.general_contract import validate_event
        event = {'hook_event_name': 'UserPromptSubmit', 'event_id': 'e1', 'session_id': 's', 'prompt': 'x'}
        team = normalize_capture(event, 'example-repo', 'synthetic-connection', sharing='team')
        self.assertEqual(team['sharing'], 'team')
        self.assertNotIn('sharing', normalize_capture(event, 'example-repo', 'synthetic-connection'))
        for forged in ('private', 'organization', 'everyone', None):
            with self.assertRaises(ValueError):validate_event(dict(team, sharing=forged))
        with self.assertRaises(ValueError):
            normalize_capture(event, 'example-repo', 'synthetic-connection', sharing='organization')
