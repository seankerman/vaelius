"""Host, forwarded headers and request targets cannot steer routing or OAuth URLs.

Requests are sent as raw ASGI scopes because HTTP clients normalize the Host
header and request target before they reach the application.
"""
import asyncio
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from agenthub.cloud_api import create_app
from agenthub.enterprise import Denied
from agenthub.oauth_provider import OAuthRegistry

RESOURCE = 'https://memory.example/mcp'
ISSUER = 'https://identity.example'
CHALLENGE = 'Bearer resource_metadata="https://memory.example/.well-known/oauth-protected-resource/mcp"'
DEFAULT_HOSTS = ['127.0.0.1', 'localhost']
FORGED_HEADERS = (
    {'host': 'evil.example'},
    {'host': 'evil.example:8443'},
    {'host': 'localhost', 'x-forwarded-host': 'evil.example', 'x-forwarded-proto': 'http'},
    {'host': 'localhost', 'forwarded': 'host=evil.example;proto=http', 'x-forwarded-port': '8443'},
    {'host': 'localhost', 'x-original-url': 'https://evil.example/mcp', 'x-forwarded-prefix': '/evil'},
)


def oauth_registry():
    """Production OAuth registry with synthetic providers and no token store."""
    registry = OAuthRegistry.__new__(OAuthRegistry)
    registry.resource = RESOURCE
    registry.legacy_interactive = False
    registry.audiences = []

    def introspect(token, resource):
        registry.audiences.append(resource)
        raise Denied()

    def store_for_token(token):
        raise Denied()

    registry.providers = [SimpleNamespace(issuer=ISSUER, tenant='acme', actions={'read'}, introspect=introspect)]
    registry.store_for_token = store_for_token
    return registry


def call(app, path, headers, *, method='GET', query=b''):
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
             'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'root_path': '',
             'query_string': query, 'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 55486),
             'headers': [(name.encode(), value.encode()) for name, value in headers.items()]}
    received = []

    async def receive():
        if received:
            return {'type': 'http.disconnect'}
        received.append(True)
        return {'type': 'http.request', 'body': b'{}' if method == 'POST' else b'', 'more_body': False}

    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m['type'] == 'http.response.start')
    response_headers = {k.decode().lower(): v.decode() for k, v in start['headers']}
    body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], response_headers, body


class RequestUrlTrustTests(unittest.TestCase):
    def test_host_header_cannot_choose_the_operation(self):
        # GHSA-86qp-5c8j-p5mr: a Host carrying `/` and `#` used to rewrite
        # request.url.path, so the real path /enterprise/v1/status ran /health.
        app = create_app(oauth_registry(), allowed_hosts=DEFAULT_HOSTS)
        for host in ('localhost:1/health#', 'localhost:1/.well-known/oauth-protected-resource#',
                     'evil.example/health#'):
            with self.subTest(host=host):
                status, headers, body = call(app, '/enterprise/v1/status', {'host': host})
                self.assertNotIn(b'Vaelius', body)
                self.assertNotIn(b'authorization_servers', body)
                self.assertEqual(status, 400)
        # Host filtering cannot be switched off: a wildcard is a configuration error.
        with self.assertRaises(ValueError):
            create_app(oauth_registry(), allowed_hosts=['*'])

    def test_decoded_path_metacharacters_do_not_reroute(self):
        # %3F and %23 decode into the ASGI path; they are not query or fragment.
        app = create_app(oauth_registry(), allowed_hosts=DEFAULT_HOSTS)
        for path in ('/health?x', '/health#x'):
            with self.subTest(path=path):
                status, headers, body = call(app, path, {'host': 'localhost'})
                self.assertEqual((status, headers.get('www-authenticate')), (401, CHALLENGE))
                self.assertNotIn(b'Vaelius', body)
        status, _, body = call(app, '/.well-known/oauth-protected-resource#/mcp', {'host': 'localhost'})
        self.assertEqual(status, 404)
        self.assertNotIn(b'authorization_servers', body)
        status, _, _ = call(app, '/health', {'host': 'localhost'}, query=b'x=1')
        self.assertEqual(status, 400)

    def test_request_target_cannot_move_into_the_authority(self):
        # GHSA-jp82-jpqv-5vv3: a target without a leading `/` matches no route,
        # and the www redirect stays on the configured host.
        status, headers, _ = call(create_app(oauth_registry(), allowed_hosts=DEFAULT_HOSTS),
                                  '@evil.example', {'host': 'localhost'})
        self.assertEqual(status, 404)
        self.assertNotIn('location', headers)
        app = create_app(oauth_registry(), allowed_hosts=['www.memory.example'])
        status, headers, _ = call(app, '@evil.example', {'host': 'memory.example'})
        self.assertEqual(status, 307)
        self.assertEqual(urlsplit(headers['location']).hostname, 'www.memory.example')

    def test_oauth_metadata_challenge_and_audience_use_configured_resource(self):
        # Worst case: the forged host is itself an allowed host name. (Host
        # filtering cannot be disabled; a wildcard is refused at startup.)
        registry = oauth_registry()
        app = create_app(registry, allowed_hosts=['localhost', 'evil.example'])
        expected = {'resource': RESOURCE, 'authorization_servers': [ISSUER],
                    'scopes_supported': ['vaelius:read'], 'bearer_methods_supported': ['header']}
        for headers in FORGED_HEADERS:
            with self.subTest(headers=headers):
                for path in ('/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp'):
                    status, _, body = call(app, path, headers)
                    self.assertEqual((status, json.loads(body)), (200, expected))
                bearer = {**headers, 'authorization': 'Bearer synthetic'}
                for method, path in (('GET', '/enterprise/v1/status'), ('POST', '/mcp')):
                    registry.audiences.clear()
                    status, response_headers, _ = call(app, path, bearer, method=method)
                    self.assertEqual((status, response_headers.get('www-authenticate')), (401, CHALLENGE))
                    self.assertEqual(registry.audiences, [RESOURCE])

    def test_origin_check_cannot_be_satisfied_by_a_forged_host(self):
        app = create_app(oauth_registry(), allowed_hosts=DEFAULT_HOSTS)
        status, _, _ = call(app, '/health', {'host': 'localhost:1/x#', 'origin': 'http://localhost:1/x#'})
        self.assertEqual(status, 400)
        status, _, _ = call(app, '/health', {'host': 'localhost', 'origin': 'http://evil.example'})
        self.assertEqual(status, 403)

    def test_runtime_ignores_forwarded_headers(self):
        import os
        import tempfile
        from pathlib import Path
        from agenthub import cloud_runtime
        # A development profile (no `deployment` declared): main() validates the
        # declared transport from runtime.json before it opens a listener.
        with tempfile.TemporaryDirectory(prefix='vaelius-profile-') as profile:
            runtime = Path(profile) / 'runtime.json'
            os.close(os.open(runtime, os.O_WRONLY | os.O_CREAT, 0o600))
            runtime.write_text('{}')
            with patch.object(cloud_runtime, 'app_from_profile', return_value=object()), \
                    patch('uvicorn.run') as run, patch('os.umask'), \
                    patch.object(sys, 'argv', ['vaelius', '--profile', profile]):
                cloud_runtime.main()
        self.assertIs(run.call_args.kwargs['proxy_headers'], False)
        self.assertNotIn('forwarded_allow_ips', run.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
