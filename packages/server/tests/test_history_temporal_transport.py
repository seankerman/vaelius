"""Source-level v3 temporal detail transport contract; no provider calls."""
import unittest
from contextlib import nullcontext

from starlette.testclient import TestClient

from agenthub.cloud_api import create_app
from agenthub.enterprise import Denied


class Store:
    tenant_id = 'synthetic'
    calls = []

    def require_ready(self): pass
    def delivery_lock(self): return nullcontext()
    def authenticate(self, token, request_id=None):
        if token != 'synthetic-token': raise Denied()
        return {'tenant': 'synthetic', 'actor': 'alice', 'request_id': request_id}
    def detail(self, ctx, ident, *, as_of=None, time_mode=None):
        self.calls.append((ident, as_of, time_mode))
        if ident != 'ta_early': raise Denied()
        return {'id': ident, 'revision': 'r1', 'claim': {'text': 'Known by cutoff'}}
    def timeline(self, ctx, ident, *, offset=0, limit=3, cursor=None, cursor_mode=False):
        self.calls.append((ident, offset, limit, cursor, cursor_mode))
        return {'id': ident, 'episodes': [], 'coverage_gaps': [], 'has_more': False,
                **({'next_cursor': None} if cursor_mode else {})}
    def project_history(self, ctx, project, *, cursor=None, limit=8):
        self.calls.append(('history', project, cursor, limit))
        return {'project': project, 'summary_revision': 'synthetic-r1',
                'phase_anchors': [], 'episodes': [], 'coverage_gaps': [],
                'has_more': False, 'next_cursor': None}
    def project_episode(self, ctx, project, episode_id, *, offset=0, limit=8, text_offset=0):
        self.calls.append(('episode', project, episode_id, offset, limit, text_offset))
        return {'episode_id': episode_id, 'assertions': [], 'coverage_gaps': []}


class Registry:
    def __init__(self):
        self.store = Store()
        self.store.calls = []
    def store_for_token(self, token): return self.store


class TransportTests(unittest.TestCase):
    def test_versioned_detail_preserves_explicit_cutoff_and_rejects_extra_fields(self):
        registry = Registry()
        client = TestClient(create_app(registry, allowed_hosts=['testserver']))
        headers = {'Authorization': 'Bearer synthetic-token'}
        result = client.post('/enterprise/v3/temporal-detail', headers=headers,
            json={'id': 'ta_early', 'as_of': '2026-09-10', 'time_mode': 'known_at'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(registry.store.calls, [('ta_early', '2026-09-10', 'known_at')])
        self.assertEqual(client.post('/enterprise/v3/temporal-detail', headers=headers,
            json={'id': 'ta_early', 'source': 'forged'}).status_code, 400)

    def test_timeline_cursor_mode_and_legacy_offset_are_distinct(self):
        registry = Registry()
        client = TestClient(create_app(registry, allowed_hosts=['testserver']))
        headers = {'Authorization': 'Bearer synthetic-token'}
        first = client.post('/enterprise/v3/timeline', headers=headers,
            json={'id': 'doc', 'limit': 2})
        self.assertEqual(first.status_code, 200)
        self.assertIn('next_cursor', first.json())
        legacy = client.post('/enterprise/v3/timeline', headers=headers,
            json={'id': 'doc', 'offset': 1, 'limit': 2})
        self.assertEqual(legacy.status_code, 200)
        self.assertNotIn('next_cursor', legacy.json())
        self.assertEqual(registry.store.calls, [('doc', 0, 2, None, True),
                                                ('doc', 1, 2, None, False)])
        self.assertEqual(client.post('/enterprise/v3/timeline', headers=headers,
            json={'id': 'doc', 'offset': 0, 'cursor': 'tc1_'+'0'*64}).status_code, 400)

    def test_project_history_and_episode_use_authenticated_project_request(self):
        registry = Registry()
        client = TestClient(create_app(registry, allowed_hosts=['testserver']))
        headers = {'Authorization': 'Bearer synthetic-token'}
        first = client.post('/enterprise/v3/project-history', headers=headers,
            json={'project': 'maple', 'limit': 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['project'], 'maple')
        detail = client.post('/enterprise/v3/project-episode', headers=headers,
            json={'project': 'maple', 'episode_id': 'occ_1', 'offset': 0, 'limit': 3})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(registry.store.calls, [
            ('history', 'maple', None, 2), ('episode', 'maple', 'occ_1', 0, 3, 0)])


if __name__ == '__main__': unittest.main()
