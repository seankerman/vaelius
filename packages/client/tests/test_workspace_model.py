"""PR A client half: scope-file `workspace` key, remote discovery, 409 handling, CLI.

The access model (`docs/ACCESS_MODEL.md`) replaces flat projects with
workspaces. The scope file carries a new `workspace` key; legacy `project`
still works. On the wire every capture carries `workspace` and (when a Git
remote is set) `remote`. The server rejects a capture whose remote is not
linked to the workspace with HTTP 409 `repo_not_linked`; the client keeps the
event queued and shows the admin fix on the status line.

All data is synthetic (example.com, alice/bob/carol, maple/birch).
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch
from urllib.error import HTTPError

from agentclient import scope_file
from agentclient.scope_file import (
    discover, discover_remote, parse, record_warning_once, ScopeFileError,
    WARN_ORIGINALS_IGNORED, WARN_SHARING_IGNORED,
)


class ScopeFileWorkspaceKeyTests(unittest.TestCase):
    """`workspace`, `project`, `sharing` and `originals` in the TOML scope file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / 'maple'
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / '.git').mkdir()

    def write(self, text):
        (self.repo / '.vaelius').write_text(text)

    def test_workspace_key_sets_workspace_and_defaults_to_inherit_audience(self):
        self.write('workspace = "maple"\n')
        found = discover(self.repo)
        self.assertEqual(found['workspace'], 'maple')
        self.assertEqual(found['project'], 'maple')
        self.assertTrue(found['workspace_key'])
        # No sharing key in a workspace file means "inherit the workspace
        # audience"; the client sends that as `sharing = "organization"` on the
        # wire (section 5), while `originals` is never sent any more.
        self.assertEqual(found['sharing'], 'organization')
        self.assertEqual(found['originals'], 'private')
        self.assertEqual(found['warnings'], [])

    def test_workspace_private_is_the_only_honored_sharing_value(self):
        self.write('workspace = "maple"\nsharing = "private"\n')
        found = discover(self.repo)
        self.assertEqual(found['sharing'], 'private')
        self.assertTrue(found['workspace_key'])
        self.assertEqual(found['warnings'], [])

    def test_workspace_file_ignores_team_and_organization_with_warnings(self):
        for level in ('team', 'organization'):
            self.write('workspace = "maple"\nsharing = "' + level + '"\n')
            found = discover(self.repo)
            self.assertTrue(found['workspace_key'])
            # The parser forces the inherit-audience default and records a
            # one-time warning for the ignored value.
            self.assertEqual(found['sharing'], 'organization', level)
            self.assertIn(WARN_SHARING_IGNORED, found['warnings'])

    def test_originals_is_ignored_with_a_warning_in_every_file_kind(self):
        self.write('workspace = "maple"\noriginals = "team"\n')
        self.assertIn(WARN_ORIGINALS_IGNORED, discover(self.repo)['warnings'])
        self.write('project = "maple"\noriginals = "organization"\n')
        self.assertIn(WARN_ORIGINALS_IGNORED, discover(self.repo)['warnings'])

    def test_legacy_project_still_names_a_workspace(self):
        self.write('project = "maple"\n')
        found = discover(self.repo)
        self.assertEqual(found['workspace'], 'maple')
        self.assertEqual(found['project'], 'maple')
        self.assertFalse(found['workspace_key'])
        # Legacy sharing semantics: private by default, team/organization pass
        # through on the wire so an unmigrated repository keeps capturing.
        self.assertEqual(found['sharing'], 'private')
        self.write('project = "maple"\nsharing = "team"\n')
        self.assertEqual(discover(self.repo)['sharing'], 'team')
        self.write('project = "maple"\nsharing = "organization"\n')
        self.assertEqual(discover(self.repo)['sharing'], 'organization')

    def test_workspace_and_project_disagreeing_fails_closed_like_any_invalid_file(self):
        self.write('workspace = "maple"\nproject = "birch"\n')
        found = discover(self.repo)
        self.assertEqual(found['capture'], 'off')
        self.assertIn('error', found)
        self.assertIn('mismatch', found['error'])

    def test_workspace_and_project_agreeing_sets_the_workspace_key(self):
        self.write('workspace = "maple"\nproject = "maple"\n')
        found = discover(self.repo)
        self.assertEqual(found['workspace'], 'maple')
        self.assertTrue(found['workspace_key'])

    def test_invalid_workspace_name_fails_closed(self):
        self.write('workspace = "has spaces"\n')
        found = discover(self.repo)
        self.assertEqual(found['capture'], 'off')
        self.assertIn('error', found)

    def test_record_warning_once_dedups_across_sessions(self):
        home = self.tmp / 'profile'
        home.mkdir()
        self.assertTrue(record_warning_once(home, str(self.repo), WARN_SHARING_IGNORED))
        self.assertFalse(record_warning_once(home, str(self.repo), WARN_SHARING_IGNORED))
        # A different warning is still shown.
        self.assertTrue(record_warning_once(home, str(self.repo), WARN_ORIGINALS_IGNORED))


