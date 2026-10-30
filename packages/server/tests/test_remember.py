"""Remembered items: the person's own explicit memory over owner-reviewed notes."""
from types import SimpleNamespace
import time
import unittest

from agentclient.enterprise_contract import VERSION
from agenthub.enterprise import Denied
from test_cloud_postgres import PostgresFixture, SERVICES

LONG = 'Deploy notes for the maple service.\n\n' + 'The staging bucket rotates keys weekly. ' * 120


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schemas required')
class RememberTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        # Only the person's own write action: no curate, correct or withdraw.
        self.writer = self.store.authenticate(self.store.enroll('acme', 'alice', 'alice-ingest', ['ingest', 'read']))
        self.bob = self.store.authenticate(self.tokens['bob'])

    def tearDown(self):
        self.postgres_teardown()

    def add(self, text='Use tabs in Makefiles, spaces elsewhere.', ctx=None, **extra):
        value = dict({'text': text, 'workspace': 'maple', 'request_key': 'k-' + str(time.time_ns()),
                      'confirm': True}, **extra)
        return self.store.remember(ctx or self.writer, value)

    def labelled(self, ctx, **label):
        return self.store.session_filter(ctx, dict({'workspace': 'maple'}, **label))

    def team(self):
        for ctx in (self.writer, self.bob):
            self.store.sharing_setting(ctx, {'max_sharing': 'organization'})

    def test_ingest_alone_writes_an_owner_private_item(self):
        result = self.add(LONG)
        self.assertEqual(result['disposition'], 'accepted')
        detail = self.store.detail(self.writer, result['document_id'])
        self.assertEqual(detail['claim']['title'], 'Deploy notes for the maple service.')
        self.assertIn('[excerpt; fetch_memory reads the rest]', detail['claim']['lesson'])
        with self.store.open() as state:
            body = state.db.execute('SELECT body FROM memories WHERE id=%s', (result['source_id'],)).fetchone()[0]
        self.assertEqual(body, LONG)  # the source keeps every word
        with self.assertRaises(Denied):
            self.store.detail(self.bob, result['document_id'])
        listed = self.store.remembered(self.writer, {})['items']
        self.assertEqual([item['document_id'] for item in listed], [result['document_id']])
        self.assertEqual(listed[0]['sharing'], 'private')
        self.assertEqual(self.store.remembered(self.bob, {})['items'], [])

    def test_needs_ingest_and_the_writer_role(self):
        reader = self.store.authenticate(self.store.enroll('acme', 'alice', 'alice-read', ['read']))
        with self.assertRaises(Denied):
            self.add(ctx=reader)
        with self.assertRaises(Denied):
            self.add(workspace='nowhere')
        with self.assertRaises(ValueError):
            self.add('x' * 8193)

    def test_preview_changes_nothing_and_only_its_token_applies(self):
        value = {'text': 'Prefer pytest.', 'workspace': 'maple', 'request_key': 'preview-1', 'always_load': True}
        preview = self.store.remember(self.writer, dict(value, confirm=False))
        self.assertTrue(preview['dry_run'])
        self.assertEqual(self.store.remembered(self.writer, {})['items'], [])
        stale = self.store.remember(self.writer, dict(value, text='Prefer unittest.', confirm=preview['confirm_token']))
        self.assertEqual(stale['status'], 'selection_changed')
        self.assertEqual(self.store.remembered(self.writer, {})['items'], [])
        applied = self.store.remember(self.writer, dict(value, confirm=preview['confirm_token']))
        self.assertEqual(applied['disposition'], 'accepted')
        again = self.store.remember(self.writer, dict(value, confirm=preview['confirm_token']))
        self.assertEqual((again['disposition'], again['document_id']), ('duplicate', applied['document_id']))

    def test_replace_versions_the_same_item_and_forget_withdraws_it(self):
        first = self.add('Release on Tuesdays.')
        second = self.store.remember(self.writer, {'text': 'Release on Thursdays.', 'replace': first['document_id'],
                                                   'request_key': 'replace-1', 'confirm': True})
        self.assertEqual(second['document_id'], first['document_id'])
        self.assertEqual(self.store.detail(self.writer, first['document_id'])['claim']['lesson'], 'Release on Thursdays.')
        with self.store.open() as state:
            old = state.db.execute('SELECT active FROM enterprise_sources WHERE id=%s', (first['source_id'],)).fetchone()[0]
            revisions = state.db.execute('SELECT count(*) FROM knowledge_revisions WHERE document_id=%s',
                                         (first['document_id'],)).fetchone()[0]
        self.assertEqual((old, revisions), (0, 2))
        # Another person's item cannot be previewed, replaced or removed.
        for value in ({'document_id': first['document_id']}, {'document_id': first['document_id'], 'confirm': True}):
            with self.assertRaises(Denied):
                self.store.forget_remembered(self.bob, value)
        with self.assertRaises(Denied):
            self.store.remember(self.bob, {'text': 'Taken over.', 'replace': first['document_id'],
                                           'request_key': 'bob-1', 'confirm': False})
        preview = self.store.forget_remembered(self.writer, {'document_id': first['document_id']})
        self.assertTrue(preview['dry_run'])
        self.store.forget_remembered(self.writer, {'document_id': first['document_id'], 'confirm': preview['confirm_token']})
        with self.assertRaises(Denied):
            self.store.detail(self.writer, first['document_id'])
        self.assertEqual(self.store.remembered(self.writer, {})['items'], [])
        with self.store.open() as state:
            audit = [r[0] for r in state.db.execute("SELECT disposition FROM enterprise_audit WHERE operation='remember' ORDER BY id")]
        self.assertEqual(audit, ['accepted', 'corrected', 'withdrawn'])

    def test_always_load_follows_the_session_audience(self):
        self.team()
        item = self.add('Answer in British English.', always_load=True)
        self.add('Not loaded automatically.')
        private = self.labelled(self.writer, private=True)
        cards = self.store.preferences(private, project='maple')['remembered']
        self.assertEqual([card['id'] for card in cards], [item['document_id']])
        self.assertEqual(cards[0]['origin']['sharing'], 'private')
        # A shared session's audience is wider than the owner: the private item stays out.
        self.assertEqual(self.store.preferences(self.labelled(self.writer), project='maple')['remembered'], [])
        self.assertEqual(self.store.preferences(self.labelled(self.bob, private=True), project='maple')['remembered'], [])

    def test_legacy_preferences_reach_only_private_sessions(self):
        self.team()
        text = 'Please remember: I prefer pytest for my projects.'
        source = self.store.ingest(self.ctx, {'version': VERSION, 'external_id': 'pref', 'session': 's', 'turn': '1',
                                              'project': 'maple', 'kind': 'UserPromptSubmit', 'body': text,
                                              'occurred_at': 1, 'visibility': 'private'})['source_id']
        self.store.curate_preference(self.ctx, source, {'key': 'test_runner', 'value': 'pytest', 'scope': 'user',
                                                        'quote': text})
        ctx = self.store.authenticate(self.tokens['alice'])
        self.assertEqual(self.store.preferences(ctx, project='maple')['values'], {'test_runner': 'pytest'})
        self.assertEqual(self.store.preferences(self.labelled(ctx, private=True), project='maple')['values'],
                         {'test_runner': 'pytest'})
        self.assertEqual(self.store.preferences(self.labelled(ctx), project='maple')['values'], {})

    def test_mcp_new_and_old_submit_shapes(self):
        from agenthub.mcp_tools import MemoryTools

        def backend(ctx):
            def request(path, value=None):
                if path == '/enterprise/v3/remember':
                    return self.store.remember(ctx, value)
                if path == '/enterprise/v1/reviewed-notes':
                    return self.store.accept_reviewed_note(ctx, value['source_id'], value['title'], value['lesson'])
                raise AssertionError(path)
            return SimpleNamespace(request=request)
        tools = MemoryTools('maple', backend(self.writer))
        args = {'request_key': 'mcp-1', 'title': 'Review style', 'evidence': 'Keep review comments short and kind.'}
        preview = tools.call('submit_memory_candidate', args)
        self.assertTrue(preview['dry_run'])
        with self.assertRaises(ValueError):  # the MCP tools never take confirm=true
            tools.call('remember', {'text': 'x', 'request_key': 'mcp-2', 'confirm': True})
        applied = tools.call('submit_memory_candidate', dict(args, confirm=preview['confirm_token']))
        self.assertEqual(applied['disposition'], 'accepted')
        # The old shape still needs curate and one source of the caller's.
        source = self.store.ingest(self.ctx, {'version': VERSION, 'external_id': 'old', 'session': 's', 'turn': '1',
                                              'project': 'maple', 'kind': 'Stop', 'body': 'Synthetic old-shape body.',
                                              'occurred_at': 1, 'visibility': 'private'})['source_id']
        old = MemoryTools('maple', backend(self.ctx)).call('submit_memory_candidate', dict(args, source_ids=[source]))
        self.assertEqual(old['status'], 'owner_reviewed_private')
        with self.assertRaises(Denied):
            tools.call('submit_memory_candidate', dict(args, request_key='mcp-3', source_ids=[source]))


if __name__ == '__main__':
    unittest.main()
