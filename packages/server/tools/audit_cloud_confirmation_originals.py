#!/usr/bin/env python3
"""Read-only exact-original audit, without search or model requests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

SOURCE_SHA = '561c61b8dcfe0b727b17b14c64c7b213d1032bc5bd57aa9e353193041f9116fc'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirect_denied')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True)
    p.add_argument('--source-fixture', required=True)
    p.add_argument('--preflight', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    profile = Path(args.profile)
    raw = Path(args.source_fixture).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise ValueError('source_fixture_changed')
    seed = json.loads((profile / 'synthetic-seed.json').read_text())
    sources = {s['id']: s['text'].encode() for s in json.loads(raw)['sources']}
    if seed['fixture_sha256'] != SOURCE_SHA or set(seed['sources']) != set(sources) or len(sources) != 60:
        raise ValueError('seed_mapping_mismatch')
    preflight = json.loads(Path(args.preflight).read_text())
    token = (profile / 'credentials/acme-alice.token').read_text().strip()
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def request(path, payload=None, binary=False):
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request('http://127.0.0.1:55486' + path, data=body,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        bound = 50 * 1024 * 1024 if binary else 128 * 1024
        with opener.open(req, timeout=15) as response:
            result = response.read(bound + 1)
        if len(result) > bound:
            raise ValueError('response_bound')
        return result if binary else json.loads(result)
    health = request('/health')
    if health['build_ids'] != preflight['build_ids']:
        raise ValueError('api_build_mismatch')
    request('/ready')
    rows = []
    start = time.monotonic()
    for fixture_id, source_id in sorted(seed['sources'].items()):
        descriptor = request('/enterprise/v3/source-documents/describe', {'source_id': source_id})
        body = request('/enterprise/v3/source-documents/download', {'source_id': source_id}, True)
        sha = hashlib.sha256(body).hexdigest()
        if body != sources[fixture_id] or descriptor.get('sha256') != sha:
            raise ValueError('original_mismatch')
        rows.append({'fixture_id': fixture_id, 'source_id': source_id, 'bytes': len(body), 'sha256': sha})
    value = {'status': 'all_originals_verified_after_once_measurement', 'created_at_unix': time.time(),
        'build_ids': health['build_ids'], 'verified_originals': len(rows),
        'original_bytes': sum(r['bytes'] for r in rows), 'source_fixture_sha256': SOURCE_SHA,
        'object_manifest_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        'seconds': time.monotonic() - start, 'search_calls': 0, 'embedding_calls': 0, 'provider_calls': 0,
        'timing_limit': 'Later authenticated read-only downloads after the harness aggregation error.'}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, indent=2, sort_keys=True)
    print(json.dumps(value))


if __name__ == '__main__':
    main()
