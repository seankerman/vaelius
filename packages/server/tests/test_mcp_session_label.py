"""The MCP connection's session label from the client's folder record, through recall.

The header helper sends the folder's label narrowed by the label the hooks recorded
for the session last active there (agentclient.session_label.compose). A private
session's MCP recall then reaches other workspaces the person can read; a shared
session's does not."""
import time
from types import SimpleNamespace
import unittest

from agentclient.session_label import compose
from agenthub.mcp_tools import MemoryTools
from agenthub.workspace_access import validate_session_label
from test_cloud_postgres import SERVICES
from test_workspace_access import ParityItems


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class MCPSessionLabelTests(ParityItems, unittest.TestCase):
    def setUp(self):
        self.parity_setup()

    def tearDown(self):
        self.postgres_teardown()

    def recall(self, recorded):
        """(sources the MCP search returned, sources the label admits) for alice in maple."""
        label = compose({'workspace': 'maple'}, recorded)
        sent = []

        def request(path, value=None):
            value = dict(value)
            ctx = self.store.session_filter(self.alice, validate_session_label(value.pop('session_label')))
            sent.append(ctx)
            return self.store.search(ctx, value)
        result = MemoryTools('maple', SimpleNamespace(request=request), label=label).call(
            'search_memory', {'query': self.fixture['query']})
        documents = self.documents()
        returned = {documents[card['id']] for card in result['records'] if card['id'] in documents}
        return returned, self.layers(sent[-1])[0]

    def test_private_session_reaches_other_workspaces_and_a_shared_one_does_not(self):
        private_returned, private_allowed = self.recall({'workspace': 'maple', 'private': True, 'updated': time.time()})
        team_returned, team_allowed = self.recall(None)
        other = self.expect('o_shared', 'o_maple', 'm_birch')
        self.assertTrue(private_allowed & other, 'a private session may read other workspaces')
        self.assertFalse(team_allowed & other, 'a shared maple session may not')
        self.assertTrue(private_returned <= private_allowed and team_returned <= team_allowed)
        self.assertTrue(private_returned & other, 'and the MCP search returns them')
        self.assertFalse(team_returned & other)

    def test_a_stale_record_or_one_for_another_workspace_keeps_the_folder_label(self):
        team = self.recall(None)[1]
        stale = {'workspace': 'maple', 'private': True, 'updated': time.time() - 13 * 3600}
        self.assertEqual(self.recall(stale)[1], team)
        self.assertEqual(self.recall({'workspace': 'oak', 'private': True, 'updated': time.time()})[1], team)


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class ReadListTests(ParityItems, unittest.TestCase):
    """`.vaelius` `read`: the default recall scope, never wider than the label allows."""

    def setUp(self):
        self.parity_setup()

    def tearDown(self):
        self.postgres_teardown()

    def returned(self, label, **arguments):
        def request(path, value=None):
            value = dict(value)
            # As the API does: the label travels as the request's session label.
            ctx = dict(self.alice, session_label_request=value.pop('session_label'), session_label_cache={})
            return self.store.search(ctx, value)
        result = MemoryTools('maple', SimpleNamespace(request=request), label=label).call(
            'search_memory', dict({'query': self.fixture['query']}, **arguments))
        documents = self.documents()
        return {documents[card['id']] for card in result['records'] if card['id'] in documents}

    def test_read_narrows_recall_cannot_widen_it_and_scope_all_broadens_within_permissions(self):
        private = {'workspace': 'maple', 'private': True}
        oak = self.expect('o_shared', 'o_maple')
        everything = self.returned(dict(private, read=['*']))
        self.assertTrue(everything & oak, 'with "*" a private session recalls other workspaces')
        default = self.returned(dict(private, read=['maple']))
        self.assertTrue(default, 'the current workspace is recalled')
        self.assertFalse(default & oak, 'the default read list is the current workspace only')
        self.assertTrue(self.returned(dict(private, read=['maple', 'oak'])) & oak, 'listed workspaces are recalled')
        self.assertTrue(self.returned(dict(private, read=['maple']), scope='all') & oak,
                        'scope "all" broadens on request')
        self.assertTrue(self.returned(dict(private, read=['maple']), scope=['oak']) & oak)
        shared = {'workspace': 'maple'}
        self.assertFalse(self.returned(dict(shared, read=['*', 'oak'])) & oak, 'never wider than the label')
        self.assertFalse(self.returned(dict(shared, read=['maple']), scope='all') & oak)


if __name__ == '__main__':
    unittest.main()
