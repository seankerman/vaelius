"""Current, compact all-input policy checks for PostgreSQL retrieval.

Policies are reusable source restrictions, not per-user allow lists. Every policy
required by a document must pass. The projection is transactionally maintained by
migration 022, including conservative observer/model-input provenance. Current
membership, connection readers and expiration are deliberately never cached here.
"""
import time


def inspect_projection(db):
    """Explicit operator audit, never part of a retrieval request.

    One statement compares the projection and authoritative relations at the same
    snapshot. Counts only; no private source text or document IDs are returned.
    """
    return dict(db.execute("""WITH expected AS MATERIALIZED (
        SELECT x.document_id,coalesce(p.policy_id,0) policy_id,count(*) n
        FROM enterprise_dependencies x LEFT JOIN search_source_policies p USING(source_id)
        GROUP BY x.document_id,coalesce(p.policy_id,0)
    ), differences AS (
        SELECT 1 FROM expected e FULL JOIN search_document_policies a USING(document_id,policy_id)
        WHERE e.n IS DISTINCT FROM a.dependency_count
    ), mapping_errors AS (
        SELECT 1 FROM search_source_policy_definitions s
        LEFT JOIN search_source_policies mapping USING(source_id)
        LEFT JOIN search_policies policy ON policy.id=mapping.policy_id
        WHERE policy.definition IS DISTINCT FROM s.definition
    )
    SELECT (SELECT count(*) FROM enterprise_dependencies) dependency_links,
        (SELECT count(*) FROM search_document_policies) document_policy_requirements,
        (SELECT count(DISTINCT policy_id) FROM search_document_policies) referenced_policies,
        (SELECT count(*) FROM differences) requirement_mismatches,
        (SELECT count(*) FROM mapping_errors) source_policy_mismatches,
        (SELECT count(*) FROM search_document_policies WHERE policy_id=0 OR dependency_count<=0) denying_requirements
    """).fetchone())


# The audience of one policy row (alias `policy`) for a reader who passes every
# ordinary source rule: the owner; otherwise never for origin_private
# (origin_ceiling 'private'); the item not private and its workspace in R (the
# reader's workspaces with a role >= reader); every carried workspace
# (origin_projects) in R; and a legacy origin ceiling of 'team' needs the workspace
# and every carried workspace in D (direct roles, not the organization entry). R and
# D are computed once per request (WorkspaceAccessMixin._reader_workspaces). There
# is no other way in: per-item sharing was removed (migrations 055 and 057). Mirrors
# search_reader_in_audience (migration 057) and WorkspaceAccessMixin._in_audience.
CARRIED = "ARRAY(SELECT jsonb_array_elements_text(coalesce(policy.definition->'origin_projects','[]'::jsonb)))"
AUDIENCE = """(policy.definition->>'owner'={A} OR (coalesce(policy.definition->>'origin_ceiling','')!='private'
                    AND coalesce((policy.definition->>'private')::boolean,false)=false
                    AND policy.definition->>'project'=ANY({R}::text[])
                    AND """ + CARRIED + """ <@ {R}::text[]
                    AND (coalesce(policy.definition->>'origin_ceiling','')!='team' OR (
                        policy.definition->>'project'=ANY({D}::text[]) AND """ + CARRIED + """ <@ {D}::text[]))))"""

# The session-label term of one policy row (recall, docs/ACCESS_MODEL.md rule 6):
# the item's audience contains the session audience. Never origin_private; the item
# not private and its workspace in S (the workspaces whose audience contains the
# session audience); every carried workspace in S; a legacy 'team' origin needs the
# workspace and every carried one in SD (direct audiences). The owner gets no short
# cut: a person's own private items are recalled only in a private session, which
# has no session term. Mirrors search_session_admits (migration 057) and
# WorkspaceAccessMixin._session_admits.
SESSION = """(coalesce(policy.definition->>'origin_ceiling','')!='private'
                    AND coalesce((policy.definition->>'private')::boolean,false)=false
                    AND policy.definition->>'project'=ANY({S}::text[])
                    AND """ + CARRIED + """ <@ {S}::text[]
                    AND (coalesce(policy.definition->>'origin_ceiling','')!='team' OR (
                        policy.definition->>'project'=ANY({SD}::text[]) AND """ + CARRIED + """ <@ {SD}::text[])))"""


