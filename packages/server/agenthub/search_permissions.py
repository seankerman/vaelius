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
# (origin_ceiling 'private'); the inherited path needs the item not private and
# its workspace in R (the reader's workspaces with a role >= reader); the share
# path ({SHARE}, an SQL condition) stands in for that term only. Both paths need
# every carried workspace (origin_projects) in R, and a legacy origin ceiling of
# 'team' needs the workspace and every carried workspace in D (direct roles, not
# the organization entry). R and D are computed once per request
# (WorkspaceAccessMixin._reader_workspaces). Mirrors search_reader_in_audience
# (migration 048) and WorkspaceAccessMixin._in_audience / _shared_with.
CARRIED = "ARRAY(SELECT jsonb_array_elements_text(coalesce(policy.definition->'origin_projects','[]'::jsonb)))"
AUDIENCE = """(policy.definition->>'owner'={A} OR (coalesce(policy.definition->>'origin_ceiling','')!='private'
                    AND ((coalesce((policy.definition->>'private')::boolean,false)=false
                            AND policy.definition->>'project'=ANY({R}::text[])) OR {SHARE})
                    AND """ + CARRIED + """ <@ {R}::text[]
                    AND (coalesce(policy.definition->>'origin_ceiling','')!='team' OR (
                        policy.definition->>'project'=ANY({D}::text[]) AND """ + CARRIED + """ <@ {D}::text[]))))"""


# One source (alias {source}, its policy row {policy}) is shared with the reader by
# an explicit share (agenthub.owner_controls): an active grant of the passage tier
# from the source's owner, for the source, its capture session or its connection,
# to the reader or a group the reader belongs to (group_closure). Never while the
# operator restricts owner sharing (owner-issued grants), in an archived workspace,
# or to a quarantined reader. A share stands in for the workspace audience only:
# the carried-workspace and origin terms of AUDIENCE and every ordinary rule still
# apply. Mirrors search_passage_granted (migration 048) and
# WorkspaceAccessMixin._shared_with.
SOURCE_GRANTED = """({source}.tenant={T} AND {source}.active=1 AND {source}.owner!={A}
                    AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held_reader
                        WHERE held_reader.tenant={T} AND held_reader.principal={A})
                    AND EXISTS(SELECT 1 FROM enterprise_organizations grant_tenant
                        JOIN enterprise_projects grant_project ON grant_project.tenant=grant_tenant.id
                            AND grant_project.id={source}.external_project AND grant_project.active=1
                        JOIN raw_source_grants grant_row ON grant_row.tenant={source}.tenant
                            AND grant_row.revoked IS NULL AND grant_row.passages=1
                            AND grant_row.granted_by={source}.owner
                        WHERE grant_tenant.id={source}.tenant
                        AND (grant_row.issued_with='policy' OR grant_tenant.owner_sharing='allowed')
                        AND ((grant_row.scope_kind='source' AND grant_row.scope_id={source}.id)
                            OR (grant_row.scope_kind='session' AND grant_row.scope_id=(
                                SELECT grant_memory.session FROM memories grant_memory WHERE grant_memory.id={source}.id))
                            OR (grant_row.scope_kind='connection'
                                AND grant_row.scope_id={policy}.definition->>'connection'))
                        AND ((grant_row.grantee_kind='principal' AND grant_row.grantee={A})
                            OR (grant_row.grantee_kind='group' AND EXISTS(
                                SELECT 1 FROM group_closure grantee_member
                                WHERE grantee_member.tenant={source}.tenant AND grantee_member.group_id=grant_row.grantee
                                AND grantee_member.principal={A})))
                        AND {GQ}
                        ))"""

# A session label (docs/ACCESS_MODEL.md section 7) additionally needs the share's
# subject to contain the session audience: a group in {SG} or a principal in {SP}.
SESSION_GRANTEE = """((grant_row.grantee_kind='principal' AND grant_row.grantee=ANY({SP}::text[]))
                            OR (grant_row.grantee_kind='group' AND grant_row.grantee=ANY({SG}::text[])))"""