@unittest.skipUnless(shutil.which('git'), 'git required for the remote tests')
class RemoteDiscoveryTests(unittest.TestCase):
    """A capture carries the scope root's Git remote, normalized to `host/path`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, GIT_AUTHOR_NAME='Example',
                        GIT_AUTHOR_EMAIL='example@example.invalid',
                        GIT_COMMITTER_NAME='Example',
                        GIT_COMMITTER_EMAIL='example@example.invalid')

    def git(self, *args, cwd):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env=self.env)

    def _repo(self, name='maple'):
        root = self.tmp / name
        root.mkdir()
        self.git('init', '-q', cwd=root)
        return root

    def test_origin_is_preferred_and_normalized(self):
        root = self._repo()
        self.git('remote', 'add', 'origin', 'https://example.com/Example-Org/Maple.git', cwd=root)
        self.git('remote', 'add', 'mirror', 'ssh://git@example.com/other/mirror.git', cwd=root)
        self.assertEqual(discover_remote(root), 'example.com/example-org/maple')

    def test_only_remote_is_used_when_there_is_no_origin(self):
        root = self._repo()
        self.git('remote', 'add', 'upstream', 'https://example.com/Example-Org/Maple', cwd=root)
        # `discover_remote` caches per-root; use a nested checkout so each case
        # resolves afresh.
        self.assertEqual(discover_remote(root), 'example.com/example-org/maple')

    def test_many_remotes_without_origin_means_no_remote(self):
        root = self._repo()
        self.git('remote', 'add', 'upstream', 'https://example.com/upstream/maple.git', cwd=root)
        self.git('remote', 'add', 'mirror', 'https://example.com/mirror/maple.git', cwd=root)
        self.assertIsNone(discover_remote(root))

    def test_no_remote_means_no_remote(self):
        self.assertIsNone(discover_remote(self._repo()))

    def test_scp_style_remote_is_normalized(self):
        root = self._repo()
        self.git('remote', 'add', 'origin', 'git@example.com:example-org/maple.git', cwd=root)
        self.assertEqual(discover_remote(root), 'example.com/example-org/maple')

    def test_non_git_folder_has_no_remote(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        self.assertIsNone(discover_remote(plain))


class RemoteNormalizationUnitTests(unittest.TestCase):
    """`normalize_remote` matches the ACCESS_MODEL section 4 table."""

    def test_cases_from_the_access_model(self):
        from agentclient.general_contract import normalize_remote
        same = 'example.com/example-org/maple'
        self.assertEqual(normalize_remote('https://example.com/Example-Org/Maple.git'), same)
        self.assertEqual(normalize_remote('ssh://git@example.com:22/example-org/maple'), same)
        self.assertEqual(normalize_remote('git@example.com:example-org/maple.git'), same)
        # Subgroups: more than two path segments.
        subgroup = 'example.com/example-org/platform/maple'
        self.assertEqual(normalize_remote('https://example.com/example-org/platform/maple.git'), subgroup)
        # Local file paths and `file://` remotes name no shared repository.
        self.assertIsNone(normalize_remote('/absolute/path/to/repo'))
        self.assertIsNone(normalize_remote('file:///absolute/path/to/repo'))
        self.assertIsNone(normalize_remote(''))


class CaptureEventFieldsTests(unittest.TestCase):
    """`workspace` and `remote` are present on every capture; `originals` never is."""

    def test_normalize_capture_sends_workspace_and_remote(self):
        from agentclient.enterprise_capture import normalize_capture
        event = {'hook_event_name': 'UserPromptSubmit', 'event_id': 'e1',
                 'session_id': 's', 'prompt': 'hello'}
        remote = 'example.com/example-org/maple'
        captured = normalize_capture(event, 'maple', 'synthetic-connection',
                                     remote=remote, workspace_key=True)
        self.assertEqual(captured['workspace'], 'maple')
        self.assertEqual(captured['remote'], remote)
        self.assertNotIn('sharing', captured)  # workspace_key + default private
        self.assertNotIn('originals', captured)

    def test_workspace_key_wider_sharing_becomes_organization_on_the_wire(self):
        from agentclient.enterprise_capture import normalize_capture
        event = {'hook_event_name': 'UserPromptSubmit', 'event_id': 'e2',
                 'session_id': 's', 'prompt': 'x'}
        captured = normalize_capture(event, 'maple', 'synthetic-connection',
                                     sharing='team', workspace_key=True)
        self.assertEqual(captured['sharing'], 'organization')


class OutboxRepoNotLinkedTests(unittest.TestCase):
    """A 409 `repo_not_linked` keeps the event queued and surfaces a notice."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'profile'
        self.home.mkdir()

    def _event(self):
        from agentclient.enterprise_capture import normalize_capture
        hook = {'hook_event_name': 'UserPromptSubmit', 'event_id': 'e-not-linked',
                'session_id': 's', 'prompt': 'anything'}
        return normalize_capture(hook, 'maple', 'synthetic-connection',
                                 remote='example.com/example-org/maple', workspace_key=True)

    def test_409_keeps_event_queued_and_adds_a_status_line_notice(self):
        from agentclient.capture_outbox import Outbox

        class Service:
            def request(self, path, value=None):
                raise HTTPError('https://service.example.invalid' + path, 409, 'Conflict', {},
                                io.BytesIO(json.dumps({'error': 'repo_not_linked'}).encode()))

        outbox = Outbox(self.home)
        try:
            outbox.put(self._event())
            result = outbox.drain(Service(), max_events=4, max_seconds=2)
            # The event stays queued; no acknowledgement.
            self.assertEqual(result['acknowledged_events'], 0)
            self.assertTrue(result['blocked'])
            self.assertEqual(result['pending_events'], 1)
            notices = [n for n in result.get('notices', []) if n['kind'] == 'repo_not_linked']
            self.assertEqual(len(notices), 1)
            text = notices[0]['text']
            self.assertIn('example.com/example-org/maple', text)
            self.assertIn('workspace "maple"', text)
            self.assertIn('vaelius-client workspace link maple example.com/example-org/maple', text)
        finally:
            outbox.close()

    def test_403_read_only_session_drops_the_event_and_keeps_draining(self):
        from agentclient.capture_outbox import Outbox

        class Service:
            calls = 0

            def request(self, path, value=None):
                Service.calls += 1
                raise HTTPError('https://service.example.invalid' + path, 403, 'Forbidden', {},
                                io.BytesIO(json.dumps({'error': 'read_only_session', 'message': 'x'}).encode()))

        outbox = Outbox(self.home)
        try:
            outbox.put(self._event())
            result = outbox.drain(Service(), max_events=4, max_seconds=2)
            self.assertEqual((result['pending_events'], result['blocked']), (0, False), 'never accepted: dropped')
            self.assertEqual(result['gaps'].get('read_only_session_refused'), 1)
        finally:
            outbox.close()

    def test_parts_notice_from_receipt_becomes_a_status_line_notice(self):
        from agentclient.capture_outbox import Outbox

        class Service:
            def __init__(self):
                self.calls = 0

            def request(self, path, value=None):
                self.calls += 1
                return {'disposition': 'accepted', 'digest': value['digest'],
                        'received_parts': value['part_index'] + 1, 'complete': True,
                        'source_id': 'src-synthetic', 'notice': 'vaelius: workspace maple linked to '
                        'example.com/example-org/maple automatically.'}

        outbox = Outbox(self.home)
        try:
            outbox.put(self._event())
            result = outbox.drain(Service(), max_events=4, max_seconds=2)
            self.assertEqual(result['acknowledged_events'], 1)
            notices = [n for n in result.get('notices', []) if n['kind'] == 'parts_notice']
            self.assertEqual(len(notices), 1)
            self.assertIn('linked', notices[0]['text'])
        finally:
            outbox.close()

    def test_apply_notices_shows_each_notice_at_most_once_per_window(self):
        from agentclient.hooks import _apply_notices
        notices = [{'kind': 'repo_not_linked', 'ident': 'x', 'text': 'vaelius: once'}]
        self.assertEqual(_apply_notices(self.home, notices), 'vaelius: once')
        # The same ident within the TTL is suppressed.
        self.assertIsNone(_apply_notices(self.home, notices))