def _bind(template, ctx, access=None):
    """Replace {T}/{A}/{R}/{D} (and the session's {S}/{SD}) placeholders, in order,
    with bound parameters."""
    import re
    args = []
    values = {'T': ctx['tenant'], 'A': ctx['actor']}
    if access is not None:
        values.update(R=list(access[0]), D=list(access[1]))
    session = ctx.get('session_label') or {}
    values.update(S=list(session.get('workspaces', ())), SD=list(session.get('direct', ())))
    def replace(match):
        args.append(values[match.group(1)])
        return '?'
    return re.sub(r'\{(SD|[TARDS])\}', replace, template), tuple(args)


def labelled(ctx):
    """The request carries a session label that restricts recall (not a private session)."""
    session = ctx.get('session_label')
    return bool(session) and not session.get('private')


def session_condition(ctx):
    """SESSION bound to the request's session label."""
    return _bind(SESSION, ctx)


def audience_condition(ctx, access):
    """AUDIENCE bound to the reader and their workspaces (R, D)."""
    return _bind(AUDIENCE, ctx, access)


def _common(ctx):
    """Every ordinary source rule of one policy row except the audience: tenant,
    lifecycle, delegation, source ACL freshness, connection readers and freshness,
    and the operator quarantine of held sources."""
    delegated = ctx.get('delegated_projects')
    delegated = sorted(delegated) if delegated is not None else None
    now = time.time()
    return """
                    (policy.definition->>'active')::integer=1
                    AND policy.definition->>'tenant'=?
                    AND (?::text[] IS NULL OR policy.definition->>'project'=ANY(?::text[]))
                    AND ((policy.definition->>'has_acl')::boolean=false OR (
                        policy.definition->>'acl_state'='current'
                        AND (policy.definition->>'acl_until')::double precision>?))
                    AND ((policy.definition->>'has_revision')::boolean=false OR (
                        connection.tenant=? AND connection.active=1
                        AND connection.permission_observed+connection.freshness_seconds>=?
                        AND (connection.reader_ids::jsonb='[]'::jsonb OR connection.owner=? OR EXISTS(
                            SELECT 1 FROM jsonb_array_elements_text(connection.reader_ids::jsonb) reader
                            WHERE reader.value=?))))
                    AND (coalesce((policy.definition->>'quarantined')::boolean,false)=false
                        OR policy.definition->>'owner'=?)""", (
        ctx['tenant'], delegated, delegated, now, ctx['tenant'], now, ctx['actor'], ctx['actor'], ctx['actor'])


def policy_condition(ctx, access):
    """One policy expression for the redacted passage tier of one source (alias `src`).

    The source owner or a reader in the item's audience; every ordinary rule
    applies to both. Retained originals never use this (agenthub.raw_access).
    `access` is (R, D).
    """
    common, common_args = _common(ctx)
    audience, audience_args = audience_condition(ctx, access)
    session, session_args = ('true', ())
    if labelled(ctx):
        session, session_args = session_condition(ctx)
    return ('(' + common + """
                    AND """ + audience + """
                    AND """ + session + """
                )""", common_args + audience_args + session_args)


def predicate(ctx, access):
    """Indexed document authorization; mirrors EnterpriseStore._document_allowed.

    One correlated pass over the document's policies checks every ordinary source
    rule and the audience: every source a document depends on is the reader's
    own or has the reader in its audience. `access` is (R, D).
    """
    common, common_args = _common(ctx)
    audience, audience_args = audience_condition(ctx, access)
    session, session_args = ('', ())
    if labelled(ctx):
        # Recall in a labelled session: also the session term (SESSION) on every policy.
        session, session_args = session_condition(ctx)
        session = ' AND ' + session
    # raw_shared (the passage's own source is not private) is a stable prefilter
    # that keeps other people's private passages cheap to exclude. The policy pass
    # below decides.
    return """AND (ed.representation!='raw' OR (? AND (ed.raw_owner=? OR ed.raw_shared>0)))
        AND (SELECT bool_and(required.dependency_count>0 AND (""" + common + """
                AND """ + audience + session + """) IS TRUE)
        FROM search_document_policies required
        LEFT JOIN search_policies policy ON policy.id=required.policy_id
        LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
        WHERE required.document_id=d.document_id)""", ('read' in ctx['actions'], ctx['actor'], *common_args,
                                                       *audience_args, *session_args)