# The reader holds at least one active passage share (directly or through a group).
# Uncorrelated, so PostgreSQL evaluates it once per query: readers without shares
# never pay for the per-document check below.
RECEIVES_GRANTS = """EXISTS(SELECT 1 FROM raw_source_grants received WHERE received.tenant={T}
                    AND received.revoked IS NULL AND received.passages=1
                    AND ((received.grantee_kind='principal' AND received.grantee={A})
                        OR (received.grantee_kind='group' AND received.grantee IN (SELECT reader_group.group_id
                            FROM group_closure reader_group WHERE reader_group.tenant={T}
                            AND reader_group.principal={A}))))"""

# A passage document (alias ed) every source of which is shared with the reader.
# raw_granted is only a prefilter maintained by shares and passage indexing.
def _document_granted(grantee='true'):
    return """(ed.representation='raw' AND ed.raw_granted=1 AND ed.raw_owner IS DISTINCT FROM {A}
                    AND """ + RECEIVES_GRANTS + """
                    AND EXISTS(SELECT 1 FROM enterprise_dependencies granted_dep WHERE granted_dep.document_id=ed.id)
                    AND NOT EXISTS(SELECT 1 FROM enterprise_dependencies granted_dep
                        JOIN enterprise_sources granted ON granted.id=granted_dep.source_id
                        LEFT JOIN search_source_policies granted_mapping ON granted_mapping.source_id=granted.id
                        LEFT JOIN search_policies granted_policy ON granted_policy.id=granted_mapping.policy_id
                        WHERE granted_dep.document_id=ed.id AND NOT coalesce(""" + SOURCE_GRANTED.format(
                            source='granted', policy='granted_policy', T='{T}', A='{A}', GQ=grantee) + """,false)))"""


DOCUMENT_GRANTED = _document_granted()

# The session-label term of one policy row (recall, docs/ACCESS_MODEL.md section 7):
# the item's audience contains the session audience. Never origin_private; the
# workspace in S (the workspaces whose audience contains the session audience) when
# the item is not private, or {SSHARE} (shares whose subjects contain it); every
# carried workspace in S; a legacy 'team' origin needs the workspace and every
# carried one in SD (direct audiences). The owner gets no short cut: a person's own
# private items are recalled only in a private session, which has no session term.
# Mirrors search_session_admits (migration 049) and WorkspaceAccessMixin._session_admits.
SESSION = """(coalesce(policy.definition->>'origin_ceiling','')!='private'
                    AND ((coalesce((policy.definition->>'private')::boolean,false)=false
                            AND policy.definition->>'project'=ANY({S}::text[])) OR {SSHARE})
                    AND """ + CARRIED + """ <@ {S}::text[]
                    AND (coalesce(policy.definition->>'origin_ceiling','')!='team' OR (
                        policy.definition->>'project'=ANY({SD}::text[]) AND """ + CARRIED + """ <@ {SD}::text[])))"""


def _bind(template, ctx, access=None):
    """Replace {T}/{A}/{R}/{D} (and the session's {S}/{SD}/{SP}/{SG}) placeholders, in
    order, with bound parameters."""
    import re
    args = []
    values = {'T': ctx['tenant'], 'A': ctx['actor']}
    if access is not None:
        values.update(R=list(access[0]), D=list(access[1]))
    session = ctx.get('session_label') or {}
    values.update(S=list(session.get('workspaces', ())), SD=list(session.get('direct', ())),
                  SP=list(session.get('principals', ())), SG=list(session.get('groups', ())))
    def replace(match):
        args.append(values[match.group(1)])
        return '?'
    return re.sub(r'\{(SD|SP|SG|[TARDS])\}', replace, template), tuple(args)


def labelled(ctx):
    """The request carries a session label that restricts recall (not a private session)."""
    session = ctx.get('session_label')
    return bool(session) and not session.get('private')


def session_condition(ctx, share):
    """SESSION with `share` (an SQL template over {T}/{A}/{SP}/{SG}) as the share path."""
    return _bind(SESSION.replace('{SSHARE}', share), ctx)


