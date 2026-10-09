from pathlib import Path
"""Frozen execution failure cases; no API key or development-model dispatch."""
import json
import threading
import unittest
from unittest.mock import patch
from agenthub.cloud_execution import ApiExecution, ExecutionError, DeterministicExecution

class ExecutionTests(unittest.TestCase):
    def test_api_requires_explicit_config(self):
        with self.assertRaises(ExecutionError): ApiExecution(model='gpt-5.6-luna',api_key=None)
    def test_structured_request_and_cache_reporting(self):
        seen=[]
        def send(request,timeout):
            seen.append(request)
            return {'id':'resp-synthetic','status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"result":"ok"}'}]}], 'usage':{'input_tokens':20,'input_tokens_details':{'cached_tokens':12},'output_tokens':3}}
        adapter=ApiExecution(model='operator-test-model',api_key='synthetic-not-live',send=send)
        result,usage,handle=adapter('.',{},'stable instructions',{'new_turn':'synthetic'}, {'type':'object'},session_id='expired',session_dir=None)
        self.assertEqual(result,{'result':'ok'});self.assertEqual(usage['cached_input_tokens'],12)
        self.assertFalse(seen[0]['store']);self.assertNotIn('previous_response_id',seen[0])
        self.assertEqual(seen[0]['text']['format']['type'],'json_schema')
        self.assertEqual(handle,'resp-synthetic')
    def test_unknown_return_and_no_hidden_retry(self):
        calls=[]
        def send(request,timeout):calls.append(1);raise TimeoutError()
        adapter=ApiExecution(model='operator-test-model',api_key='synthetic',send=send)
        with self.assertRaisesRegex(ExecutionError,'dispatch_uncertain'):adapter('.',{},'i',{}, {},session_id=None,session_dir=None)
        self.assertEqual(len(calls),1)
    def test_refusal_and_output_bound(self):
        for response in [{'status':'incomplete'}, {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'no'}]}]}]:
            adapter=ApiExecution(model='operator-test',api_key='synthetic',send=lambda *_:response)
            with self.assertRaises(ExecutionError):adapter('.',{},'i',{}, {},session_id=None,session_dir=None)
    def test_deterministic_requires_fixture(self):
        with self.assertRaisesRegex(ExecutionError,'fixture_required'): DeterministicExecution(None)

class StreamDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((Path(__file__).resolve().parent/'fixtures/service').joinpath('cloud_execution_deadline_v1.json').read_text())
        self.now=0;self.chunks_read=0;self.closed=False
        self.cancel=threading.Event()
        self.adapter=ApiExecution(model='operator-test',api_key='synthetic-secret-canary',
            timeout=self.fixture['timeout_seconds'],cancel=self.cancel)

    def response(self,*,step=1,headers_elapsed=0,cancel_at_end=False):
        owner=self
        class Response:
            status_code=200
            def __enter__(self):
                owner.now+=headers_elapsed
                return self
            def __exit__(self,*_):owner.closed=True
            def iter_content(self,size):
                for chunk in owner.fixture['slow_stream']['chunks']:
                    owner.now+=step;owner.chunks_read+=1
                    yield chunk.encode()
                if cancel_at_end:owner.cancel.set()
        return Response()

    def test_slow_stream_cannot_extend_elapsed_deadline(self):
        case=self.fixture['slow_stream']
        with patch('requests.post',return_value=self.response()) as post,patch('time.monotonic',side_effect=lambda:self.now):
            with self.assertRaisesRegex(ExecutionError,case['expected_error']) as raised:
                self.adapter._send({},self.fixture['timeout_seconds'])
        self.assertEqual(self.now,case['expected_elapsed_seconds'])
        self.assertEqual(self.chunks_read,case['expected_chunks_read'])
        self.assertEqual(post.call_count,1);self.assertTrue(self.closed)
        self.assertNotIn('synthetic-secret-canary',str(raised.exception))

    def test_late_headers_do_not_start_response_processing(self):
        case=self.fixture['headers_past_deadline']
        with patch('requests.post',return_value=self.response(headers_elapsed=case['elapsed_seconds'])) as post,patch('time.monotonic',side_effect=lambda:self.now):
            with self.assertRaisesRegex(ExecutionError,case['expected_error']):
                self.adapter._send({},self.fixture['timeout_seconds'])
        self.assertEqual(self.chunks_read,case['expected_chunks_read'])
        self.assertEqual(post.call_count,1);self.assertTrue(self.closed)

    def test_cancellation_rechecked_at_transport_dispatch(self):
        case=self.fixture['cancel_before_dispatch'];self.cancel.set()
        with patch('requests.post',return_value=self.response(step=0.25)) as post,patch('time.monotonic',side_effect=lambda:self.now):
            with self.assertRaisesRegex(ExecutionError,case['expected_error']):
                self.adapter._send({},self.fixture['timeout_seconds'])
        self.assertEqual(post.call_count,case['expected_http_attempts'])

    def test_cancel_at_stream_end_cannot_admit_response(self):
        case=self.fixture['cancel_at_stream_end']
        with patch('requests.post',return_value=self.response(step=0.25,cancel_at_end=True)) as post,patch('time.monotonic',side_effect=lambda:self.now):
            with self.assertRaisesRegex(ExecutionError,case['expected_error']):
                self.adapter._send({},self.fixture['timeout_seconds'])
        self.assertEqual(post.call_count,case['expected_http_attempts']);self.assertTrue(self.closed)

    def test_timely_stream_remains_valid_without_retry(self):
        case=self.fixture['normal_response']
        with patch('requests.post',return_value=self.response(step=case['chunk_elapsed_seconds'])) as post,patch('time.monotonic',side_effect=lambda:self.now):
            response=self.adapter._send({},self.fixture['timeout_seconds'])
        self.assertEqual(response,{'status':'completed','output':[]})
        self.assertEqual(self.now,case['expected_elapsed_seconds'])
        self.assertEqual(post.call_count,case['expected_http_attempts']);self.assertTrue(self.closed)
