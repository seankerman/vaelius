"""Receiving-agent contract: optional filters must not masquerade as defaults."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

class QueryToolContractTests(unittest.TestCase):

    def test_explicit_search_delivers_a_bounded_slow_loopback_response(self):
        from agentclient.mcp import MemoryTools

        class Handler(BaseHTTPRequestHandler):

            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                time.sleep(2.25)
                value = {'answerable': True, 'records': [{'id': 'doc_synthetic', 'revision': 'rev_synthetic', 'title': 'Synthetic CSV location', 'lesson': 'The CSV is in data/example.csv.', 'evidence_status': 'source_linked_unverified'}]}
                body=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'content':[{'type':'text','text':json.dumps(value)}],'isError':False}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as home:
            credential = Path(home, 'credential')
            credential.write_text('synthetic-token')
            credential.chmod(384)
            Path(home, 'config.json').write_text(json.dumps({'projects': {'/synthetic': 'demo'}, 'knowledge_backend': {'mode': 'enterprise_local', 'api_version': 'cloud-local-1', 'url': f'http://127.0.0.1:{server.server_port}', 'credential_file': str(credential)}}))
            result = MemoryTools(home, 'demo').call('search_memory', {'query': 'Where is the CSV?'})
        self.assertTrue(result['answerable'])
        self.assertEqual(result['records'][0]['id'], 'doc_synthetic')
if __name__ == '__main__':
    unittest.main()
