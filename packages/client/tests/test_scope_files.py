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

    def test_visibility_is_ignored_and_an_unusable_read_list_applies_no_filter(self):
        self.write(self.repo / '.vaelius', 'workspace = "example-repo"\nvisibility = "org"\nread = ["*", 1]\n')
        found = discover(self.repo)
        self.assertEqual(found['capture'], 'on', 'ignored, not invalid')
        self.assertEqual(found['warnings'], [scope_file.WARN_VISIBILITY_IGNORED, scope_file.WARN_READ_INVALID])
        self.assertNotIn('visibility', found)
        self.assertIsNone(found['read'], 'no filter: recall covers everything the session may read')

    def test_read_is_a_recall_filter_and_private_is_a_capture_mode(self):
        self.write(self.repo / '.vaelius', 'workspace = "maple"\ncapture = "private"\nread = ["maple", "birch", "maple"]\n')
        found = discover(self.repo)
        self.assertEqual((found['capture'], found['read'], found['warnings']), ('private', ['maple', 'birch'], []))
        self.write(self.repo / '.vaelius', 'workspace = "maple"\n')
        self.assertIsNone(discover(self.repo)['read'], 'no key: unfiltered')
        self.assertEqual(scope_file.CAPTURE_MODES, ('on', 'private', 'read-only', 'off'))
        self.assertEqual(scope_file.narrower('on', 'private'), 'private')
        self.assertEqual(scope_file.narrower('read-only', 'private'), 'read-only')

    def test_local_override_only_narrows_capture_and_cannot_change_the_workspace(self):
        ladder = scope_file.CAPTURE_MODES
        for committed in ladder:
            for local in ladder:
                with self.subTest(committed=committed, local=local):
                    self.write(self.repo / '.vaelius', 'workspace = "maple"\ncapture = "' + committed + '"\n')
                    self.write(self.repo / '.vaelius.local', 'capture = "' + local + '"\n')
                    found = discover(self.repo)
                    wider = ladder.index(local) < ladder.index(committed)
                    # Narrow-only: the effective mode is never wider than the committed file's.
                    self.assertEqual(found['capture'], committed if wider else local)
                    self.assertEqual(scope_file.WARN_LOCAL_WIDER in found['warnings'], wider)
        self.write(self.repo / '.vaelius', 'workspace = "maple"\nread = ["maple"]\n')
        self.write(self.repo / '.vaelius.local', 'workspace = "birch"\nsharing = "organization"\nread = ["birch"]\n')
        found = discover(self.repo)
        self.assertEqual((found['workspace'], found['capture'], found['read']), ('maple', 'on', ['birch']))
        self.assertEqual(found['warnings'], [scope_file.WARN_LOCAL_KEYS])
        self.assertEqual(found['local'], str(self.repo / '.vaelius.local'))
        # An override Vaelius cannot read fails closed, like any invalid scope file.
        self.write(self.repo / '.vaelius.local', 'capture = "always"\n')
        self.assertEqual((discover(self.repo)['capture'], discover(self.repo)['error']), ('off', 'scope_local_invalid_capture'))

    def test_a_local_override_without_a_scope_file_does_nothing(self):
        self.write(self.repo / '.vaelius.local', 'capture = "on"\nread = ["maple"]\n')
        self.assertIsNone(discover(self.repo))
        from agentclient.hooks import resolve_scope
        self.assertIsNone(resolve_scope({}, {'session_id': 's', 'cwd': str(self.repo)}))

    def test_write_local_goes_into_git_info_exclude_never_gitignore(self):
        self.write(self.repo / '.vaelius', 'workspace = "maple"\n')
        self.write(self.repo / 'pkg/.vaelius', 'workspace = "maple"\n')
        first = scope_file.write_local(self.repo / 'pkg/.vaelius', capture='read-only', read=['maple'])
        again = scope_file.write_local(self.repo / 'pkg/.vaelius', capture='private')
        self.assertEqual((first['excluded'], again['excluded']), (True, True))
        self.assertEqual((self.repo / '.git/info/exclude').read_text().splitlines().count('/pkg/.vaelius.local'), 1)
        self.assertFalse((self.repo / '.gitignore').exists())
        self.assertEqual(discover(self.repo / 'pkg')['capture'], 'private')
        self.assertEqual(discover(self.repo / 'pkg')['read'], ['maple'], 'a key that is not given is kept')
        scope_file.write_local(self.repo / 'pkg/.vaelius', read=['birch'])
        found = discover(self.repo / 'pkg')
        self.assertEqual((found['capture'], found['read']), ('private', ['birch']),
                         'setting the filter never drops the narrower capture mode')
        loose = self.tmp / 'loose'
        loose.mkdir()
        self.write(loose / '.vaelius', 'workspace = "maple"\n')
        self.assertFalse(scope_file.write_local(loose / '.vaelius', capture='off')['excluded'])

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

    def test_sharing_level_is_read_and_originals_is_warned_about(self):
        # Legacy sharing (no `workspace` key) is still read and sent on the wire,
        # so a repository that captured in the old model keeps capturing in the
        # old model. `originals` is accepted so the file is not invalid but is
        # never sent (the access model drops the originals ladder); the parser
        # records a one-time warning.
        self.write(self.repo / '.vaelius', 'project = "example-repo"\nsharing = "organization"\noriginals = "team"\n')
        found = discover(self.repo)
        self.assertEqual(found['sharing'], 'organization')
        self.assertEqual(found['originals'], 'private')
        self.assertIn(scope_file.WARN_ORIGINALS_IGNORED, found['warnings'])
        self.write(self.repo / '.vaelius', 'project = "example-repo"\nsharing = "team"\n')
        self.assertEqual(discover(self.repo)['originals'], 'private')
        for text in ('originals = "everyone"\n', 'originals = true\n', 'sharing = "public"\n'):
            self.write(self.repo / '.vaelius', 'project = "example-repo"\n' + text)
            found = discover(self.repo)
            self.assertEqual((found['capture'], found['sharing'], found['originals']), ('off', 'private', 'private'), text)
            self.assertIn('error', found)
        # The legacy key/value reader maps the same keys and levels.
        self.write(self.repo / '.mindshare', 'mode: default\nsharing: organization\noriginals: team\nteam: x\n')
        names = scope_file.scope_names({'scope_files': ['.mindshare']})
        found = discover(self.repo, names)
        self.assertEqual((found['capture'], found['sharing']), ('on', 'organization'))
        self.assertEqual(found['originals'], 'private')
        self.assertIn(scope_file.WARN_ORIGINALS_IGNORED, found['warnings'])

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
        self.requests = []

    def request(self, path, value=None):
        self.calls.append(path)
        self.requests.append((path, value))
        if path.endswith('capture-notice'):
            raise OSError('service unavailable')
        if path.endswith('preferences'):
            return {'values': {}, 'preferences': []}
        if path.endswith('search'):
            return {'answerable': True, 'support': 'complete', 'results': [
                {'id': 'doc-1', 'revision': 'rev-1', 'title': 'Synthetic', 'lesson': 'Synthetic lesson.',
                 'evidence_status': 'discovery_only', 'origin': {'project': 'shared-docs', 'sharing': 'organization'}}]}
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

    def test_legacy_sharing_requests_are_sent_but_originals_is_dropped(self):
        # In a legacy file (no `workspace =`), sharing keeps today's meaning so
        # repositories that captured in the old model keep doing so. Originals
        # are no longer sent on the wire (the access model drops the ladder):
        # a file that still carries `originals = ...` captures without it, with
        # a one-time scope-file warning.
        self.scope('project = "example-repo"\nsharing = "organization"\noriginals = "team"\n')
        self.hook()
        self.assertEqual(Outbox.events[0]['sharing'], 'organization')
        self.assertNotIn('originals', Outbox.events[0])
        self.scope('project = "example-repo"\nsharing = "team"\n')
        self.hook()
        self.assertEqual(Outbox.events[-1]['sharing'], 'team')
        self.assertNotIn('originals', Outbox.events[-1])

    def test_private_request_is_not_sent(self):
        self.scope('project = "example-repo"\n')
        self.hook()
        self.assertNotIn('sharing', Outbox.events[0])
        self.assertNotIn('originals', Outbox.events[0])

    def test_read_only_recalls_but_does_not_capture(self):
        self.scope('project = "example-repo"\ncapture = "read-only"\nsharing = "team"\n')
        result, backend = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertIn('/enterprise/v3/search', backend.calls)
        self.assertEqual(Outbox.events, [])
        status, _ = self.hook('SessionStart')
        self.assertIn('read-only', status['systemMessage'])
        self.assertEqual(Outbox.events, [])

    def test_private_folder_is_captured_owner_private(self):
        self.scope('workspace = "maple"\ncapture = "private"\n')
        result, backend = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertEqual([e['project'] for e in Outbox.events], ['maple'])
        self.assertNotIn('sharing', Outbox.events[0], 'owner-private: no sharing request goes on the wire')
        status, _ = self.hook('SessionStart')
        self.assertIn('capture PRIVATE for workspace maple', status['systemMessage'])
        self.assertIn('personal memory only', status['systemMessage'])
        from agentclient.hooks import resolve_scope
        scope = resolve_scope(self.config, {'session_id': 's1', 'cwd': str(self.repo)})
        self.assertEqual((scope['capture'], scope['sharing'], scope['private']), ('on', 'private', True))

    def test_local_override_narrows_the_hook_and_a_wider_one_warns_once(self):
        self.scope('workspace = "maple"\n')
        self.scope('capture = "read-only"\n', name='.vaelius.local')
        result, backend = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertEqual(Outbox.events, [], 'narrowed to read-only: recall, nothing saved')
        self.scope('workspace = "maple"\ncapture = "read-only"\n')
        self.scope('capture = "on"\n', name='.vaelius.local')
        first, _ = self.hook()
        self.assertEqual(Outbox.events, [], 'a local file never widens the committed one')
        self.assertIn('can only narrow', first['systemMessage'])
        again, _ = self.hook()
        self.assertNotIn('can only narrow', again.get('systemMessage', ''), 'once')

    def test_read_filter_travels_with_recall(self):
        self.scope('workspace = "maple"\nread = ["maple", "birch"]\n')
        _, backend = self.hook()
        search = [value for path, value in backend.requests if path == '/enterprise/v3/search'][0]
        self.assertEqual(search['workspaces'], ['maple', 'birch'])
        self.scope('workspace = "maple"\n')
        _, backend = self.hook()
        search = [value for path, value in backend.requests if path == '/enterprise/v3/search'][0]
        self.assertNotIn('workspaces', search, 'no key: everything the session may read')

    def test_default_capture_read_only_recalls_and_saves_nothing_where_no_scope_file_applies(self):
        self.write(default_capture='read-only')
        result, backend = self.hook()
        self.assertTrue(self.recalled(result))
        self.assertEqual(backend.calls, ['/enterprise/v3/search'], 'recall only: no preferences, no receipt')
        self.assertNotIn('project', backend.requests[0][1], 'no workspace: everything the person can read')
        self.assertNotIn('session_label', backend.requests[0][1])
        start, backend = self.hook('SessionStart')
        self.assertIn(('/enterprise/v3/sessions/read-only', {'sessions': ['s1']}), backend.requests,
                      'the service refuses writes naming this chat')
        self.assertIn('sent to the Vaelius service as recall queries', start['systemMessage'])
        for kind, extra in (('PostToolUse', {'tool_name': 'Bash', 'tool_input': {'command': 'true'}, 'tool_response': {}}),
                            ('Stop', {'last_assistant_message': 'done'}), ('PostCompact', {'trigger': 'auto'})):
            self.assertEqual(self.hook(kind, **extra)[0], {})
        self.assertEqual(Outbox.events, [], 'nothing is ever captured by the default')
        # A scope file in the folder governs instead: off stays off and an invalid one fails closed.
        for text in ('project = "example-repo"\ncapture = "off"\n', 'project = [\n'):
            self.scope(text)
            result, backend = self.hook()
            self.assertEqual((result, backend.calls, Outbox.events), ({}, [], []), text)
        # And it is off unless the person turned it on.
        (self.repo / '.vaelius').unlink()
        self.write()
        result, backend = self.hook()
        self.assertEqual((result, backend.calls, Outbox.events), ({}, [], []))

    def test_default_capture_can_only_be_off_or_read_only(self):
        from agentclient.hooks import default_scope
        for value in ('on', 'private', 'ON', 'always', True, None, ['read-only']):
            with self.subTest(value=value):
                self.write(default_capture=value)
                with self.assertRaises(ScopeFileError):
                    self.hook()
                with self.assertRaises(ScopeFileError):
                    self.hook('SessionStart')
                with self.assertRaises(ScopeFileError):
                    scope_file.default_capture({'default_capture': value})
                with self.assertRaises(ScopeFileError):
                    default_scope({'default_capture': value}, {'cwd': str(self.repo)})
                self.assertEqual(Outbox.events, [], 'a profile setting never turns capture on')
        self.assertEqual(scope_file.default_capture({}), 'off')
        self.assertEqual(scope_file.DEFAULT_CAPTURE, ('off', 'read-only'))
        scope = default_scope({'default_capture': 'read-only'}, {'cwd': str(self.repo)})
        self.assertEqual((scope['capture'], scope['project']), ('read-only', None))

    def test_config_command_sets_and_shows_default_capture(self):
        import contextlib
        import io
        from agentclient import cli
        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cli.main(['--home', str(self.home), *argv])
            return json.loads(out.getvalue())
        self.assertEqual(run('config', 'default-capture')['default_capture'], 'off')
        self.assertEqual(run('config', 'default-capture', 'read-only'), mock.ANY)
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['default_capture'], 'read-only')
        self.assertEqual(run('scope-status', '--cwd', str(self.repo))['default_capture'], 'read-only')
        for value in ('on', 'private'):
            with self.assertRaises(SystemExit):
                run('config', 'default-capture', value)
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['default_capture'], 'read-only')
        # A hand-edited value is rejected at load by every other command, and `config` repairs it.
        self.write(default_capture='on')
        with self.assertRaises(SystemExit) as refused:
            run('scope-status', '--cwd', str(self.repo))
        self.assertIn('invalid_default_capture', str(refused.exception))
        self.assertEqual(run('config', 'default-capture', 'off')['default_capture'], 'off')

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
    def test_only_shared_levels_are_carried_and_nothing_else_is_accepted(self):
        from agentclient.enterprise_capture import normalize_capture
        from agentclient.general_contract import validate_event
        event = {'hook_event_name': 'UserPromptSubmit', 'event_id': 'e1', 'session_id': 's', 'prompt': 'x'}
        team = normalize_capture(event, 'example-repo', 'synthetic-connection', sharing='team')
        self.assertEqual(team['sharing'], 'team')
        self.assertNotIn('originals', team)
        plain = normalize_capture(event, 'example-repo', 'synthetic-connection')
        self.assertNotIn('sharing', plain)
        self.assertNotIn('originals', plain)
        # In a legacy file (no `workspace =`), sharing passes through unchanged;
        # the access model drops the originals ladder, so it is never sent.
        wide = normalize_capture(event, 'example-repo', 'synthetic-connection', sharing='organization',
                                 originals='team')
        self.assertEqual(wide['sharing'], 'organization')
        self.assertNotIn('originals', wide)
        # In a workspace file, only `private` is honored; a wider request is
        # translated to `organization` (the server reads it as "inherit the
        # workspace audience"), and `originals` is still never sent.
        workspace = normalize_capture(event, 'example-repo', 'synthetic-connection', sharing='team',
                                      workspace_key=True)
        self.assertEqual(workspace['sharing'], 'organization')
        self.assertNotIn('originals', workspace)
        workspace_private = normalize_capture(event, 'example-repo', 'synthetic-connection', sharing='private',
                                              workspace_key=True)
        self.assertNotIn('sharing', workspace_private)
        # Private travels as omission, so older services see the same event.
        for key in ('sharing', 'originals'):
            for forged in ('private', 'everyone', 'public', None, ['team']):
                with self.assertRaises(ValueError):
                    validate_event(dict(wide, **{key: forged}))
        for key in ('sharing', 'originals'):
            with self.assertRaises(ValueError):
                normalize_capture(event, 'example-repo', 'synthetic-connection', **{key: 'everyone'})


