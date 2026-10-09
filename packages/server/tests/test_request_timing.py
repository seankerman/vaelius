"""Source-free per-request timing, including failures and concurrent requests."""
import asyncio
import json
import unittest
from starlette.testclient import TestClient
from agenthub.cloud_api import create_app
from test_cloud_api import Registry, Store


class RequestTimingTests(unittest.TestCase):
    def test_search_timings_do_not_log_content_or_client_identity(self):
        records=[]
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver'],timing_sink=records.append))
        response=client.post('/enterprise/v3/search',json={'query':'PRIVATE_QUERY'},
            headers={'Authorization':'Bearer synthetic','X-Request-ID':'PRIVATE_ID'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(records),1)
        row=records[0]
        self.assertEqual(row['status'],200)
        self.assertEqual(response.headers['X-AgentHub-Request-ID'],row['request_id'])
        self.assertEqual(set(row),{'event','request_id','status','elapsed_ms','stages_ms'})
        self.assertTrue({'authenticate','ready','meter'}<=set(row['stages_ms']))
        self.assertGreaterEqual(row['elapsed_ms'],max(row['stages_ms'].values()))
        for canary in ('PRIVATE_QUERY','PRIVATE_ID','synthetic','alice','acme'):
            self.assertNotIn(canary,json.dumps(row))
        client.get('/health')
        self.assertEqual(len(records),1)

    def test_failure_and_broken_sink_preserve_response(self):
        records=[]
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver'],timing_sink=records.append))
        response=client.post('/enterprise/v3/search',json={'query':'PRIVATE_QUERY'})
        self.assertEqual(response.status_code,404)
        self.assertEqual(records[0]['status'],404)
        self.assertNotIn('PRIVATE_QUERY',json.dumps(records))
        self.assertNotIn('X-AgentHub-Request-ID',response.headers)
        def broken(_):raise RuntimeError('PRIVATE_SINK_ERROR')
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver'],timing_sink=broken))
        self.assertEqual(client.post('/enterprise/v3/search',json={},
            headers={'Authorization':'Bearer synthetic'}).status_code,200)

    def test_context_isolation_nested_and_failed_spans(self):
        from agenthub.request_timing import capture,span
        async def one(name):
            with capture() as trace:
                with span('search'):
                    await asyncio.sleep(0)
                    with span(name):await asyncio.sleep(0)
                try:
                    with span('delivery_check'):raise ValueError('PRIVATE_ERROR')
                except ValueError:pass
                return trace.record(200)
        async def both():return await asyncio.gather(one('embedding'),one('lexical_sql'))
        a,b=asyncio.run(both())
        self.assertNotEqual(a['request_id'],b['request_id'])
        self.assertNotIn('lexical_sql',a['stages_ms'])
        self.assertNotIn('embedding',b['stages_ms'])
        self.assertIn('delivery_check',a['stages_ms'])
        self.assertGreaterEqual(a['stages_ms']['search'],a['stages_ms']['embedding'])
        with span('search'):pass # direct callers need no request context
        with self.assertRaises(ValueError):
            with span('PRIVATE_DYNAMIC_LABEL'):pass
