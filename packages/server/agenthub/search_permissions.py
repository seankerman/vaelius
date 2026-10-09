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


def policy_condition(ctx, *, raw=False):
    """One policy expression for indexed document and raw-source authorization."""
    delegated = ctx.get('delegated_projects')
    delegated = sorted(delegated) if delegated is not None else None
    now = time.time()
    sql = """(
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
                    AND (policy.definition->>'visibility'='organization'
                        OR (policy.definition->>'visibility'='private' AND policy.definition->>'owner'=?)
                        OR (policy.definition->>'visibility'='team' AND EXISTS(
                            SELECT 1 FROM enterprise_memberships membership
                            JOIN enterprise_projects project ON project.tenant=membership.tenant AND project.id=membership.project
                            WHERE membership.tenant=? AND membership.project=policy.definition->>'project'
                                AND membership.principal=? AND membership.active=1 AND project.active=1)))
                )"""
    args = (ctx['tenant'], delegated, delegated, now, ctx['tenant'], now,
            ctx['actor'], ctx['actor'], ctx['actor'], ctx['tenant'], ctx['actor'])
    if raw:
        # Raw originals are owner-only, including organization/team sources.
        # Document visibility alone never grants raw-source access.
        sql = sql[:sql.index("AND (policy.definition->>'visibility'")] + "AND policy.definition->>'owner'=?)"
        args = args[:8] + (ctx['actor'],)
    return sql, args


def predicate(ctx):
    condition,args=policy_condition(ctx)
    return """AND (ed.representation!='raw' OR (ed.raw_owner=? AND ?))
        AND (SELECT bool_and(required.dependency_count>0 AND """+condition+""" IS TRUE)
        FROM search_document_policies required
        LEFT JOIN search_policies policy ON policy.id=required.policy_id
        LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
        WHERE required.document_id=d.document_id)""",(ctx['actor'],'source_read' in ctx['actions'],*args)


def validate_source_snapshots(store, db, ctx, expected):
    """Current raw authorization and exact source identity in bounded indexed batches.

    No positive authorization cache. Call again at delivery. Compare every frozen
    influence, not just selected citations. Bodies stay in PostgreSQL; only hashes
    cross the connection. Missing projection, source, or body denies the snapshot.
    """
    store.current_identity(db,ctx)
    store._need(ctx,'read');store._need(ctx,'source_read')
    condition,args=policy_condition(ctx,raw=True)
    ids=sorted(expected)
    for offset in range(0,len(ids),1000):
        batch=ids[offset:offset+1000]
        # Materialize only this requested batch. Without the fence PostgreSQL
        # can start with every allowed policy mapping and perform tens of
        # thousands of source lookups before testing src.id against the batch.
        rows=db.execute("""WITH requested_sources AS MATERIALIZED (
                SELECT id,source_version FROM enterprise_sources WHERE id=ANY(?::text[])
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
    """Bounded original-source read through the same indexed raw policy oracle."""
    store.current_identity(db,ctx);store._need(ctx,'read');store._need(ctx,'source_read')
    ids=sorted(set(ids))
    if len(ids)>1000:raise ValueError('context_source_batch_bound')
    if not ids:return []
    condition,args=policy_condition(ctx,raw=True)
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
