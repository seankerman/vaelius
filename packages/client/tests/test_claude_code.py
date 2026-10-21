"""Claude Code client: hook events, self-capture exclusion, header helper, installer and plugin.

Synthetic payloads shaped like Claude Code's documented hook input, fictional
paths and a stub backend; no service, model or real Claude Code configuration.
"""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

PLUGIN = Path(__file__).resolve().parents[3] / 'integrations' / 'claude-code'
SESSION = '6f1d2c3b-0000-4000-8000-00000000c1a0'
PROMPT = '9a8b7c6d-0000-4000-8000-0000000000aa'


def payload(kind, cwd, **fields):
    """Common Claude Code hook input; Claude Code sends prompt_id, never turn_id."""
    value = {'session_id': SESSION, 'transcript_path': '/fictional/.claude/projects/p/' + SESSION + '.jsonl',
             'cwd': str(cwd), 'permission_mode': 'default', 'hook_event_name': kind}
    if kind not in ('SessionStart',):
        value['prompt_id'] = PROMPT
    value.update(fields)
    return value


FIXTURES = {
    'SessionStart': {'source': 'startup', 'model': 'synthetic-model'},
    'UserPromptSubmit': {'prompt': 'Where is the Maple import file kept?'},
    'PostToolUse': {'tool_name': 'Bash', 'tool_input': {'command': 'ls data'},
                    'tool_response': {'stdout': 'maple.csv', 'stderr': '', 'interrupted': False},
                    'tool_use_id': 'toolu_01synthetic', 'duration_ms': 12},
    'PostToolUseFailure': {'tool_name': 'Bash', 'tool_input': {'command': 'false'},
                           'tool_use_id': 'toolu_02synthetic', 'error': 'Command exited with non-zero status code 1',
                           'is_interrupt': False},
    'Stop': {'stop_hook_active': False, 'last_assistant_message': 'The Maple import file is data/maple.csv.',
             'background_tasks': [], 'session_crons': []},
    'StopFailure': {'error': 'rate_limit', 'error_details': '429 Too Many Requests',
                    'last_assistant_message': 'API Error: Rate limit reached'},
    'PostCompact': {'trigger': 'manual', 'compact_summary': 'Summary of the synthetic conversation.'},
    'SessionEnd': {'reason': 'prompt_input_exit'},
}


class Backend:
    """Accepts outbox parts and answers retrieval with one synthetic card."""
    def __init__(self):
        self.calls = []

    def request(self, path, value=None):
        self.calls.append((path, value))
        if path == '/enterprise/v2/parts':
            return {'disposition': 'accepted', 'digest': value['digest'], 'received_parts': 1,
                    'complete': True, 'source_id': 'src-' + value['digest'][:8]}
        if path.endswith('capture-notice'):
            raise OSError('no operator notice')
        if path.endswith('preferences'):
            return {'values': {}, 'preferences': []}
        if path.endswith('search'):
            return {'answerable': True, 'support': 'complete', 'results': [
                {'id': 'doc-1', 'revision': 'r1', 'title': 'Maple import', 'lesson': 'data/maple.csv',
                 'provenance': {'author': 'alice', 'project': 'demo', 'wording': 'original'}}]}
        return {'status': 'recorded'}

    def delivered(self):
        events = []
        for path, value in self.calls:
            if path == '/enterprise/v2/parts':
                events.append(json.loads(base64.b64decode(value['data'])))
        return events


class Profile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home = base / 'profile';self.home.mkdir(mode=0o700)
        self.project = base / 'repo';self.project.mkdir()
        self.other = base / 'unrelated';self.other.mkdir()
        self.token = self.home / 'credential'
        self.token.write_text('synthetic-access-token\n');self.token.chmod(0o600)
        self.config = {'paused': False, 'projects': {str(self.project): 'demo'}, 'sessions': {},
                       'knowledge_backend': {'mode': 'enterprise_local', 'api_version': 'cloud-local-1',
                           'capture_version': 'enterprise-local-2', 'capture_owner': 'transcript',
                           'url': 'http://127.0.0.1:65530', 'credential_file': str(self.token),
                           'connection_id': 'claude-code-synthetic'}}
        self.save()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        path = self.home / 'config.json';path.write_text(json.dumps(self.config));path.chmod(0o600)

    def hook(self, kind, backend=None, *, cwd=None, raises=None, **fields):
        from agentclient.hooks import handle
        event = payload(kind, cwd or self.project, **dict(FIXTURES.get(kind, {}), **fields))
        side = {'side_effect': raises} if raises else {'return_value': backend}
        with patch('agentclient.transport.enterprise_client', **side):
            return handle(self.home, event, host='claude-code')

    def outbox(self):
        from agentclient.capture_outbox import Outbox
        box = Outbox(self.home)
        try:
            rows = box.db.execute('SELECT payload FROM pending ORDER BY created').fetchall()
            return [json.loads(row['payload']) for row in rows]
        finally:box.close()