class WorkspaceCliTests(unittest.TestCase):
    """`vaelius-client workspace` builds request bodies that match API.md."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'config.json').write_text(json.dumps({'projects': {},
            'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2',
                                  'api_version': 'cloud-local-1'}}))

    def _run(self, *argv, response=None):
        from agentclient import cli
        from agentclient.cloud_contract import validate_response as cloud_validate

        class Service:
            def __init__(self, response):
                self.calls = []
                self.response = response or {}

            def request(self, path, value=None):
                self.calls.append((path, value))
                # Mimic the transport boundary: validate the response the server
                # would send, so a shape change breaks the CLI contract too.
                cloud_validate(path, self.response)
                return self.response

        service = Service(response)
        with patch('agentclient.transport.enterprise_client', return_value=service), \
                patch('builtins.print') as printed:
            cli.main(['--home', str(self.home), *argv])
        return service.calls, json.loads(printed.call_args[0][0]) if printed.call_args else None

    def test_workspace_list(self):
        response = {'workspaces': [{'id': 'maple', 'name': 'maple', 'role': 'writer',
                                    'item_default': 'shared', 'active': True, 'organization_entry': None}]}
        calls, _ = self._run('workspace', 'list', response=response)
        self.assertEqual(calls, [('/enterprise/v3/workspaces', {})])

    def test_workspace_create_show_settings(self):
        response = {'workspace': {'id': 'maple', 'name': 'maple', 'item_default': 'shared', 'active': True},
                    'role': 'admin'}
        with self.assertRaises(SystemExit, msg='a visibility is required'), \
                mock.patch('sys.stderr', new=io.StringIO()):
            self._run('workspace', 'create', 'maple', response=response)
        calls, _ = self._run('workspace', 'create', 'maple', '--visibility', 'private', '--item-default', 'private',
                             response=dict(response, workspace=dict(response['workspace'], item_default='private')))
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/create',
                                    {'name': 'maple', 'visibility': 'private', 'item_default': 'private'}))
        calls, _ = self._run('workspace', 'set-visibility', 'maple', 'org', response={'visibility': 'org'})
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/visibility', {'workspace': 'maple', 'visibility': 'org'}))
        show_response = {'workspace': response['workspace'], 'role': 'writer', 'entries': [
            {'subject_kind': 'principal', 'subject': 'alice', 'role': 'admin',
             'suspended': False, 'granted_by': 'migration', 'created': 1}], 'repos': [], 'audience_size': 2}
        calls, _ = self._run('workspace', 'show', 'maple', response=show_response)
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/show', {'workspace': 'maple'}))
        settings_response = {'workspace': response['workspace'], 'changed': True}
        calls, _ = self._run('workspace', 'settings', 'maple', '--active', 'false',
                             '--item-default', 'private', response=dict(
                                 settings_response, workspace=dict(response['workspace'], item_default='private'),
                             ))
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/settings',
                                    {'workspace': 'maple', 'item_default': 'private', 'active': False}))

    def test_workspace_entries_set_and_remove(self):
        response = {'workspace': 'maple', 'entry': {'subject_kind': 'principal', 'subject': 'bob', 'role': 'writer'},
                    'operation': 'added', 'audience_before': 1, 'audience_after': 2}
        calls, _ = self._run('workspace', 'set-entry', 'maple',
                             '--subject-kind', 'principal', '--subject', 'bob', '--role', 'writer',
                             response=response)
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/entries/set',
                                    {'workspace': 'maple', 'subject_kind': 'principal',
                                     'subject': 'bob', 'role': 'writer'}))
        # Organization entry omits --subject.
        org_response = dict(response, entry={'subject_kind': 'organization', 'role': 'reader'})
        calls, _ = self._run('workspace', 'set-entry', 'maple',
                             '--subject-kind', 'organization', '--role', 'reader', response=org_response)
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/entries/set',
                                    {'workspace': 'maple', 'subject_kind': 'organization', 'role': 'reader'}))
        remove_response = {'workspace': 'maple', 'removed': True, 'audience_before': 2, 'audience_after': 1}
        calls, _ = self._run('workspace', 'remove-entry', 'maple',
                             '--subject-kind', 'principal', '--subject', 'bob', response=remove_response)
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/entries/remove',
                                    {'workspace': 'maple', 'subject_kind': 'principal', 'subject': 'bob'}))
        # Principal and group entries require --subject.
        with self.assertRaises(SystemExit):
            self._run('workspace', 'set-entry', 'maple', '--subject-kind', 'principal', '--role', 'writer')

    def test_workspace_link_and_unlink_normalize_the_remote(self):
        link_response = {'workspace': 'maple', 'remote': 'example.com/example-org/maple',
                         'linked': True, 'changed': True}
        calls, _ = self._run('workspace', 'link', 'maple',
                             'https://example.com/Example-Org/Maple.git', response=link_response)
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/repos/link',
                                    {'workspace': 'maple', 'remote': 'example.com/example-org/maple'}))
        unlink_response = dict(link_response, linked=False)
        calls, _ = self._run('workspace', 'unlink', 'maple',
                             'git@example.com:example-org/maple.git', response=unlink_response)
        self.assertEqual(calls[0], ('/enterprise/v3/workspaces/repos/unlink',
                                    {'workspace': 'maple', 'remote': 'example.com/example-org/maple'}))


class GroupCliTests(unittest.TestCase):
    """`vaelius-client group` builds request bodies that match API.md."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'config.json').write_text(json.dumps({'projects': {},
            'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2',
                                  'api_version': 'cloud-local-1'}}))

    def _run(self, *argv, response):
        from agentclient import cli
        from agentclient.cloud_contract import validate_response as cloud_validate

        class Service:
            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append((path, value))
                cloud_validate(path, response)
                return response

        service = Service()
        with patch('agentclient.transport.enterprise_client', return_value=service), \
                patch('builtins.print'):
            cli.main(['--home', str(self.home), *argv])
        return service.calls

    def test_group_list_create_show(self):
        listing = {'groups': [{'id': 'maple-team', 'name': 'maple-team', 'source': 'local',
                               'workspace': 'maple', 'sync_state': 'current', 'active': True,
                               'admin': True, 'member': True}]}
        calls = self._run('group', 'list', response=listing)
        self.assertEqual(calls[0], ('/enterprise/v3/groups', {}))
        create = {'group': {'id': 'maple-team', 'name': 'maple-team', 'source': 'local'}}
        calls = self._run('group', 'create', 'maple-team', '--workspace', 'maple', response=create)
        self.assertEqual(calls[0], ('/enterprise/v3/groups/create',
                                    {'name': 'maple-team', 'workspace': 'maple'}))
        show = {'group': create['group'], 'members': [{'kind': 'principal', 'id': 'alice', 'role': 'admin'}]}
        calls = self._run('group', 'show', 'maple-team', response=show)
        self.assertEqual(calls[0], ('/enterprise/v3/groups/show', {'group': 'maple-team'}))

    def test_group_add_remove_delete(self):
        add = {'group': 'maple-team', 'member_kind': 'principal', 'member': 'bob',
               'role': 'admin', 'changed': True}
        calls = self._run('group', 'add-member', 'maple-team',
                          '--member-kind', 'principal', '--member', 'bob', '--role', 'admin', response=add)
        self.assertEqual(calls[0], ('/enterprise/v3/groups/members/add',
                                    {'group': 'maple-team', 'member_kind': 'principal',
                                     'member': 'bob', 'role': 'admin'}))
        remove = {'group': 'maple-team', 'member_kind': 'principal', 'member': 'bob', 'removed': True}
        calls = self._run('group', 'remove-member', 'maple-team',
                          '--member-kind', 'principal', '--member', 'bob', response=remove)
        self.assertEqual(calls[0], ('/enterprise/v3/groups/members/remove',
                                    {'group': 'maple-team', 'member_kind': 'principal', 'member': 'bob'}))
        delete = {'group': 'maple-team', 'deleted': True}
        calls = self._run('group', 'delete', 'maple-team', response=delete)
        self.assertEqual(calls[0], ('/enterprise/v3/groups/delete', {'group': 'maple-team'}))


