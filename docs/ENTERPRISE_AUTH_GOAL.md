# Enterprise authentication delivery

Replace bespoke enterprise login/token lifecycle with an operator-configured
OAuth authorization server. Vaelius remains the resource server and sole owner
of tenant, delegation, enrollment and document authorization. Keycloak is the
reproducible local/self-hosted reference; no paid provider is selected.

1. Freeze the three PR regression cases and provider security matrix first.
2. Fix delegation narrowing, secure keyring selection and short access lifetimes.
3. Accept audience-restricted provider access tokens through bounded RFC 7662
   introspection, explicit issuer/subject bindings and canonical current checks.
   Register only digest metadata; never store provider refresh tokens on the server.
4. Publish protected-resource metadata and OAuth challenges for HTTP MCP/API.
   Legacy interactive issuance is disabled when provider auth is configured;
   explicitly provisioned service credentials and explicit legacy profiles remain
   compatible. There is one permission implementation and one knowledge authority.
5. Implement public-client authorization code/PKCE, provider refresh/revocation,
   secure storage and in-place reauthentication with preserved projects/outbox.
   Lost refresh replies require reauthentication unless a validated provider reply
   was durably saved; never retry an ambiguous rotation blindly.
6. Provide private, disposable Keycloak setup and run real authorization code,
   refresh, revocation, offboarding, wrong-audience, tenant and MCP delivery checks.
7. Verify installed wheels, SQL bounds/timings, migration/rollback, container,
   packaging, secret scans and CI. Commit and push a reviewable final branch.

Do not modify the user's live profile, capture new conversations, invoke models,
or deploy a cloud service. Record implementation, installed evidence and remaining
production controls separately in `ENTERPRISE_AUTH_ACCEPTANCE.md`.

Frozen security cases: the three reproduced PR failures; expired access versus
refresh; refresh/ID token rejection at protected resources; wrong audience/client;
inactive introspection; missing identity binding; disabled tenant/user; permission
reduction; logout; cross-tenant isolation; in-flight withdrawal; reauthentication
with preserved enrollment, source connections and private capture state.
