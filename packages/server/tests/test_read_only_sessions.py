"""Read-only chats are enforced by the service (agenthub.read_only).

A stub store behind the real API and MCP endpoint: every write route and write tool
is refused, with one clear message, for a connection that says it is read-only (the
`X-Vaelius-Read-Only` header or a `read_only` session label) or that names a session
registered as read-only; reads keep working. No database: the registration table and
the ingest-side check are covered against PostgreSQL in test_workspace_access."""
from contextlib import nullcontext
import json
import unittest

from starlette.testclient import TestClient

from agenthub.cloud_api import ROUTE_ACTIONS, create_app, route_rule
from agenthub.enterprise import Denied
from agenthub.read_only import MESSAGE, ReadOnlySession, is_write

FULL = ['ingest', 'read', 'source_read', 'feedback', 'correct', 'curate', 'withdraw', 'share', 'manage', 'policy']
MCP = {'Authorization': 'Bearer synthetic', 'Accept': 'application/json, text/event-stream',
       'MCP-Protocol-Version': '2025-11-25', 'X-AgentNetwork-Project': 'maple'}
CONFIRM = '0' * 32


class Store:
    tenant_id = 'acme'
    serving_reranker = dsn = decorate_delivery = ingest_limits = None

    def __init__(self):
        self.writes = []
        self.reads = 0
        self.registered = set()

    def require_ready(self):
        pass

    def authenticate(self, token, request_id=None):
        if token != 'synthetic':
            raise Denied()
        return {'tenant': 'acme', 'actor': 'alice', 'principal': 'alice', 'actions': list(FULL),
                'request_id': request_id}

    def delivery_read_lock(self):
        return nullcontext()

    delivery_lock = delivery_read_lock

    def search(self, ctx, value):
        self.reads += 1
        return {'results': [], 'answerable': False}

    def validate_search_delivery(self, ctx, value, result):
        return result

    def mark_read_only(self, ctx, value):
        self.registered |= set(value['sessions'])
        return {'read_only': True, 'sessions': len(value['sessions']), 'message': MESSAGE}

    def refuse_read_only(self, ctx, sessions, db=None):
        if set(sessions) & self.registered:
            raise ReadOnlySession()

    def __getattr__(self, name):
        # Every business method is a write the test must never reach.
        def write(*args, **kwargs):
            self.writes.append(name)
            return {'status': 'recorded', 'id': 'x', 'classification': 'agent_self_report',
                    'independently_verified': False}
        return write


class ReadOnlyEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.store = store = Store()
        registry = type('Registry', (), {'store_for_token': lambda _, token: store})()
        self.client = self.enterContext(TestClient(create_app(registry, allowed_hosts=['testserver'])))

    def tool(self, name, arguments, headers):
        response = self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                  'params': {'name': name, 'arguments': arguments}},
                                    headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['result']

    def test_every_write_tool_is_refused_on_a_read_only_connection_and_reads_work(self):
        writes = {
            'remember': {'text': 'keep this', 'request_key': 'k1', 'confirm': False},
            'report_memory_outcome': {'id': 'd', 'revision': 'r', 'request_key': 'k2', 'outcome': 'helpful'},
            'withdraw_memory': {'id': 's', 'expected_revision': '1', 'request_key': 'k3'},
            'forget_memories': {'session': 'current', 'confirm': CONFIRM},
            'forget_remembered': {'id': 'm', 'confirm': CONFIRM},
            'submit_memory_candidate': {'request_key': 'k4', 'title': 'Note', 'evidence': 'x' * 30,
                                        'source_ids': ['s']},
        }
        for headers in ({**MCP, 'X-Vaelius-Read-Only': '1'},
                        {**MCP, 'X-Vaelius-Session-Label': json.dumps({'workspace': 'maple', 'read_only': True})}):
            for name, arguments in writes.items():
                with self.subTest(name=name, header=sorted(set(headers) - set(MCP))):
                    result = self.tool(name, arguments, headers)
                    self.assertTrue(result['isError'])
                    self.assertEqual(result['content'][0]['text'], MESSAGE)
            self.assertEqual(self.store.writes, [], 'no write reached the store')
        read_only = {**MCP, 'X-Vaelius-Read-Only': '1'}
        self.assertFalse(self.tool('search_memory', {'query': 'anything'}, read_only).get('isError'), 'reads work')
        self.assertEqual(self.store.reads, 1)
        # A connection without it writes as before.
        self.assertFalse(self.tool('remember', writes['remember'], MCP).get('isError'))
        self.assertEqual(self.store.writes, ['remember'])

    def test_http_write_routes_answer_403_with_the_message(self):
        headers = {'Authorization': 'Bearer synthetic', 'X-Vaelius-Read-Only': '1'}
        for path, body in (('/enterprise/v3/remember', {'text': 'x'}), ('/enterprise/v1/lifecycle',
                                                                           {'operation': 'withdraw'}),
                           ('/enterprise/v3/my-data/forget', {}), ('/enterprise/v2/parts', {}),
                           ('/enterprise/v1/boundary', {}), ('/enterprise/v1/receipts', {}),
                           ('/enterprise/v3/workspaces/create', {})):
            with self.subTest(path=path):
                response = self.client.post(path, json=body, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {'error': 'read_only_session', 'message': MESSAGE})
        self.assertEqual(self.store.writes, [])

    def test_a_registered_session_is_refused_by_id(self):
        auth = {'Authorization': 'Bearer synthetic'}
        self.assertEqual(self.client.post('/enterprise/v3/sessions/read-only', json={'sessions': ['chat-1']},
                                          headers=auth).status_code, 200)
        receipt = {'version': 1, 'session': 'chat-1', 'request_id': 't', 'cards': [], 'serialized_chars': 0}
        refused = self.client.post('/enterprise/v1/receipts', json=receipt, headers=auth)
        self.assertEqual((refused.status_code, refused.json()['error']), (403, 'read_only_session'))
        boundary = {'version': 1, 'event': {'hook_event_name': 'PostCompact', 'session_id': 'chat-1'}}
        self.assertEqual(self.client.post('/enterprise/v1/boundary', json=boundary, headers=auth).status_code, 403)
        self.assertEqual(self.store.writes, [])
        other = dict(receipt, session='chat-2')
        self.assertEqual(self.client.post('/enterprise/v1/receipts', json=other, headers=auth).status_code, 200)
        self.assertEqual(self.store.writes, ['receipt'], 'another chat still writes')

    def test_the_mcp_tool_makes_its_own_connection_read_only(self):
        headers = {**MCP, 'X-AgentNetwork-Session': 'mcp-connection-1'}
        result = self.tool('read_only_session', {}, headers)
        self.assertFalse(result.get('isError'), result)
        self.assertIs(json.loads(result['content'][0]['text'])['vaelius_session_read_only'], True)
        self.assertEqual(self.store.registered, {'mcp-connection-1'})
        refused = self.tool('remember', {'text': 'x', 'request_key': 'k', 'confirm': False}, headers)
        self.assertEqual(refused['content'][0]['text'], MESSAGE)
        self.assertEqual(self.store.writes, [])

    def test_every_route_is_classified_and_reads_are_never_refused(self):
        writes = {path for (method, path), rule in ROUTE_ACTIONS.items() if is_write(rule, path)}
        for path in ('/enterprise/v3/search', '/enterprise/v3/document-evidence', '/enterprise/v3/workspaces/descriptor',
                     '/enterprise/v3/sessions/read-only', '/enterprise/v3/remember/list', '/enterprise/v3/my-data'):
            self.assertNotIn(path, writes)
        for path in ('/enterprise/v3/remember', '/enterprise/v3/remember/forget', '/enterprise/v3/feedback',
                     '/enterprise/v3/my-data/forget', '/enterprise/v2/parts', '/enterprise/v1/sources',
                     '/enterprise/v1/boundary', '/enterprise/v1/receipts', '/enterprise/v1/reviewed-notes',
                     '/enterprise/v3/workspaces/visibility'):
            self.assertIn(path, writes)
        for operation in ('correct', 'withdraw', 'delete'):
            self.assertTrue(is_write(route_rule('POST', '/enterprise/v1/lifecycle', {'operation': operation}),
                                     '/enterprise/v1/lifecycle'))


if __name__ == '__main__':
    unittest.main()