class PersonSharingTests(unittest.TestCase):
    """`vaelius-client sharing --set shared|private` sends `contributions_default`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'config.json').write_text(json.dumps({'projects': {},
            'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2',
                                  'api_version': 'cloud-local-1'}}))

    def _run(self, *argv, service):
        from agentclient import cli
        with patch('agentclient.transport.enterprise_client', return_value=service), \
                patch('builtins.print') as printed:
            cli.main(['--home', str(self.home), *argv])
        return service.calls, json.loads(printed.call_args[0][0]) if printed.call_args else None

    def test_set_shared_or_private_sends_contributions_default(self):
        class Service:
            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append((path, value))
                return {'principal': 'alice', 'contributions_default': value.get('contributions_default', 'shared'),
                        'changed': True}
        service = Service()
        calls, output = self._run('sharing', '--set', 'shared', service=service)
        self.assertEqual(calls, [('/enterprise/v3/sharing', {'contributions_default': 'shared'})])
        self.assertEqual(output['contributions_default'], 'shared')
        service = Service()
        calls, output = self._run('sharing', '--set', 'private', service=service)
        self.assertEqual(calls, [('/enterprise/v3/sharing', {'contributions_default': 'private'})])

    def test_legacy_level_names_are_still_accepted_as_shared(self):
        class Service:
            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append((path, value))
                return {'principal': 'alice', 'contributions_default': 'shared', 'changed': True}
        for level in ('team', 'organization'):
            service = Service()
            self._run('sharing', '--set', level, service=service)
            self.assertEqual(service.calls, [('/enterprise/v3/sharing', {'contributions_default': 'shared'})])

    def test_apply_to_existing_previews_then_confirms(self):
        token = 'c' * 32

        class Service:
            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append((path, value))
                if value.get('confirm') == token:
                    return {'principal': 'alice', 'contributions_default': 'private', 'changed': True,
                            'existing': {'sources': 7, 'confirm_token': None, 'applied': 7}}
                return {'principal': 'alice', 'contributions_default': 'private', 'changed': False,
                        'existing': {'sources': 7, 'confirm_token': token}}
        service = Service()
        calls, output = self._run('sharing', '--set', 'private', '--apply-to-existing', service=service)
        self.assertEqual(calls, [('/enterprise/v3/sharing',
                                  {'contributions_default': 'private', 'apply_to_existing': True})])
        self.assertIn('next', output)
        self.assertIn(token, output['next'])
        service = Service()
        calls, output = self._run('sharing', '--set', 'private', '--apply-to-existing',
                                  '--confirm', token, service=service)
        self.assertEqual(calls, [('/enterprise/v3/sharing',
                                  {'contributions_default': 'private', 'apply_to_existing': True,
                                   'confirm': token})])
        service = Service()
        calls, output = self._run('sharing', '--set', 'private', '--apply-to-existing', '--yes', service=service)
        self.assertEqual([c[1] for c in calls], [
            {'contributions_default': 'private', 'apply_to_existing': True},
            {'contributions_default': 'private', 'apply_to_existing': True, 'confirm': token},
        ])

    def test_apply_to_existing_is_only_valid_with_set_private(self):
        class Service:
            def request(self, path, value=None):
                raise AssertionError('server should not be called')
        with self.assertRaises(SystemExit):
            self._run('sharing', '--set', 'shared', '--apply-to-existing', service=Service())


class ScopeFileWarningHookTests(unittest.TestCase):
    """A one-time scope-file warning becomes a one-time status-line message."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'profile'
        self.home.mkdir()
        self.repo = self.tmp / 'maple'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / 'src').mkdir()
        (self.home / 'config.json').write_text(json.dumps({'projects': {}, 'sessions': {},
            'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2',
                                  'capture_owner': 'hooks', 'api_version': 'cloud-local-1',
                                  'connection_id': 'synthetic-connection'}}))

    def test_workspace_with_wider_sharing_warns_once(self):
        from agentclient.hooks import handle

        class Backend:
            def __init__(self):
                self.calls = []

            def request(self, path, value=None):
                self.calls.append(path)
                if path.endswith('preferences'):
                    return {'values': {}, 'preferences': []}
                if path.endswith('search'):
                    return {'answerable': False, 'support': 'none', 'results': []}
                return {'status': 'recorded'}

        (self.repo / '.vaelius').write_text('workspace = "maple"\nsharing = "team"\n')

        class StubOutbox:
            events = []

            def __init__(self, home):
                pass

            def queue_diagnostics(self, config):
                pass

            def put(self, event):
                StubOutbox.events.append(event)

            def drain(self, *args, **kwargs):
                return {'notices': [], 'acknowledged_events': 1, 'blocked': False, 'pending_events': 0,
                        'pending_bytes': 0, 'oldest_age_seconds': 0, 'max_bytes': 0, 'max_age_seconds': 0,
                        'gaps': {}, 'searchable': False, 'local_corpus_opened': False}

            def close(self):
                pass

        with patch('agentclient.transport.enterprise_client', return_value=Backend()), \
                patch('agentclient.capture_outbox.Outbox', StubOutbox):
            first = handle(self.home, {'hook_event_name': 'UserPromptSubmit',
                                       'cwd': str(self.repo / 'src'), 'session_id': 's1',
                                       'prompt': 'Hello'})
            second = handle(self.home, {'hook_event_name': 'UserPromptSubmit',
                                        'cwd': str(self.repo / 'src'), 'session_id': 's2',
                                        'prompt': 'Hello again'})
        self.assertIn('workspace', first['systemMessage'])
        self.assertIn('ignored', first['systemMessage'])
        # Second session for the same scope root does not repeat the warning.
        self.assertNotIn('ignored', second.get('systemMessage', '') or '')
        # The capture itself carries the workspace-model sharing on the wire.
        self.assertEqual(StubOutbox.events[0]['sharing'], 'organization')
        self.assertEqual(StubOutbox.events[0]['workspace'], 'maple')


if __name__ == '__main__':
    unittest.main()
