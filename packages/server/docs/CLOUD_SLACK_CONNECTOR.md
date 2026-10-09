# First vendor adapter: read-only selected Slack channels

`SlackConnector` connects explicitly enrolled selected channels and fetched threads
to the existing general source intake and continuous backend observer. It makes
zero model calls itself and has no connector-specific knowledge corpus. Stable
message identity includes channel and Slack message timestamp; equal text from
different speakers remains distinct. Canonical source revisions preserve verified
internal speaker, vendor user, thread, timestamps and edit lineage.

The initial capability excludes DMs, shared/cross-organization channels, archived
channels, unmapped bots, unsupported message subtypes and Slack files/attachments.
File ACL semantics have not been established; an attachment is held explicitly,
not downloaded with the connector's privileges. Private channel readership requires
all current vendor members to have explicit operator-verified internal bindings.
Source authors/display names/emails never authorize access. Unknown mappings or
unsupported channel policies hold capture and make existing policy stale so recall
and observer dependencies fail closed. Membership removals narrow current readers.
Membership expansion after narrowing requires explicit operator re-enrollment.

Complete paginated channel/member/thread responses are fetched within explicit
message/page bounds before capture/checkpoint advancement. Failed/partial windows
never tombstone absent messages. Signed explicit Slack deletion events delete
their sources through the canonical lifecycle, including retained originals and
cached private payloads. The durable vendor item tombstone prevents stale polling
from recreating a deleted message. Event signatures authenticate the
exact raw request body with Slack's v0 HMAC scheme and reject timestamps outside
five minutes. Event identity/digest deduplication and source event timestamps protect
transport retry and out-of-order edits/deletions. Polling incremental history alone
cannot detect old edits/deletions: event delivery is a required real integration
input, or an operator must run a bounded historical reconciliation from an earlier
checkpoint. No event endpoint registration is performed by local tests.

Deletion inventories only the same connector's selected channel/message revisions;
other messages/connectors stay intact. Inventory, canonical revision purges and
the item tombstone share the capture/edit advisory lock. The operation accepts at
most 100 revisions with a 60-second processing deadline. A larger inventory or
partial object failure holds current connection permissions and leaves the signed
event unaccepted. Recovery requires explicit bounded membership sync and retry of
the same signed deletion; no missing-object projection is silently repaired.

429 responses create durable per-connector/per-method Retry-After deadlines. Finite
sync exits without sleeping or spinning. Restart preserves the deadline and capture
checkpoint; another method remains independently usable. Client requests carry
tokens only in authorization headers and reject redirects. Default HTTP adapters
require a loopback endpoint. Real `https://slack.com/api/` requires explicit
`allow_vendor: true` and prior source authorization; finding a token is insufficient.

Use a private mode-0600 connector JSON with `backend_profile` (the explicit private
cloud profile directory), `tenant`,
`enterprise_token`, `slack_token`, `endpoint`, `allow_vendor`, `connector`, `team`,
`channel`, `project`, `bindings` and optional `freshness`. Do not put credentials or
vendor source bodies in Git. Example bounded operator commands:

~~~sh
python -m agenthub.slack_connector --profile /private/slack.json discover
python -m agenthub.slack_connector --profile /private/slack.json enroll
python -m agenthub.slack_connector --profile /private/slack.json sync --dry-run --max-messages 100 --max-pages 5
python -m agenthub.slack_connector --profile /private/slack.json sync --max-messages 100 --max-pages 5
python -m agenthub.slack_connector --profile /private/slack.json status
~~~

These commands do not run on startup and do not create recurring collection.
Cloud mode resolves the selected tenant through the profile's canonical runtime
registry and uses its object adapter. New messages/completion markers retain
byte-verified structured segments before source acceptance; the configured worker
checks those originals before dispatch. Object/canonical capture failures hold the
connection's current permission state and preserve the prior sync checkpoint.
After recovery, an explicit bounded sync reconciles membership and retries the same
source versions. No manual `retain-existing` step is needed for new cloud capture.

An optional legacy `dsn` or `home` alongside `backend_profile` must exactly match
the resolved route. Conflicting routes, unknown tenants, missing object adapters
and wrong-tenant credentials refuse before Slack HTTP. Without `backend_profile`,
the independent compatibility mode still requires `home` and `dsn` and stores
PostgreSQL-only sources. It does not satisfy the cloud original-object worker
guard; there is no silent fallback between these modes.

Provider-free tests use an actual local HTTP fixture server and real PostgreSQL:
`python -m unittest discover -s tests -p test_cloud_slack.py`. Set an explicitly
isolated `CLOUD_TEST_DSN`; otherwise database rows are skipped and remain pending.
Mocked vendor fixtures are authored engineering cases, not independent held-out or
real workspace evidence. No real workspace has been enrolled by this implementation.
The additional frozen `cloud_slack_backend_v1.json` fixture drives the actual
subprocess CLI and configured worker in disposable tenant databases:
`python -m unittest discover -s tests -p test_cloud_slack_backend.py` with
`AGENTNETWORK_PG_SERVICES` pointing to the explicit local services file. That test
creates/deletes only its own `test_slack_*` databases/roles. Its old pre-fix
reproduction is intentionally skipped in the current suite; active tests remain
authored regression evidence, with zero provider/vendor calls.

Vendor references rechecked September 26, 2026:

- [Conversations history](https://docs.slack.dev/reference/methods/conversations.history/)
- [Conversations replies](https://docs.slack.dev/reference/methods/conversations.replies/)
- [Rate limits and Retry-After](https://docs.slack.dev/apis/web-api/rate-limits/)
- [Verifying Slack requests](https://docs.slack.dev/authentication/verifying-requests-from-slack/)

Token/scopes and app distribution determine actual method availability/rate limits.
The channel replies method may require an appropriate user token; a successful
fixture is not proof that a customer's chosen token can call it. Actual vendor
OAuth, workspace scope verification and revocation remain external acceptance.
