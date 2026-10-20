# Original documents and individual source objects

The PostgreSQL profile uses `DocumentStore(store, objects)` with either
`S3SourceObjects` pointing at an explicitly configured local SDK endpoint or
`FileSourceObjects` for private durable recovery rehearsals. PostgreSQL is the
metadata/knowledge authority. Object storage contains original bytes, not a second
search corpus. One original document/version has one object even when it produces
many canonical native passages.

`DocumentStore.ingest(ctx, connection, external_id, version, filename, stream,
title=..., media_type=...)` accepts an explicitly enrolled document connection.
The default streaming original-file limit is 50 MiB, separately from agent event
limits. SHA-256 and byte length are verified against retained bytes. An immutable
document/version cannot be reused for different bytes. Upload reservation commits
before object writes; interrupted metadata completion can be retried with the same
identity and bytes. Fully accepted duplicates verify the retained object again.

TXT/Markdown, HTML, text PDF and DOCX produce faithful normalized text and canonical
native knowledge passages without model calls. Markdown headings, HTML tables,
PDF page references and DOCX headings/tables are retained where the format permits.
HTML scripts/styles are absent from parsed text. The original HTML bytes remain
unchanged. Unsupported, encrypted, OCR-only, unavailable and failed parsers have
explicit dispositions and remain independently retrievable as original files.
OCR is not implemented. DOCX decompression has a separate 100 MiB expansion bound.

`describe(ctx, source_id, version=...)` returns bounded metadata, never file bodies
or base64. `list(ctx, title=..., limit=...)` discovers readable current originals.
`fetch(ctx, source_id, version=...)` returns a verified temporary binary stream and
safe attachment headers. Original bytes are least privilege: besides `describe`'s
current read access, `fetch` requires the credential action `source_read` and
either source ownership or an explicit owner-issued raw grant
(`python -m agenthub.raw_access`, see [data boundaries](../../../docs/DATA_BOUNDARIES.md#raw-access-policy)).
Team or organization visibility alone no longer permits download. Its caller must
close that stream, authenticate and recheck with `authorize_original(ctx, ...)`
immediately before sending it. Historical versions use
the current source head's permissions. Revocation blocks historical download;
already delivered bytes cannot be recalled. Content is sent as an attachment with
`application/octet-stream`, `nosniff`, no-store and a checksum header, so untrusted
HTML does not execute as a same-origin page.

Authenticated API endpoints are `/enterprise/v3/source-documents/describe`,
`/download` and `/upload`. Actual route acceptance is recorded separately from
parser/service tests. Source identifiers are opaque; a source URL or path is never
dereferenced as an arbitrary server file. MCP returns a bounded descriptor;
`find_source_documents` discovers authorized title metadata before the result
limit. An explicit `fetch_source_document` request with `download_to` writes
verified original bytes to a new absolute local file. The HTTP/CLI consumer can
also fetch those bytes outside normal memory context. The original is not replaced
with a reconstruction or summary.

Cleanup refuses accepted references, in-flight uploads or recent reservations.
An uploaded but unaccepted orphan may be removed only after the explicit grace
boundary while its metadata row is locked. Unknown/missing or checksum-mismatched
retained bytes fail retrieval; they do not silently fall back to a summary.

Uploads containing a detected credential (in bytes, parsed text or metadata) are
rejected before any object write; originals are never rewritten.

## Hardening

- **No body logging.** Adapters never log object bodies or put them in exception
  text. Keep `botocore` loggers below DEBUG in production regardless.
- **Local files.** `FileSourceObjects` writes each object 0600 inside a 0700 root,
  independent of the process umask.
- **TLS.** `s3_aws` refuses a non-HTTPS endpoint, including an ambient override such
  as `AWS_ENDPOINT_URL_S3`. An explicit `s3` endpoint may use plain HTTP only on a
  loopback address (a local emulator); any other configured host must use HTTPS.
- **SSE-KMS.** Optional `kms_key_id` (key ID, alias or ARN) on `s3_aws` or `s3`:

  ```json
  {"objects": {"kind": "s3_aws", "bucket": "example-vaelius-originals", "region": "us-east-1",
    "kms_key_id": "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"}}
  ```

  Before the first write the adapter calls `kms:DescribeKey` and requires an enabled
  symmetric encryption key; otherwise it raises `ObjectEncryptionUnavailable` and
  writes nothing. Each `PutObject` requests `aws:kms` with that key (S3 Bucket Keys
  enabled) and must be confirmed by the response; an unconfirmed new object is
  deleted and the upload fails. A key disabled later makes S3 refuse writes and
  reads, which also fails closed. Existing objects are not re-encrypted.

Example least-privilege bucket policy (synthetic account, role, bucket and key;
also enable S3 Block Public Access and, if required, versioning with expiry):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": ["arn:aws:s3:::example-vaelius-originals", "arn:aws:s3:::example-vaelius-originals/*"],
     "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
    {"Sid": "DenyUnencryptedOrOtherKey", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
     "Resource": "arn:aws:s3:::example-vaelius-originals/*",
     "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption-aws-kms-key-id":
       "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"}}},
    {"Sid": "ServiceObjectAccess", "Effect": "Allow",
     "Principal": {"AWS": "arn:aws:iam::111122223333:role/example-vaelius-service"},
     "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::example-vaelius-originals/sources/*"}
  ]
}
```

Example key policy statements for the same key (keep your account's key
administration statement alongside them):

```json
[
  {"Sid": "ServiceUseThroughS3Only", "Effect": "Allow",
   "Principal": {"AWS": "arn:aws:iam::111122223333:role/example-vaelius-service"},
   "Action": ["kms:GenerateDataKey", "kms:Decrypt"], "Resource": "*",
   "Condition": {"StringEquals": {"kms:ViaService": "s3.us-east-1.amazonaws.com",
     "kms:EncryptionContext:aws:s3:arn": "arn:aws:s3:::example-vaelius-originals"}}},
  {"Sid": "ServiceVerifiesKey", "Effect": "Allow",
   "Principal": {"AWS": "arn:aws:iam::111122223333:role/example-vaelius-service"},
   "Action": "kms:DescribeKey", "Resource": "*"}
]
```

With S3 Bucket Keys the encryption context is the bucket ARN, as above. The service
role needs no `s3:ListBucket`, no bucket-policy rights and no other key actions.

Moto is a local SDK protocol test double. Its in-memory restart does not prove S3
durability, IAM, object locks, encryption or failure semantics. File-adapter restart
tests are labeled private local recovery evidence. Later cloud acceptance must
exercise actual S3 credentials and policies. Endpoint configuration never falls
back to AWS, and both credentials are explicit so ambient credentials are unused.

Provider-free checks: `python -m unittest discover -s tests -p test_cloud_objects.py`.
Set `CLOUD_TEST_DSN` and optionally `CLOUD_TEST_TENANT` to an explicitly isolated,
migrated PostgreSQL test database to run its integration rows; omission is a skip,
not PostgreSQL acceptance. Only synthetic sources enter these tests.
