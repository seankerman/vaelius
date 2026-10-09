# Search request timings

The API emits one `search_request_timing` JSON record for each POST to the v1 or
v3 search endpoint that reaches dispatch. Successful responses include its
server-generated `X-AgentHub-Request-ID`. This ID is independent of the client's
request ID; it identifies the diagnostic record, not a billing or retry key.

The record contains only a random ID, status, total milliseconds and fixed-name
stage durations. It contains no query, source text, SQL, credential, tenant,
principal, client request ID or exception message. Uvicorn's existing operator
log handler receives the records. Configure retention/rotation at the service
log sink; timing collection does not write another database row per stage.

| Stage | Includes |
| --- | --- |
| authenticate | All authentication calls during this request, aggregated |
| ready | Readiness and recovery-hold checks |
| search | Canonical search, including candidate generation and selection |
| candidates | Candidate lookup, policy setup, text/vector rank and fusion |
| lexical_sql | Authorized text-ranking statement |
| embedding | Query embedding, including cache lookup/model initialization when needed |
| vector_sql | Authorized vector-ranking statement |
| selection | Supported-card selection |
| delivery_lock_wait | Acquiring the final shared delivery lock |
| delivery_check | Current authorization/revision check, including reauthentication |
| meter | Post-search authentication and usage recording |

**Durations are inclusive and overlap. Do not sum them.** Request elapsed time
also includes dispatch/thread scheduling, body decoding and response encoding.
It ends before network transmission; compare it with client wall time for that
remaining overhead. Failed dispatches still produce a timing record, without a
response diagnostic ID. Requests rejected before dispatch by the host middleware
do not produce this record.

Context-local traces isolate concurrent requests. Direct internal calls have no
trace unless an operator explicitly enters a capture context. Diagnostics never
change an API outcome if their sink fails. They do not weaken the current-policy
delivery check or expose detailed evidence to callers.
