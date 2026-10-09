"""Actual blocked transport regressions; synthetic loopback only, no model use."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
import unittest

from agenthub.cloud_execution import ApiExecution, ExecutionError


class WalltimeTests(unittest.TestCase):
    def setUp(self):
        fixture_path = os.environ.get('STAGING_WALLTIME_FIXTURE')
        source = Path(fixture_path) if fixture_path else Path(__file__).resolve().parent.joinpath(
            'fixtures/service/staging_execution_walltime_v1.json')
        self.fixture = json.loads(source.read_text())
        self.attempts = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.attempts += 1
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path == '/redirect':
                    self.send_response(307)
                    self.send_header('Location', '/normal')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                raw = json.dumps({'status': 'completed', 'id': 'synthetic-return',
                    'output': [{'type': 'message', 'content': [
                        {'type': 'output_text', 'text': '{"result":"ok"}'}]}]}).encode()
                try:
                    if self.path == '/headers':
                        time.sleep(owner.fixture['blocked_headers_seconds'])
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    if self.path == '/trickle':
                        parts = owner.fixture['trickle_chunks']
                        for index in range(parts):
                            time.sleep(owner.fixture['trickle_interval_seconds'])
                            self.wfile.write(raw[len(raw)*index//parts:len(raw)*(index+1)//parts])
                            self.wfile.flush()
                    else:
                        self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def adapter(self, path, timeout=1, cancel=None):
        adapter = ApiExecution(model='synthetic', api_key='not-a-credential',
            timeout=timeout, cancel=cancel)
        # Tests alone replace the already validated endpoint with their owned server.
        adapter.endpoint = f'http://127.0.0.1:{self.server.server_port}/{path}'
        return adapter

    def invoke(self, adapter, config=None):
        return adapter('.', config or {}, 'synthetic instructions', {}, {'type': 'object'})

    def test_blocked_headers_are_bounded(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ExecutionError, 'dispatch_uncertain'):
            self.invoke(self.adapter('headers'))
        self.assertLess(time.monotonic()-started, self.fixture['maximum_return_seconds'])
        self.assertEqual(self.attempts, 1)

    def test_mid_body_trickle_cannot_extend_walltime(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ExecutionError, 'dispatch_uncertain'):
            self.invoke(self.adapter('trickle'))
        self.assertLess(time.monotonic()-started, self.fixture['maximum_return_seconds'])
        self.assertEqual(self.attempts, 1)

    def test_cancellation_interrupts_blocked_transport(self):
        cancel = threading.Event()
        timer = threading.Timer(self.fixture['cancel_after_seconds'], cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ExecutionError, 'dispatch_uncertain_cancelled'):
                self.invoke(self.adapter('headers', timeout=3, cancel=cancel))
            self.assertLess(time.monotonic()-started, 1.2)
        finally:
            timer.cancel()
        self.assertLessEqual(self.attempts, 1)

    def test_worker_remaining_time_limits_adapter_timeout(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ExecutionError, 'dispatch_uncertain'):
            self.invoke(self.adapter('trickle', timeout=3), {'observer': {'timeout_seconds': 1}})
        self.assertLess(time.monotonic()-started, self.fixture['maximum_return_seconds'])
        self.assertEqual(self.attempts, 1)

    def test_timely_result_has_no_retained_transport_child(self):
        children = {child.pid for child in multiprocessing.active_children()}
        result, usage, handle = self.invoke(self.adapter('normal', timeout=3))
        self.assertEqual(result, {'result': 'ok'})
        self.assertEqual(handle, 'synthetic-return')
        self.assertEqual(usage, {})
        self.assertEqual(self.attempts, 1)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, children)

    def test_redirect_cannot_create_an_unaccounted_second_request(self):
        with self.assertRaisesRegex(ExecutionError, 'provider_rejected'):
            self.invoke(self.adapter('redirect', timeout=3))
        self.assertEqual(self.attempts, 1)


if __name__ == '__main__':
    unittest.main()
