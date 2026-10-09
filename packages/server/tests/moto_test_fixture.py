"""Actual SDK feature fixture with an explicitly declared local emulator.

Use a unique owned bucket when Moto is a prestarted local service; otherwise
start the optional Python fixture. Never discover ambient cloud credentials.
"""
from contextlib import contextmanager
import os
import uuid

from agenthub.source_objects import S3SourceObjects


@contextmanager
def sdk_objects(prefix):
    endpoint = os.environ.get('CLOUD_TEST_MOTO_ENDPOINT')
    server = None
    if not endpoint:
        from moto.server import ThreadedMotoServer
        server = ThreadedMotoServer(ip_address='127.0.0.1', port=0, verbose=False)
        server.start()
        host, port = server.get_host_and_port()
        endpoint = f'http://{host}:{port}'
    allowed = tuple(filter(None, os.environ.get('CLOUD_TEST_MOTO_HOSTS', '').split(',')))
    adapter = S3SourceObjects(endpoint, prefix+'-'+uuid.uuid4().hex, 'fixture', 'fixture', local_hosts=allowed)
    created = False
    try:
        adapter.client.create_bucket(Bucket=adapter.bucket)
        created = True
        yield adapter
    finally:
        if created:
            # Frozen fixtures contain fewer than ten objects; never purge a
            # shared bucket or an unbounded emulator inventory.
            response = adapter.client.list_objects_v2(Bucket=adapter.bucket, MaxKeys=100)
            if response.get('IsTruncated'):
                raise RuntimeError('synthetic_moto_cleanup_bound_exceeded')
            for item in response.get('Contents', []):
                adapter.client.delete_object(Bucket=adapter.bucket, Key=item['Key'])
            adapter.client.delete_bucket(Bucket=adapter.bucket)
        if server is not None:
            server.stop()