def audience_condition(ctx, access, share=None):
    """AUDIENCE with `share` (an SQL condition, its arguments) standing in for the
    workspace term, or without one (the inherited path only)."""
    template = AUDIENCE.replace('{SHARE}', '<SHARE>')
    sql, args = _bind(template, ctx, access)
    if share is None:
        return sql.replace('<SHARE>', 'false'), args
    head, tail = sql.split('<SHARE>')
    count = head.count('?')
    return head + '(' + share[0] + ')' + tail, args[:count] + tuple(share[1]) + args[count:]


def source_granted_condition(ctx, source='src', policy='policy'):
    return _bind(SOURCE_GRANTED.format(source=source, policy=policy, T='{T}', A='{A}', GQ='true'), ctx)


def document_granted_condition(ctx):
    return _bind(DOCUMENT_GRANTED, ctx)


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


def policy_condition(ctx, access, *, raw=True):
    """One policy expression for the redacted passage tier of one source (alias `src`).

    The source owner, a reader in the item's audience, or a reader the source is
    explicitly shared with (agenthub.owner_controls), who still passes the
    carried-workspace and origin terms; every ordinary rule applies to all three.
    Retained originals never use this (agenthub.raw_access). `access` is (R, D).
    """
    common, common_args = _common(ctx)
    inherited, inherited_args = audience_condition(ctx, access)
    shared, shared_args = audience_condition(ctx, access, source_granted_condition(ctx))
    session, session_args = ('true', ())
    if labelled(ctx):
        session, session_args = session_condition(ctx, SOURCE_GRANTED.format(
            source='src', policy='policy', T='{T}', A='{A}', GQ=SESSION_GRANTEE))
    return ('(' + common + """
                    AND (""" + inherited + """ OR """ + shared + """)
                    AND """ + session + """
                )""", common_args + inherited_args + shared_args + session_args)


def predicate(ctx, access):
    """Indexed document authorization; mirrors EnterpriseStore._document_allowed.

    One correlated pass over the document's policies checks every ordinary source
    rule and the audience: every source a document depends on is the reader's
    own or has the reader in its audience; a passage may instead be shared with
    the reader through every source it depends on, which still obeys the
    carried-workspace and origin terms. `access` is (R, D).
    """
    granted = document_granted_condition(ctx)
    common, common_args = _common(ctx)
    inherited, inherited_args = audience_condition(ctx, access)
    shared, shared_args = audience_condition(ctx, access, granted)
    session, session_args = ('', ())
    if labelled(ctx):
        # Recall in a labelled session: also the session term (SESSION) on every policy.
        session, session_args = session_condition(ctx, "(ed.representation='raw' AND "
                                                  + _document_granted(SESSION_GRANTEE) + ')')
        session = ' AND ' + session
    # raw_shared (the passage's own source is not private) and raw_granted (an
    # active share covers it) are stable prefilters that keep other people's
    # private passages cheap to exclude. The policy pass below decides.
    return """AND (ed.representation!='raw' OR (? AND (ed.raw_owner=? OR ed.raw_shared>0 OR ed.raw_granted=1)))
        AND (SELECT bool_and(required.dependency_count>0 AND (""" + common + """
                AND (""" + inherited + """ OR (ed.representation='raw' AND """ + shared + """))""" + session + """) IS TRUE)
        FROM search_document_policies required
        LEFT JOIN search_policies policy ON policy.id=required.policy_id
        LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
        WHERE required.document_id=d.document_id)""", ('read' in ctx['actions'], ctx['actor'], *common_args,
                                                       *inherited_args, *shared_args, *session_args)


def validate_source_snapshots(store, db, ctx, expected):
    """Current raw authorization and exact source identity in bounded indexed batches.

    No positive authorization cache. Call again at delivery. Compare every frozen
    influence, not just selected citations. Bodies stay in PostgreSQL; only hashes
    cross the connection. Missing projection, source, or body denies the snapshot.
    """
    store.current_identity(db,ctx)
    # Redacted passage tier (owner, or shared at its level): `read` suffices. Retained
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
    """Bounded redacted source context (owner or shared at its level) through the indexed raw policy oracle."""
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