def validate_source_snapshots(store, db, ctx, expected):
    """Current raw authorization and exact source identity in bounded indexed batches.

    No positive authorization cache. Call again at delivery. Compare every frozen
    influence, not just selected citations. Bodies stay in PostgreSQL; only hashes
    cross the connection. Missing projection, source, or body denies the snapshot.
    """
    store.current_identity(db,ctx)
    # Redacted passage tier (owner, or in the item's audience): `read` suffices. Retained
    # originals are a separate source_read + owner/grant check in agenthub.raw_access.
    store._need(ctx,'read')
    store._label(db,ctx)
    condition,args=policy_condition(ctx,store._reader_workspaces(db,ctx))
    ids=sorted(expected)
    for offset in range(0,len(ids),1000):
        batch=ids[offset:offset+1000]
        # Materialize only this requested batch. Without the fence PostgreSQL
        # can start with every allowed policy mapping and perform tens of
        # thousands of source lookups before testing src.id against the batch.
        rows=db.execute("""WITH requested_sources AS MATERIALIZED (
                SELECT id,source_version,tenant,owner,active,external_project FROM enterprise_sources WHERE id=ANY(?::text[])
            ) SELECT src.id,src.source_version,
            encode(sha256(convert_to(m.body,'UTF8')),'hex') body_sha256
            FROM requested_sources src
            JOIN search_source_policies mapping ON mapping.source_id=src.id
            JOIN search_policies policy ON policy.id=mapping.policy_id
            LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
            JOIN memories m ON m.id=src.id AND m.active=1
            WHERE """+condition,(batch,*args)).fetchall()
        if len(rows)!=len(batch):raise PermissionError('source_no_longer_visible_or_changed')
        for row in rows:
            saved=expected[row['id']]
            if row['source_version']!=saved['version'] or row['body_sha256']!=saved['canonical_sha256']:
                raise PermissionError('source_changed')


def current_document_revisions(store, db, ctx, documents):
    """Return current authorized revisions in bounded indexed batches."""
    from types import SimpleNamespace
    expected={}
    for document in documents:
        if document['id'] in expected and expected[document['id']]!=document['revision']:
            raise PermissionError('conflicting_document_snapshots')
        expected[document['id']]=document['revision']
    ids=sorted(expected)
    current={}
    for offset in range(0,len(ids),1000):
        batch=ids[offset:offset+1000]
        where,args=store._authorization_predicate(SimpleNamespace(db=db),ctx,candidate_ids=batch)
        rows=db.execute('''SELECT d.document_id,d.active_revision_id FROM enterprise_documents ed
            JOIN knowledge_documents d ON d.document_id=ed.id
            WHERE d.document_id=ANY(?::text[]) AND '''+where,(batch,*args)).fetchall()
        allowed={row['document_id']:row['active_revision_id'] for row in rows}
        missing=sorted(set(batch)-set(allowed))
        if missing:
            # Rare explicitly reviewed releases retain their canonical stricter
            # authorization; normal documents never invoke a per-ID oracle.
            releases=db.execute('SELECT document_id FROM backend_releases WHERE document_id=ANY(?::text[])',
                (missing,)).fetchall()
            for row in releases:
                document=store._document_allowed(db,ctx,row['document_id'])
                if document:allowed[row['document_id']]=document['active_revision_id']
        current.update(allowed)
    return current


def validate_document_snapshots(store, db, ctx, documents):
    """Fail closed if any exact frozen document is no longer authorized/current."""
    current=current_document_revisions(store,db,ctx,documents)
    if any(current.get(row['id'])!=row['revision'] for row in documents):
        raise PermissionError('document_changed_or_withdrawn')

def read_source_context(store,db,ctx,ids):
    """Bounded redacted source context (owner or in the item's audience) through the indexed raw policy oracle."""
    store.current_identity(db,ctx);store._need(ctx,'read')
    ids=sorted(set(ids))
    if len(ids)>1000:raise ValueError('context_source_batch_bound')
    if not ids:return []
    store._label(db,ctx)
    condition,args=policy_condition(ctx,store._reader_workspaces(db,ctx))
    return [dict(row) for row in db.execute('''WITH requested AS MATERIALIZED (
        SELECT * FROM enterprise_sources WHERE id=ANY(?::text[])
    ) SELECT src.id,src.source_version AS version,src.external_project AS project,
        src.occurred_at,m.body,m.session,m.turn,m.kind,
        encode(sha256(convert_to(m.body,'UTF8')),'hex') AS canonical_sha256
        FROM requested src JOIN memories m ON m.id=src.id AND m.active=1
        JOIN search_source_policies mapping ON mapping.source_id=src.id
        JOIN search_policies policy ON policy.id=mapping.policy_id
        LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
        WHERE '''+condition,(ids,*args)).fetchall()]
