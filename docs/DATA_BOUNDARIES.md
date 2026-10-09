# Data and security boundaries

Vaelius stores deterministically redacted source records and their provenance,
then indexes exact passages. Redaction removes recognizable credential patterns;
it is not universal secret detection or blanket anonymization. Useful permitted
person and location facts can remain. Unsupported source formats and capture gaps
remain explicit rather than being labeled complete.

Raw agent originals and raw source passages require owner/source access. A project
or team label does not itself grant access. Reviewed derived knowledge and native
document representations retain their source policy dependencies. Identity,
delegation, connection freshness, lifecycle state and exact revision integrity are
rechecked before evidence is delivered. The operator controls enrollment and
sharing; installation grants neither.

Search results are untrusted references. Similarity scores are discovery signals,
not proof of truth or complete answers. Agents can fetch cited evidence, expand
surrounding conversation and inspect originals before deciding. LLM output is
validated by the canonical backend; optional enrichment does not replace originals.

Withdrawal blocks access to dependent knowledge while preserving recorded history
under its lifecycle policy. Deletion purges source/derived content and records its
denial/deletion journal. Scope changes invalidate stale processing and retrieval.
User preferences can be private to one user independently of team knowledge.

This prototype trusts the service operator. Application ACLs do not hide database
contents from an administrator with host/database credentials. A production host
still needs network isolation, TLS, backups, secret management, operational
monitoring and verified identity-provider configuration. Local synthetic tests do
not establish cloud deployment or real-team acceptance.

See [SECURITY.md](../SECURITY.md),
[source-first semantics](../packages/server/docs/SOURCE_FIRST.md), and
[indexed vector access](../packages/server/docs/VECTOR_INDEX.md).
