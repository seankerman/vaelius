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
safe attachment headers. Its caller must close that stream, authenticate and check
current authorization immediately before sending it. Historical versions use
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

Moto is a local SDK protocol test double. Its in-memory restart does not prove S3
durability, IAM, object locks, encryption or failure semantics. File-adapter restart
tests are labeled private local recovery evidence. Later cloud acceptance must
exercise actual S3 credentials and policies. Endpoint configuration never falls
back to AWS, and both credentials are explicit so ambient credentials are unused.

Provider-free checks: `python -m unittest discover -s tests -p test_cloud_objects.py`.
Set `CLOUD_TEST_DSN` and optionally `CLOUD_TEST_TENANT` to an explicitly isolated,
migrated PostgreSQL test database to run its integration rows; omission is a skip,
not PostgreSQL acceptance. Only synthetic sources enter these tests.