class HookEventTests(Profile):
    def test_each_documented_event_is_handled_with_prompt_id_as_turn(self):
        backend = Backend()
        outputs = {kind: self.hook(kind, backend) for kind in FIXTURES}
        events = backend.delivered()
        by_kind = {event['event']['kind']: event for event in events}
        # Capture comes from hooks even though this profile's Codex capture owner is 'transcript'.
        self.assertEqual(set(by_kind), {'UserPromptSubmit', 'PostToolUse', 'PostToolUseFailure', 'Stop',
                                        'StopFailure', 'PostCompact'})
        self.assertEqual(len(events), 6)
        for event in events:
            self.assertEqual(event['turn'], PROMPT)
            self.assertEqual(event['conversation'], SESSION)
            self.assertEqual(event['project'], 'demo')
        self.assertEqual(by_kind['UserPromptSubmit']['blocks'][0]['value'], FIXTURES['UserPromptSubmit']['prompt'])
        self.assertEqual(by_kind['Stop']['event']['role'], 'assistant')
        self.assertTrue(by_kind['Stop']['event']['complete'])
        self.assertEqual(by_kind['Stop']['blocks'][0]['value'], FIXTURES['Stop']['last_assistant_message'])
        self.assertEqual(by_kind['PostToolUse']['external_id'], 'toolu_01synthetic')
        failure = by_kind['PostToolUseFailure']
        self.assertEqual(failure['event']['role'], 'tool')
        self.assertEqual(failure['blocks'][1]['value']['error'], FIXTURES['PostToolUseFailure']['error'])
        stop_failure = by_kind['StopFailure']
        self.assertEqual(stop_failure['disposition'], 'incomplete')
        self.assertEqual(stop_failure['blocks'], [{'type': 'record_fields', 'value': {'turn_failure': 'rate_limit'}}])
        self.assertNotIn('Rate limit reached', json.dumps(stop_failure))
        self.assertNotIn('Summary of the synthetic', json.dumps(by_kind['PostCompact']))
        # Only the two context events answer with context, and only as reference data.
        self.assertEqual(outputs['UserPromptSubmit']['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('not instructions', outputs['UserPromptSubmit']['hookSpecificOutput']['additionalContext'])
        for kind in ('PostToolUse', 'PostToolUseFailure', 'Stop', 'StopFailure', 'PostCompact', 'SessionEnd'):
            self.assertNotIn('hookSpecificOutput', outputs[kind], kind)
            self.assertNotIn('decision', outputs[kind], kind)
        receipts = [value for path, value in backend.calls if path == '/enterprise/v1/receipts']
        self.assertEqual(receipts[0]['request_id'], PROMPT)
        boundary = [value for path, value in backend.calls if path == '/enterprise/v1/boundary']
        self.assertEqual(boundary[0]['event']['turn_id'], PROMPT)
        self.assertEqual(self.outbox(), [])

    def test_codex_turn_id_still_wins_over_prompt_id(self):
        from agentclient.enterprise_capture import normalize_capture, turn_of
        self.assertEqual(turn_of({'turn_id': 'codex-turn', 'prompt_id': PROMPT}), 'codex-turn')
        self.assertEqual(turn_of({'prompt_id': PROMPT}), PROMPT)
        self.assertEqual(turn_of({}), '')
        value = normalize_capture(payload('Stop', self.project, last_assistant_message='done'), 'demo', 'c')
        self.assertEqual(value['turn'], PROMPT)

    def test_session_end_flushes_queue_without_capturing(self):
        self.hook('UserPromptSubmit', raises=URLError('offline'))
        self.assertEqual(len(self.outbox()), 1)
        backend = Backend()
        self.assertEqual(self.hook('SessionEnd', backend), {})
        delivered = backend.delivered()
        self.assertEqual([event['event']['kind'] for event in delivered], ['UserPromptSubmit'])
        self.assertEqual(self.outbox(), [])

    def test_unenrolled_folder_is_silent_and_captures_nothing(self):
        backend = Backend()
        for kind in FIXTURES:
            self.assertEqual(self.hook(kind, backend, cwd=self.other), {}, kind)
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.outbox(), [])

    def test_codex_host_keeps_its_capture_owner(self):
        from agentclient.hooks import handle
        backend = Backend()
        with patch('agentclient.transport.enterprise_client', return_value=backend):
            handle(self.home, payload('Stop', self.project, **FIXTURES['Stop']))
        self.assertEqual(backend.delivered(), [])


class SelfCaptureTests(Profile):
    SELF = ['mcp__vaelius__search_memory', 'mcp__vaelius__fetch_memory',
            'mcp__plugin_vaelius_vaelius__search_memory', 'mcp__plugin_vaelius_vaelius__withdraw_memory',
            'MCP__Vaelius__Search_Memory', 'mcp__agentnetwork_memory__search_memory', 'agentnetwork_memory',
            'agentnetwork-memory', 'mcp__agentnetwork__agentnetwork_memory']
    OTHER = ['Bash', 'mcp__github__search_code', 'mcp__plugin_other_vaelius__search_memory',
             'mcp__vaelius_notes__search_memory', 'search_memory', 'mcp__memory__create_entities']

    def test_vaelius_tool_names_are_never_captured(self):
        from agentclient.enterprise_capture import normalize_capture
        for name in self.SELF:
            value = normalize_capture(payload('PostToolUse', self.project, tool_name=name,
                tool_input={'query': 'q'}, tool_response={'records': [{'lesson': 'retrieved memory text'}]},
                tool_use_id='t-' + name), 'demo', 'c')
            self.assertEqual(value['disposition'], 'excluded', name)
            self.assertEqual(value['blocks'], [], name)
            self.assertNotIn('retrieved memory text', json.dumps(value), name)
        for name in self.OTHER:
            value = normalize_capture(payload('PostToolUse', self.project, tool_name=name,
                tool_input={'q': 1}, tool_response={'ok': True}, tool_use_id='t-' + name), 'demo', 'c')
            self.assertEqual(value['disposition'], 'accepted', name)

    def test_failed_memory_tool_call_is_also_excluded(self):
        backend = Backend()
        self.hook('PostToolUseFailure', backend, tool_name='mcp__plugin_vaelius_vaelius__fetch_memory',
                  tool_input={'id': 'doc-1'}, error='Memory request unavailable')
        [event] = backend.delivered()
        self.assertEqual((event['disposition'], event['blocks']), ('excluded', []))

    def test_installed_and_plugin_server_names_match_the_exclusion(self):
        from agentclient import claude_code
        from agentclient.enterprise_capture import self_tool
        manifest = json.loads((PLUGIN / '.claude-plugin/plugin.json').read_text())
        [server] = json.loads((PLUGIN / '.mcp.json').read_text())['mcpServers']
        self.assertTrue(self_tool('mcp__plugin_' + manifest['name'] + '_' + server + '__search_memory'))
        self.assertTrue(self_tool('mcp__' + claude_code.SERVER + '__search_memory'))


class StatusTests(Profile):
    def test_signed_out_session_start_says_so_and_capture_keeps_queuing(self):
        from agentclient.credentials import LoginRequired
        result = self.hook('SessionStart', raises=LoginRequired())
        self.assertIn('Vaelius capture ON for project demo', result['systemMessage'])
        self.assertIn('Vaelius signed out', result['systemMessage'])
        self.assertNotIn('hookSpecificOutput', result)
        for kind in ('UserPromptSubmit', 'PostToolUse', 'Stop'):
            self.assertNotIn('hookSpecificOutput', self.hook(kind, raises=LoginRequired()))
        self.assertEqual([e['event']['kind'] for e in self.outbox()], ['UserPromptSubmit', 'PostToolUse', 'Stop'])
        gaps = self.home / 'enterprise-capture-gaps.jsonl'
        reasons = [json.loads(line)['reason'] for line in gaps.read_text().splitlines()] if gaps.exists() else []
        # Being signed out is not a capture gap: nothing was lost.
        self.assertNotIn('structured_capture_backpressure_or_invalid', reasons)
        second = self.hook('SessionStart', raises=LoginRequired())
        self.assertIn('(3 pending)', second['systemMessage'])
        backend = Backend()
        self.hook('SessionStart', backend)
        self.assertEqual(len(backend.delivered()), 3)
        self.assertEqual(self.outbox(), [])

    def test_aws_profile_names_the_sign_in_command(self):
        from agentclient.aws_sts import SignedOut
        self.config['knowledge_backend']['aws_sts'] = {'profile': 'synthetic-sso', 'region': 'us-west-2', 'server_id': 'x'}
        self.save()
        result = self.hook('SessionStart', raises=SignedOut())
        self.assertIn('aws sso login --profile synthetic-sso', result['systemMessage'])

    def test_rejected_credential_and_unreachable_service_are_distinguished(self):
        from agentclient.hooks import service_problem
        self.assertEqual(service_problem(HTTPError('u', 401, 'unauthorized', {}, None)), 'signed_out')
        self.assertEqual(service_problem(HTTPError('u', 503, 'down', {}, None)), 'unavailable')
        self.assertEqual(service_problem(URLError('refused')), 'unreachable')
        self.assertIn('unreachable', self.hook('SessionStart', raises=URLError('refused'))['systemMessage'])

    def test_paused_and_missing_profile_report_off(self):
        self.config['paused'] = True;self.save()
        self.assertIn('OFF (paused)', self.hook('SessionStart', Backend())['systemMessage'])
        from agentclient.hooks import handle
        empty = Path(self.tmp.name) / 'no-profile'
        result = handle(empty, payload('SessionStart', self.project), host='claude-code', via='plugin')
        self.assertIn('not signed in', result['systemMessage'])
        self.assertEqual(handle(empty, payload('Stop', self.project), host='claude-code', via='plugin'), {})


class LabelTests(Profile):
    def test_global_capture_labels_are_host_neutral_and_old_key_still_reads(self):
        from agentclient.hooks import fallback_binding
        common = Path('/fictional/src/maple/.git')
        self.assertIsNone(fallback_binding({}, 's', common))
        new = fallback_binding({'capture_all_sessions': True}, 's', common)
        self.assertTrue(new.startswith('Repo:maple:'), new)
        self.assertTrue(fallback_binding({'capture_all_sessions': True}, 's', None).startswith('Chat:'))
        old = fallback_binding({'capture_all_codex_sessions': True}, 's', common)
        self.assertEqual(old, 'Code' + 'x' + new)
        self.assertIsNone(fallback_binding({'capture_all_sessions': False, 'capture_all_codex_sessions': True}, 's', common))
        # A project disabled under its old name stays disabled after the rename.
        self.assertIsNone(fallback_binding({'capture_all_sessions': True, 'capture_disabled_projects': [old]}, 's', common))

    def test_new_profiles_write_the_host_neutral_key(self):
        import inspect
        from agentclient import aws_sts, cloud_enroll, oauth
        for module in (aws_sts, cloud_enroll, oauth):
            source = inspect.getsource(module)
            self.assertNotIn('capture_all_codex_sessions', source, module.__name__)
            self.assertIn('capture_all_sessions', source, module.__name__)


class HeaderHelperTests(Profile):
    def run_cli(self, *args, env=None):
        from agentclient.cli import main
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.dict(os.environ, env or {}, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:main(['--home', str(self.home), *args])
            except SystemExit as exit:code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_prints_only_json_headers_for_this_profile_service(self):
        url = {'CLAUDE_CODE_MCP_SERVER_URL': 'http://127.0.0.1:65530/mcp'}
        (self.project / 'sub').mkdir()
        code, out, err = self.run_cli('mcp-headers', '--project-dir', str(self.project / 'sub'), env=url)
        self.assertEqual((code, err), (0, ''))
        headers = json.loads(out)
        self.assertEqual(headers['Authorization'], 'Bearer synthetic-access-token')
        self.assertEqual(headers['X-AgentNetwork-Project'], 'demo')
        self.assertTrue(headers['X-AgentNetwork-Session'].startswith('mcp-'))
        self.assertTrue(all(isinstance(v, str) for v in headers.values()))

    def test_token_is_never_released_to_another_url(self):
        for url in ('https://memory.example.invalid/mcp', 'http://127.0.0.1:65531/mcp', 'http://127.0.0.1:65530/other'):
            code, out, err = self.run_cli('mcp-headers', '--project', 'demo', env={'CLAUDE_CODE_MCP_SERVER_URL': url})
            self.assertEqual((code, out), (1, ''), url)
            self.assertIn('mcp_url_not_this_profile_service', err)
            self.assertNotIn('synthetic-access-token', err)

    def test_signed_out_and_unenrolled_fail_closed_with_a_reason(self):
        self.token.unlink()
        code, out, err = self.run_cli('mcp-headers', '--project', 'demo')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('signed out', err)
        self.token.write_text('synthetic-access-token\n');self.token.chmod(0o600)
        code, out, err = self.run_cli('mcp-headers', '--project', 'not-enrolled')
        self.assertEqual((code, out), (1, ''))
        self.assertIn('project_not_enrolled', err)


class InstallerTests(Profile):
    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name).resolve()
        self.claude = base / 'claude-config';self.claude.mkdir()
        self.settings = self.claude / 'settings.json'
        self.state = self.claude / '.claude.json'
        self.settings.write_text(json.dumps({'model': 'synthetic', 'permissions': {'allow': ['Bash(ls)']},
            'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'other-tool prompt'}]}],
                      'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'guard'}]}]},
            'pluginConfigs': {'x@y': {'options': {'a': 1}}}}, indent=2) + '\n')
        self.state.write_text(json.dumps({'numStartups': 3, 'mcpServers': {'github': {'type': 'http', 'url': 'https://mcp.example.invalid'}},
            'projects': {str(self.project): {'allowedTools': [], 'mcpServers': {'notes': {'command': 'notes-mcp'}}},
                         str(self.other): {'hasTrustDialogAccepted': True}}}, indent=2) + '\n')
        self.original = {p: p.read_text() for p in (self.settings, self.state)}

    def install(self, **options):
        from agentclient.claude_code import install
        return install(self.home, claude_config_dir=self.claude, **options)

    def test_user_install_is_idempotent_and_keeps_unrelated_settings(self):
        from agentclient.claude_code import EVENTS, hook_command
        result = self.install()
        self.assertEqual(result['mcp_projects'], ['demo'])
        first = {p: p.read_text() for p in (self.settings, self.state)}
        self.install()
        self.assertEqual({p: p.read_text() for p in (self.settings, self.state)}, first)
        settings = json.loads(first[self.settings]);state = json.loads(first[self.state])
        command = hook_command(self.home)
        for event in EVENTS:
            mine = [h for g in settings['hooks'][event] for h in g['hooks'] if h['command'] == command]
            self.assertEqual(len(mine), 1, event)
        self.assertIn({'hooks': [{'type': 'command', 'command': 'other-tool prompt'}]}, settings['hooks']['UserPromptSubmit'])
        before = json.loads(self.original[self.settings]);before_state = json.loads(self.original[self.state])
        for key in ('model', 'permissions', 'pluginConfigs'):
            self.assertEqual(settings[key], before[key])
        self.assertEqual(settings['hooks']['PreToolUse'], before['hooks']['PreToolUse'])
        servers = state['projects'][str(self.project)]['mcpServers']
        self.assertEqual(servers['notes'], {'command': 'notes-mcp'})
        self.assertEqual(servers['vaelius']['type'], 'http')
        self.assertEqual(servers['vaelius']['url'], 'http://127.0.0.1:65530/mcp')
        helper = shlex.split(servers['vaelius']['headersHelper'])
        self.assertEqual(helper[-3:], ['mcp-headers', '--project', 'demo'])
        self.assertNotIn('synthetic-access-token', first[self.settings] + first[self.state])
        self.assertEqual(state['mcpServers'], before_state['mcpServers'])
        self.assertEqual(state['projects'][str(self.other)], before_state['projects'][str(self.other)])
        self.assertEqual(state['numStartups'], 3)
        backups = list((self.home / 'claude-code-backups').iterdir())
        self.assertEqual(sorted(b.read_text() for b in backups), sorted(self.original.values()))

    def test_uninstall_restores_only_what_was_added(self):
        self.install()
        # Claude Code and the user keep editing after installation.
        state = json.loads(self.state.read_text());state['numStartups'] = 4
        self.state.write_text(json.dumps(state))
        settings = json.loads(self.settings.read_text());settings['theme'] = 'dark'
        self.settings.write_text(json.dumps(settings))
        from agentclient.claude_code import uninstall
        removed = uninstall(self.home)
        self.assertEqual(len(removed), 1)
        expected = json.loads(self.original[self.settings]);expected['theme'] = 'dark'
        self.assertEqual(json.loads(self.settings.read_text()), expected)
        expected_state = json.loads(self.original[self.state]);expected_state['numStartups'] = 4
        self.assertEqual(json.loads(self.state.read_text()), expected_state)
        self.assertEqual(uninstall(self.home), [])

    def test_uninstall_keeps_entries_someone_else_changed(self):
        from agentclient.claude_code import uninstall, hook_command
        self.install()
        state = json.loads(self.state.read_text())
        edited = dict(state['projects'][str(self.project)]['mcpServers']['vaelius'], timeout=90000)
        state['projects'][str(self.project)]['mcpServers']['vaelius'] = edited
        self.state.write_text(json.dumps(state))
        settings = json.loads(self.settings.read_text())
        changed = dict(settings['hooks']['Stop'][-1]['hooks'][0], command=hook_command(self.home) + ' --verbose')
        settings['hooks']['Stop'].append({'hooks': [changed]})
        self.settings.write_text(json.dumps(settings))
        uninstall(self.home)
        self.assertEqual(json.loads(self.state.read_text())['projects'][str(self.project)]['mcpServers']['vaelius'], edited)
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['Stop'], [{'hooks': [changed]}])

    def test_project_scope_creates_and_removes_its_local_settings(self):
        from agentclient.claude_code import uninstall, settings_install_covers
        result = self.install(scope='project', project_root=self.project / '.')
        local = self.project / '.claude' / 'settings.local.json'
        self.assertEqual(result['settings_file'], str(local))
        self.assertTrue(local.exists())
        self.assertEqual(self.settings.read_text(), self.original[self.settings])
        self.assertTrue(settings_install_covers(self.home, str(self.project / 'src')))
        self.assertFalse(settings_install_covers(self.home, str(self.other)))
        uninstall(self.home, scope='project')
        self.assertFalse(local.exists())
        self.assertEqual(json.loads(self.state.read_text()), json.loads(self.original[self.state]))

    def test_refusals_change_nothing(self):
        with self.assertRaises(ValueError):
            self.install(scope='project', project_root=self.other)
        state = json.loads(self.state.read_text())
        state['projects'][str(self.project)]['mcpServers']['vaelius'] = {'type': 'http', 'url': 'https://someone-else.invalid/mcp'}
        self.state.write_text(json.dumps(state))
        snapshot = {p: p.read_text() for p in (self.settings, self.state)}
        with self.assertRaisesRegex(ValueError, 'not_owned'):
            self.install()
        self.assertEqual({p: p.read_text() for p in (self.settings, self.state)}, snapshot)
        self.assertFalse((self.home / 'claude-code-installation.json').exists())

    def test_interpreter_change_replaces_only_the_recorded_command(self):
        from agentclient.claude_code import EVENTS
        self.install()
        old = shlex.join(['/old/python', '-m', 'agentclient.cli', '--home', str(self.home), 'hook', '--host', 'claude-code'])
        receipt = self.home / 'claude-code-installation.json'
        data = json.loads(receipt.read_text())
        [key] = data['installs']
        current = data['installs'][key]['command']
        self.settings.write_text(self.settings.read_text().replace(json.dumps(current)[1:-1], old.replace('"', '\\"')))
        data['installs'][key]['command'] = old;receipt.write_text(json.dumps(data))
        self.install()
        text = self.settings.read_text()
        self.assertNotIn('/old/python', text)
        settings = json.loads(text)
        for event in EVENTS:
            self.assertEqual(sum(h['command'] == current for g in settings['hooks'][event] for h in g['hooks']), 1)

    def test_cli_install_and_uninstall_for_claude_code_only(self):
        from agentclient.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['--home', str(self.home), 'install', '--claude-code', '--claude-config-dir', str(self.claude)])
        self.assertEqual(json.loads(out.getvalue())['host'], 'claude-code')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['--home', str(self.home), 'uninstall', '--claude-code'])
        self.assertEqual(len(json.loads(out.getvalue())['removed']), 1)
        self.assertFalse(json.loads((self.home / 'config.json').read_text())['paused'])
        self.assertEqual(json.loads(self.settings.read_text()), json.loads(self.original[self.settings]))

    def test_installed_commands_run_the_hook_and_helper(self):
        self.install()
        from agentclient.claude_code import hook_command
        settings = json.loads(self.settings.read_text())
        self.assertIn(hook_command(self.home), json.dumps(settings))
        helper = json.loads(self.state.read_text())['projects'][str(self.project)]['mcpServers']['vaelius']['headersHelper']
        env = dict(os.environ, CLAUDE_CODE_MCP_SERVER_URL='http://127.0.0.1:65530/mcp')
        run = subprocess.run(helper, shell=True, capture_output=True, text=True, env=env, cwd=self.claude, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['X-AgentNetwork-Project'], 'demo')
        hook = subprocess.run(hook_command(self.home), shell=True, capture_output=True, text=True, timeout=60,
                              input=json.dumps(payload('SessionStart', self.other)))
        self.assertEqual((hook.returncode, json.loads(hook.stdout)), (0, {}))