class IgnoredLocationTest(unittest.TestCase):
    """A scope file at home, above it, or directly in a top-level personal folder never activates capture."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.top = self.tmp / 'root'  # stands in for `/` above the temporary home
        self.home = self.top / 'Users/person'
        self.home.mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {'HOME': str(self.home)}))

    def place(self, *files):
        for relative in files:
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('workspace = "example-ws"\n')

    def test_discovery_table(self):
        rows = [
            # (name, scope files relative to home, git dirs, cwd relative to home, expected root or None)
            ('home file, scratch folder, no git', ['.vaelius'], [], 'scratch', None),
            ('Documents file, notes folder', ['Documents/.vaelius'], [], 'Documents/notes', None),
            ('Desktop file', ['Desktop/.vaelius'], [], 'Desktop/scratch', None),
            ('above home (filesystem-root stand-in)', ['../../.vaelius'], [], 'scratch', None),
            ('mail store, any depth', ['Library/Mail/V10/.vaelius'], [], 'Library/Mail/V10/box', None),
            ('dotfiles repo at home', ['.vaelius'], ['.git'], 'scratch', None),
            ('project folder in Documents, no git', ['Documents/proj/.vaelius'], [], 'Documents/proj/sub', 'Documents/proj'),
            ('repo with its own file in Documents', ['Documents/repo/.vaelius', 'Documents/.vaelius'],
             ['Documents/repo/.git'], 'Documents/repo/src', 'Documents/repo'),
            ('project under home, home file too', ['.vaelius', 'code/app/.vaelius'], [], 'code/app/x', 'code/app'),
        ]
        for name, files, gits, cwd, expected in rows:
            with self.subTest(name):
                shutil.rmtree(self.top)
                self.home.mkdir(parents=True)
                self.place(*files)
                for g in gits:
                    (self.home / g).mkdir(parents=True)
                (self.home / cwd).mkdir(parents=True, exist_ok=True)
                found = discover(self.home / cwd)
                if expected is None:
                    self.assertIsNone(found)
                else:
                    self.assertEqual(found['root'], str(self.home / expected))
                    self.assertEqual(found['project'], 'example-ws')

    def test_ignored_file_is_skipped_not_a_stop_and_is_reported(self):
        self.place('Documents/.vaelius', '.vaelius')
        (self.home / 'Documents/notes').mkdir()
        ignored = []
        self.assertIsNone(discover(self.home / 'Documents/notes', ignored=ignored))
        # The walk went past the first ignored file and reported both.
        self.assertEqual(ignored, [self.home / 'Documents/.vaelius', self.home / '.vaelius'])

    def test_legacy_names_follow_the_same_rule(self):
        (self.home / '.mindshare').write_text('mode: default\n')
        (self.home / 'scratch').mkdir()
        names = scope_file.scope_names({'scope_files': ['.vaelius', '.mindshare']})
        self.assertIsNone(discover(self.home / 'scratch', names))

    @unittest.skipUnless(shutil.which('git'), 'git required')
    def test_worktree_fallback_follows_the_same_rule(self):
        # A dotfiles-style repository at home: the main checkout's file is at home, so it is ignored.
        git('init', '-q', cwd=self.home)
        self.place('.vaelius')
        (self.home / 'a.txt').write_text('a\n')
        git('add', 'a.txt', cwd=self.home)
        git('commit', '-qm', 'init', cwd=self.home)
        git('worktree', 'add', '-q', str(self.tmp / 'wt'), cwd=self.home)
        self.assertIsNone(discover(self.tmp / 'wt'))

    def test_enrollment_is_never_recorded_or_reported_for_an_ignored_root(self):
        profile = self.tmp / 'profile'
        profile.mkdir()
        self.assertFalse(scope_file.record_enrollment(profile, {'root': str(self.home), 'project': 'example-ws'}))
        self.assertFalse((profile / scope_file.ENROLLMENTS).exists())
        # A root recorded before the rule existed is no longer reported.
        (profile / scope_file.ENROLLMENTS).write_text(json.dumps({
            str(self.home): {'project': 'example-ws'}, str(self.home / 'code/app'): {'project': 'example-ws'}}))
        self.assertEqual(list(scope_file.enrollments(profile)), [str(self.home / 'code/app')])

    def test_predicate_parity_with_origin_labels(self):
        from agentclient import origin
        self.assertIs(origin.ignored_scope_root, scope_file.ignored_scope_root)
        context = origin.Context(None, str(self.home), {})
        self.assertEqual(context.home, self.home)
        cases = {self.top: True, self.top / 'Users': True, self.home: True, self.home / 'Documents': True,
                 self.home / 'Desktop': True, self.home / 'Downloads': True, self.home / 'Library/CloudStorage': True,
                 self.home / 'Library/Mail/V10/box': True, self.home / 'Library/Messages': True,
                 self.home / 'Documents/proj': False, self.home / 'code': False, self.home / 'Library': False,
                 self.tmp / 'elsewhere': False}
        for directory, expected in cases.items():
            with self.subTest(str(directory)):
                self.assertEqual(scope_file.ignored_scope_root(directory), expected)
                self.assertEqual(scope_file.ignored_scope_root(directory, self.home), expected)
                self.assertEqual(context._unscopable(directory), expected)

    def test_session_start_warns_the_person_once_and_never_the_model(self):
        from agentclient.hooks import handle
        profile = self.tmp / 'profile'
        profile.mkdir()
        (profile / 'config.json').write_text(json.dumps({'projects': {}, 'sessions': {}, 'knowledge_backend': {
            'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2', 'capture_owner': 'hooks',
            'api_version': 'cloud-local-1', 'connection_id': 'synthetic-connection'}}))
        self.place('.vaelius')
        (self.home / 'scratch').mkdir()
        Outbox.events = []
        results = []
        for _ in range(2):
            with mock.patch('agentclient.transport.enterprise_client', return_value=Backend()), \
                    mock.patch('agentclient.capture_outbox.Outbox', Outbox):
                results.append(handle(profile, {'hook_event_name': 'SessionStart', 'cwd': str(self.home / 'scratch'),
                                                'session_id': 's1'}))
        self.assertIn(str(self.home / '.vaelius') + ' is ignored', results[0]['systemMessage'])
        self.assertNotIn('hookSpecificOutput', results[0])
        self.assertEqual(results[1], {})
        self.assertEqual(Outbox.events, [])
