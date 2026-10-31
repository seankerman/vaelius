"""`vaelius-client push-session`: one past conversation, owner-private, replayed under the live rules.

A fictional home folder, Claude Code configuration folder and Codex home in a
temporary directory, synthetic transcripts and a stub backend. The real
~/.claude and ~/.codex are never read.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_claude_code import Backend

SESSION = '7a1d2c3b-0000-4000-8000-00000000d1a0'
CODEX_SESSION = '0199aa00-0000-7000-8000-00000000c0de'


def entry(kind, cwd, stamp, **fields):
    value = {'type': kind, 'sessionId': SESSION, 'cwd': str(cwd), 'timestamp': stamp, 'uuid': 'u-' + stamp[-6:]}
    value.update(fields)
    return value


def prompt(cwd, stamp, text):
    return entry('user', cwd, stamp, message={'role': 'user', 'content': text})


def tool(cwd, stamp, ident, name, arguments):
    return entry('assistant', cwd, stamp, message={'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': ident, 'name': name, 'input': arguments}]})


def result(cwd, stamp, ident, text, structured=None, **fields):
    value = entry('user', cwd, stamp, message={'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': ident, 'content': text}]}, **fields)
    if structured is not None:
        value['toolUseResult'] = structured
    return value


def reply(cwd, stamp, text):
    return entry('assistant', cwd, stamp, message={'role': 'assistant', 'content': [{'type': 'text', 'text': text}]})


class PushFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.homedir = base / 'home'
        self.repo = self.homedir / 'src/maple';self.repo.mkdir(parents=True)
        (self.repo / '.vaelius').write_text('workspace = "maple"\ncapture = "on"\n')
        (self.repo / 'src').mkdir();(self.repo / 'src/app.py').write_text('print(1)\n')
        self.birch = self.homedir / 'src/birch';self.birch.mkdir(parents=True)
        (self.birch / '.vaelius').write_text('workspace = "birch"\ncapture = "on"\n')
        (self.birch / 'notes.md').write_text('birch notes\n')
        self.loose = self.homedir / 'scratch';self.loose.mkdir()
        (self.homedir / 'Documents').mkdir();(self.homedir / 'Documents/x.txt').write_text('personal\n')
        self.claude = self.homedir / '.claude';self.codex = self.homedir / '.codex'
        self.profile = base / 'profile';self.profile.mkdir(mode=0o700)
        token = self.profile / 'credential';token.write_text('synthetic-access-token\n');token.chmod(0o600)
        self.config = {'paused': False, 'projects': {}, 'sessions': {},
                       'knowledge_backend': {'mode': 'enterprise_local', 'api_version': 'cloud-local-1',
                           'capture_version': 'enterprise-local-2', 'capture_owner': 'transcript',
                           'url': 'http://127.0.0.1:65530', 'credential_file': str(token),
                           'connection_id': 'claude-code-synthetic'}}
        (self.profile / 'config.json').write_text(json.dumps(self.config))
        isolation = [patch.dict(os.environ, {'HOME': str(self.homedir), 'CLAUDE_CONFIG_DIR': str(self.claude),
                                             'CODEX_HOME': str(self.codex), 'TMPDIR': str(base / 'tmp')}),
                     patch('agentclient.push_session._role', return_value='member')]
        (base / 'tmp').mkdir()
        for item in isolation:
            item.start();self.addCleanup(item.stop)
        self.backend = Backend()

    def write_claude(self, rows, *, cwd=None, session=SESSION, subagent=None):
        from agentclient.origin import slug
        folder = self.claude / 'projects' / slug(str(cwd or self.repo));folder.mkdir(parents=True, exist_ok=True)
        path = folder / (session + '.jsonl')
        path.write_text(''.join(json.dumps(dict(r, sessionId=session)) + '\n' for r in rows))
        if subagent:
            sub = folder / session / 'subagents';sub.mkdir(parents=True)
            (sub / 'agent-a1.jsonl').write_text(''.join(json.dumps(dict(r, isSidechain=True)) + '\n' for r in subagent))
        return path

    def session_rows(self):
        r, d = self.repo, str(self.homedir / 'Documents/x.txt')
        return [prompt(r, '2026-05-01T10:00:00.000001Z', 'Where is the app entry point?'),
                tool(r, '2026-05-01T10:00:01.000002Z', 't1', 'Read', {'file_path': str(r / 'src/app.py')}),
                result(r, '2026-05-01T10:00:02.000003Z', 't1', 'print(1)', {'type': 'text'}),
                reply(r, '2026-05-01T10:00:03.000004Z', 'It is src/app.py.'),
                prompt(r, '2026-05-01T10:01:00.000005Z', 'Check my notes too.'),
                tool(r, '2026-05-01T10:01:01.000006Z', 't2', 'Read', {'file_path': d}),
                result(r, '2026-05-01T10:01:02.000007Z', 't2', 'personal'),
                tool(r, '2026-05-01T10:01:03.000008Z', 't3', 'Bash', {'command': 'ls src'}),
                result(r, '2026-05-01T10:01:04.000009Z', 't3', 'app.py'),
                reply(r, '2026-05-01T10:01:05.000010Z', 'Done; nothing else to change.')]

    def push(self, target=SESSION, *, apply=False):
        from agentclient import push_session
        with patch('agentclient.transport.enterprise_client', return_value=self.backend):
            return push_session.push(self.config, self.profile, target, apply=apply)

    def queued(self):
        from agentclient.capture_outbox import Outbox
        box = Outbox(self.profile)
        try:
            return [json.loads(r['payload']) for r in box.db.execute('SELECT payload FROM pending ORDER BY created')]
        finally:
            box.close()


class PushSessionTests(PushFixture):
    def test_dry_run_counts_only_and_sends_nothing(self):
        path = self.write_claude(self.session_rows())
        report = self.push()
        self.assertTrue(report['dry_run'])
        self.assertEqual((report['host'], report['workspace'], report['sharing'], report['role']),
                         ('claude-code', 'maple', 'private', 'member'))
        self.assertEqual(report['counts']['by_kind'], {'UserPromptSubmit': 2, 'PostToolUse': 3, 'Stop': 2})
        self.assertEqual(report['counts']['tainted_private'], 3)
        for text in ('app entry point', 'personal', 'Documents', 'print(1)'):
            self.assertNotIn(text, json.dumps(report))
        self.assertEqual([c for c in self.backend.calls if c[0] == '/enterprise/v2/parts'], [])
        self.assertEqual(self.queued(), [])
        self.assertFalse((self.profile / 'pushed-sessions.json').exists())
        self.assertTrue(path.exists())

    def test_in_scope_push_lands_private_with_taint_from_the_personal_read_on(self):
        path = self.write_claude(self.session_rows())
        before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
        report = self.push(apply=True)
        self.assertEqual(report['sent'], {'queued': 7, 'acknowledged': 7, 'remaining': 0})
        events = self.backend.delivered()
        self.assertEqual(len(events), 7)
        kinds = [e['event']['kind'] for e in events]
        self.assertEqual(kinds, ['UserPromptSubmit', 'PostToolUse', 'Stop', 'UserPromptSubmit', 'PostToolUse',
                                 'PostToolUse', 'Stop'])
        for event in events:
            # Owner-private whatever the workspace default: no sharing request is ever sent.
            self.assertNotIn('sharing', event)
            self.assertEqual((event['workspace'], event['conversation']), ('maple', SESSION))
            self.assertTrue(event['external_id'].startswith('push:'))
        ceilings = [(e.get('origin_label') or {}).get('ceiling') for e in events]
        # Before the ~/Documents read nothing is restricted; from it on, everything is private.
        self.assertEqual(ceilings, [None, None, None, None, 'private', 'private', 'private'])
        self.assertEqual(events[4]['origin_label']['reason'], 'personal_content')
        self.assertTrue(events[5]['origin_label']['inherited'])
        self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), before)
        # Live taint state is untouched: the replay used its own.
        self.assertFalse((self.profile / 'origin-sessions.json').exists())

    def test_a_chat_that_ran_read_only_is_refused(self):
        from agentclient import session_label
        from agentclient.push_session import PushRefused
        self.write_claude(self.session_rows())
        session_label._remember_read_only(self.profile, [session_label._key(SESSION)])
        with self.assertRaisesRegex(PushRefused, 'session_ran_read_only'):
            self.push(apply=True)
        self.assertEqual(self.queued(), [])

    def test_rerun_is_idempotent(self):
        self.write_claude(self.session_rows())
        first = self.push(apply=True)
        second = self.push(apply=True)
        self.assertEqual(second['counts']['already_pushed'], first['counts']['events'])
        self.assertEqual(second['sent'], {'queued': 0, 'acknowledged': 0, 'remaining': 0})
        self.assertEqual(len(self.backend.delivered()), 7)
        ids = [e['external_id'] for e in self.backend.delivered()]
        self.assertEqual(len(set(ids)), 7)
        from agentclient.push_session import source_id
        self.assertEqual(ids[0], source_id('claude-code', SESSION, 'main:0'))

    def test_reading_another_workspace_carries_it_including_from_a_subagent(self):
        r = self.repo
        rows = self.session_rows()[:4]
        sub = [tool(r, '2026-05-01T10:00:01.500000Z', 's1', 'Read', {'file_path': str(self.birch / 'notes.md')}),
               result(r, '2026-05-01T10:00:01.600000Z', 's1', 'birch notes'),
               reply(r, '2026-05-01T10:00:01.700000Z', 'The subagent summary is not a reply of the session.')]
        self.write_claude(rows, subagent=sub)
        report = self.push(apply=True)
        self.assertEqual(report['carried_workspaces'], ['birch'])
        events = self.backend.delivered()
        self.assertEqual([e['event']['kind'] for e in events], ['UserPromptSubmit', 'PostToolUse', 'PostToolUse', 'Stop'])
        self.assertEqual(events[2]['origin_label']['projects'], ['birch'])  # the subagent read
        self.assertEqual(events[3]['origin_label']['projects'], ['birch'])  # and everything after it
        self.assertNotIn('subagent summary', json.dumps(events))

    def test_out_of_scope_and_foreign_transcripts_are_refused(self):
        from agentclient.push_session import PushRefused
        loose = self.write_claude([prompt(self.loose, '2026-05-01T10:00:00.000001Z', 'hello')], cwd=self.loose,
                                  session='5b1d2c3b-0000-4000-8000-00000000e2b0')
        with self.assertRaisesRegex(PushRefused, 'scoped_folder'):
            self.push('5b1d2c3b-0000-4000-8000-00000000e2b0')
        elsewhere = Path(self.tmp.name) / 'copy.jsonl';elsewhere.write_bytes(loose.read_bytes())
        with self.assertRaisesRegex(PushRefused, 'not_your_transcript'):
            self.push(str(elsewhere))
        self.write_claude(self.session_rows(), subagent=[prompt(self.repo, '2026-05-01T10:00:00.5Z', 'sub')])
        with self.assertRaisesRegex(PushRefused, 'subagent'):
            self.push(str(next((self.claude / 'projects').rglob('agent-a1.jsonl'))))
        with patch('agentclient.push_session._role', return_value='reader'):
            with self.assertRaisesRegex(PushRefused, 'membership_required'):
                self.push(apply=True)
        self.assertEqual(self.backend.delivered(), [])

    def test_codex_rollout_is_replayed_private(self):
        folder = self.codex / 'sessions/2026/05/01';folder.mkdir(parents=True)
        rows = [{'type': 'session_meta', 'timestamp': '2026-05-01T10:00:00Z',
                 'payload': {'id': CODEX_SESSION, 'cwd': str(self.repo)}},
                {'type': 'event_msg', 'timestamp': '2026-05-01T10:00:01Z', 'payload': {
                    'type': 'item_completed', 'thread_id': CODEX_SESSION, 'turn_id': 'turn-1',
                    'item': {'id': 'i1', 'type': 'UserMessage', 'content': [{'type': 'input_text', 'text': 'List files'}]}}},
                {'type': 'event_msg', 'timestamp': '2026-05-01T10:00:02Z', 'payload': {
                    'type': 'item_completed', 'thread_id': CODEX_SESSION, 'turn_id': 'turn-1',
                    'item': {'id': 'i2', 'type': 'CommandExecution', 'cwd': str(self.repo),
                             'command': 'cat ' + str(self.homedir / 'Documents/x.txt'), 'aggregated_output': 'personal'}}},
                {'type': 'event_msg', 'timestamp': '2026-05-01T10:00:03Z', 'payload': {
                    'type': 'task_complete', 'turn_id': 'turn-1', 'last_agent_message': 'Done.'}}]
        (folder / ('rollout-2026-05-01T10-00-00-' + CODEX_SESSION + '.jsonl')).write_text(
            ''.join(json.dumps(r) + '\n' for r in rows))
        self.push(CODEX_SESSION, apply=True)
        events = self.backend.delivered()
        self.assertEqual([e['event']['kind'] for e in events], ['UserPromptSubmit', 'PostToolUse', 'Stop'])
        self.assertEqual([(e.get('origin_label') or {}).get('ceiling') for e in events], [None, 'private', 'private'])
        self.assertFalse(any('sharing' in e for e in events))

    def test_list_recent_names_sessions_without_content(self):
        self.write_claude(self.session_rows())
        from agentclient.push_session import recent
        listed = recent(self.config)['sessions']
        self.assertEqual([(s['host'], s['session'], s['workspace'], s['pushable']) for s in listed],
                         [('claude-code', SESSION, 'maple', True)])
        self.assertNotIn('entry point', json.dumps(listed))


class CommandLineTests(PushFixture):
    def run_cli(self, *args):
        from agentclient.cli import vaelius_main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('agentclient.transport.enterprise_client', return_value=self.backend):
            vaelius_main(['--home', str(self.profile), *args])
        return json.loads(out.getvalue())

    def test_push_session_command_and_the_removed_bulk_backfill(self):
        self.write_claude(self.session_rows())
        self.assertTrue(self.run_cli('push-session', SESSION)['dry_run'])
        self.assertEqual(self.run_cli('push-session', '--list-recent')['sessions'][0]['session'], SESSION)
        with self.assertRaises(SystemExit) as refused:
            self.run_cli('push-session', 'no-such-session-id')
        self.assertIn('session_not_found', str(refused.exception))
        # The client-side bulk Codex backfill and its `python -m` entry point are gone.
        self.assertIsNone(importlib.util.find_spec('agentclient.enterprise_backfill'))
        gone = subprocess.run([sys.executable, '-m', 'agentclient.enterprise_backfill', 'plan', '--home', str(self.profile)],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn('No module named', gone.stderr)


if __name__ == '__main__':
    unittest.main()