class PluginTests(Profile):
    def test_manifest_hooks_and_mcp_agree_with_the_client(self):
        from agentclient import claude_code
        manifest = json.loads((PLUGIN / '.claude-plugin/plugin.json').read_text())
        client = tomllib.loads((PLUGIN.parents[1] / 'packages/client/pyproject.toml').read_text())['project']
        self.assertEqual(manifest['version'], client['version'])
        self.assertEqual(manifest['name'], 'vaelius')
        hooks = json.loads((PLUGIN / 'hooks/hooks.json').read_text())['hooks']
        self.assertEqual(set(hooks), set(claude_code.EVENTS))
        for event, groups in hooks.items():
            [handler] = [h for g in groups for h in g['hooks']]
            self.assertEqual(handler['command'], '"${CLAUDE_PLUGIN_ROOT}"/scripts/vaelius-claude-code hook')
            self.assertLessEqual(handler['timeout'], 10)
        server = json.loads((PLUGIN / '.mcp.json').read_text())['mcpServers']['vaelius']
        self.assertEqual(server['type'], 'http')
        self.assertEqual(server['url'], '${user_config.server_url}/mcp')
        self.assertIn('server_url', manifest['userConfig'])
        # Claude Code refuses ${user_config.*} in a shell-run headersHelper.
        self.assertNotIn('user_config', server['headersHelper'])
        self.assertIn('mcp-headers --project-dir "${CLAUDE_PROJECT_DIR}"', server['headersHelper'])
        market = json.loads((PLUGIN.parents[1] / '.claude-plugin/marketplace.json').read_text())
        self.assertEqual(market['plugins'][0]['source'], './integrations/claude-code')

    def wrapper(self, mode, stdin='', **env):
        script = PLUGIN / 'scripts/vaelius-claude-code'
        environment = {'PATH': '/usr/bin:/bin', **env}
        return subprocess.run([str(script), *mode], input=stdin, capture_output=True, text=True, env=environment, timeout=30)

    def test_wrapper_without_client_is_quiet_except_once_per_session(self):
        start = self.wrapper(['hook'], json.dumps(payload('SessionStart', self.project)))
        self.assertEqual(start.returncode, 0)
        self.assertIn('not installed', json.loads(start.stdout)['systemMessage'])
        tool = self.wrapper(['hook'], json.dumps(payload('PostToolUse', self.project, **FIXTURES['PostToolUse'])))
        self.assertEqual((tool.returncode, tool.stdout), (0, ''))
        headers = self.wrapper(['mcp-headers', '--project-dir', str(self.project)])
        self.assertEqual((headers.returncode, headers.stdout), (1, ''))

    def test_wrapper_forwards_profile_and_host(self):
        stub = Path(self.tmp.name) / 'stub-client'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\ncat >/dev/null\n');stub.chmod(0o755)
        hook = self.wrapper(['hook'], '{}', VAELIUS_CLIENT=str(stub), VAELIUS_CLIENT_HOME=str(self.home))
        self.assertEqual(hook.stdout.split('\n')[:-1], ['--home', str(self.home), 'hook', '--host', 'claude-code', '--via', 'plugin'])
        helper = self.wrapper(['mcp-headers', '--project-dir', '/fictional dir'], VAELIUS_CLIENT=str(stub))
        self.assertEqual(helper.stdout.split('\n')[:-1], ['mcp-headers', '--project-dir', '/fictional dir'])


if __name__ == '__main__':
    unittest.main()
