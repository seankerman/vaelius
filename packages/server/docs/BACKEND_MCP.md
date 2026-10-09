# Backend MCP

AgentHub serves Streamable HTTP MCP at `/mcp` with the official `mcp==2.3.0`
Python SDK. Its 13 tools use the same service operations, identity/delegation,
permission checks, lifecycle rules, delivery limits and retrieval metering as
REST. There is no second search implementation or client corpus.

The AgentClient installer registers `url` and `http_headers_helper` in the
host's MCP configuration. The helper reads the existing private credential file
and supplies a bearer token plus project/session headers. Tokens are not written
into MCP configuration. Hosts cache helper output; a credential change requires
their refresh/reconnect behavior. A tool's explicit `project` is still subject to
backend authorization. The service never trusts the project header as permission.

Each MCP HTTP request authenticates, including initialization and tool listing.
Each operation resolves current identity again. Search rechecks permissions and
revisions before and after optional model reranking. Host/origin checks apply
before dispatch; query-string credentials are unsupported. Errors omit private
source text. Responses are marked `no-store`.

The small `agentclient.mcp` stdio bridge remains for older hosts and existing
evaluation harnesses. It forwards JSON-RPC to this same endpoint and owns no
catalogue or business logic. New installations use direct HTTP. Remote tools
cannot write server files on behalf of the client: original-document download to
a client path remains an explicit client download operation.

This is a local authenticated transport implementation. It does not deploy an
internet endpoint, configure cloud OAuth discovery, or alter existing installed
profiles. The API must run with its ASGI lifespan enabled for the SDK session
manager. It uses stateless requests; observer conversations are independent of
MCP transport sessions.

## Verification

`test_backend_mcp` exercises official SDK initialization, tool listing, search,
schema rejection, authentication, revocation, host/origin controls and REST parity.
`test_mcp_transport` in AgentClient covers direct registration, private credential
headers, stdio forwarding and safe migration of the installer's own prior entry.
Tool behavior tests live in AgentHub, alongside their source/temporal/feedback
checks. These tests do not establish ordinary receiving-agent usefulness.

References: [official Python SDK ASGI integration](https://py.sdk.modelcontextprotocol.io/run/asgi/)
and [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
