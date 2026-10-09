"""Immutable individual source objects; metadata authority remains PostgreSQL.

The private file adapter is for durable local recovery rehearsals. S3 tests use an
explicit SDK endpoint and never discover ambient AWS credentials or endpoints.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

FILE_LIMIT = 50 * 1024 * 1024
READ_SIZE = 64 * 1024


class ObjectMissing(Exception):
    pass


class ObjectConflict(Exception):
    pass


class ObjectCorrupt(Exception):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    length: int
    sha256: str


def object_key(tenant: str, source: str, version: str, sha256: str) -> str:
    """No user paths, source URLs, filenames or credentials enter object names."""
    if not all(isinstance(x, str) and x for x in (tenant, source, version)):
        raise ValueError('invalid_object_identity')
    if not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise ValueError('invalid_object_checksum')
    hashed = [hashlib.sha256(x.encode()).hexdigest() for x in (tenant, source, version)]
    return '/'.join(['sources', *hashed, sha256])


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'sources(?:/[0-9a-f]{64}){4}', key):
        raise ValueError('invalid_object_key')
    return key


@contextmanager
def bounded_spool(stream, *, limit=FILE_LIMIT, expected_sha256=None):
    """Read a stream with finite reads, enforcing the byte bound before upload."""
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid_file_bound')
    digest = hashlib.sha256(); length = 0
    with tempfile.TemporaryFile() as spool:
        while True:
            block = stream.read(min(READ_SIZE, limit - length + 1))
            if not block:
                break
            if not isinstance(block, bytes):
                raise ValueError('binary_stream_required')
            length += len(block)
            if length > limit:
                raise ValueError('source_file_too_large')
            digest.update(block); spool.write(block)
        sha = digest.hexdigest()
        if expected_sha256 is not None and sha != expected_sha256:
            raise ObjectCorrupt('source_checksum_mismatch')
        spool.seek(0)
        yield spool, length, sha


class FileSourceObjects:
    """Checksummed private test storage, explicitly distinct from S3 durability."""
    kind = 'private_file_recovery_adapter'

    def __init__(self, root, *, limit=FILE_LIMIT):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.limit = limit

    def _path(self, key):
        validate_key(key)
        # One private directory avoids attacker-controlled path components.
        return self.root / hashlib.sha256(key.encode()).hexdigest()

    def put(self, key, stream, *, expected_sha256=None):
        target = self._path(key)
        with bounded_spool(stream, limit=self.limit, expected_sha256=expected_sha256) as (spool, length, sha):
            if sha != key.rsplit('/', 1)[1]:
                raise ObjectCorrupt('object_identity_checksum_mismatch')
            fd, name = tempfile.mkstemp(prefix='.upload-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as out:
                    while block := spool.read(READ_SIZE):
                        out.write(block)
                    out.flush(); os.fsync(out.fileno())
                try:
                    os.link(name, target)
                except FileExistsError:
                    prior = self.head(key)
                    if (prior.length, prior.sha256) != (length, sha):
                        raise ObjectConflict('object_already_exists_with_different_bytes')
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                os.unlink(name)
        return ObjectInfo(key, length, sha)

    def open(self, key):
        path = self._path(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise ObjectMissing('source_object_missing') from None
        return os.fdopen(fd, 'rb')

    def head(self, key):
        with self.open(key) as source, bounded_spool(source, limit=self.limit) as (_, length, sha):
            return ObjectInfo(key, length, sha)

    def delete(self, key):
        # Authorization/reference/in-flight checks belong to DocumentStore.
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


class S3SourceObjects:
    """SDK adapter with explicit, restricted local test endpoint and credentials."""
    kind = 's3_sdk_local_endpoint'

    def __init__(self, endpoint, bucket, access_key, secret_key, *, region='us-east-1',
                 local_hosts=(), limit=FILE_LIMIT, workload_identity=False):
        if workload_identity:
            if endpoint is not None or access_key is not None or secret_key is not None or not bucket:
                raise ValueError('aws_workload_configuration')
            import boto3
            from botocore.config import Config
            self.client=boto3.client('s3',region_name=region,config=Config(
                signature_version='s3v4',retries={'max_attempts':0},connect_timeout=5,read_timeout=15))
            self.kind='s3_aws';self.bucket=bucket;self.limit=limit
            return
        address = urlsplit(endpoint)
        permitted = {'127.0.0.1', 'localhost', '::1', *local_hosts}
        if (address.scheme not in {'http', 'https'} or address.hostname not in permitted
                or address.username or address.password or address.query or address.fragment
                or address.path not in {'', '/'}):
            raise ValueError('explicit_local_s3_endpoint_required')
        if not access_key or not secret_key or not bucket:
            raise ValueError('explicit_s3_configuration_required')
        import boto3
        from botocore.config import Config
        self.client = boto3.client('s3', endpoint_url=endpoint,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            region_name=region, config=Config(signature_version='s3v4',
                retries={'max_attempts': 0}, connect_timeout=5, read_timeout=15,
                s3={'addressing_style': 'path'}))
        self.bucket = bucket; self.limit = limit

    def put(self, key, stream, *, expected_sha256=None):
        validate_key(key)
        from botocore.exceptions import ClientError
        with bounded_spool(stream, limit=self.limit, expected_sha256=expected_sha256) as (spool, length, sha):
            if sha != key.rsplit('/', 1)[1]:
                raise ObjectCorrupt('object_identity_checksum_mismatch')
            try:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=spool,
                    ContentLength=length, Metadata={'sha256': sha}, IfNoneMatch='*')
            except ClientError as error:
                if error.response['Error']['Code'] not in {'PreconditionFailed', '412'}:
                    raise
                prior = self.head(key)
                if (prior.length, prior.sha256) != (length, sha):
                    raise ObjectConflict('object_already_exists_with_different_bytes') from None
        return ObjectInfo(key, length, sha)

    def open(self, key):
        validate_key(key)
        from botocore.exceptions import ClientError
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)['Body']
        except ClientError as error:
            if error.response['Error']['Code'] in {'NoSuchKey', '404'}:
                raise ObjectMissing('source_object_missing') from None
            raise

    def head(self, key):
        # Actual retained bytes are verified; neither ETag nor stored metadata is
        # accepted as sufficient proof of a source content checksum.
        with self.open(key) as source, bounded_spool(source, limit=self.limit) as (_, length, sha):
            return ObjectInfo(key, length, sha)

    def delete(self, key):
        validate_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)


def verify(info, expected_length, expected_sha256):
    if (info.length, info.sha256) != (expected_length, expected_sha256):
        raise ObjectCorrupt('retained_source_checksum_mismatch')
    return info
