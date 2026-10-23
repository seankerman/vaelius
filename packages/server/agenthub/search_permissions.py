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


# The CURRENT level of one policy row (alias `policy`): the level stored at
# ingest (min of the person setting, scope-file request, project and tenant
# ceilings and connection visibility then), capped again by today's person
# setting (`author`), project ceiling (or tenant default), tenant ceiling,
# connection visibility, origin ceiling, and the current ceiling of every other
# project whose content the source carries (`origin_projects`; an unknown or
# inactive one counts as private). 0 private, 1 team, 2 organization. Absent
# origin keys change nothing (agenthub.project_sharing, agentclient.origin).
# The origin terms of a policy row (agentclient.origin, migration 046): the stored
# origin ceiling and the current ceiling of every other project the source carries
# (an unknown or inactive one counts as private). Needs aliases `project` and
# `organization` for the source project.
ORIGIN_TERMS = """coalesce(array_position(ARRAY['team','organization'],
                            coalesce(policy.definition->>'origin_ceiling','organization')),0),
                        coalesce((SELECT min(CASE WHEN other.id IS NULL THEN 0 ELSE coalesce(array_position(
                                ARRAY['team','organization'],coalesce(other.max_sharing,organization.default_max_sharing)),0) END)
                            FROM jsonb_array_elements_text(coalesce(policy.definition->'origin_projects','[]'::jsonb)) named(id)
                            LEFT JOIN enterprise_projects other ON other.tenant=project.tenant AND other.id=named.id
                                AND other.active=1),2)"""
LEVEL = """least(
                        coalesce(array_position(ARRAY['team','organization'],policy.definition->>'sharing'),0),
                        coalesce(array_position(ARRAY['team','organization'],policy.definition->>'visibility'),0),
                        coalesce(array_position(ARRAY['team','organization'],author.max_sharing),0),
                        coalesce(array_position(ARRAY['team','organization'],
                            coalesce(project.max_sharing,organization.default_max_sharing)),0),
                        coalesce(array_position(ARRAY['team','organization'],organization.max_sharing),0),
                        """ + ORIGIN_TERMS + """)"""
# A member of the source project must also be an active member of every other
# project whose content the source carries (`origin_projects`).
ORIGIN_MEMBERS = """NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(
                                coalesce(policy.definition->'origin_projects','[]'::jsonb)) named(id)
                            WHERE NOT EXISTS(SELECT 1 FROM enterprise_memberships carried
                                WHERE carried.tenant=membership.tenant AND carried.project=named.id
                                AND carried.principal=membership.principal AND carried.active=1))"""
# Every document, curated and derived ones included, reaches a reader other than
# the source's owner only as far as the source's origin allows: organization
# changes nothing, team needs an active member of the source project and of every
# other project it carries, private reaches nobody else. Sources without an origin
# take the first branch, so their authorization is exactly as before. Mirrored by
# ProjectSharingMixin._origin_reaches and search_origin_reaches (migration 046).
ORIGIN_REACHES = """((policy.definition->>'origin_ceiling' IS NULL AND policy.definition->'origin_projects' IS NULL)
                        OR EXISTS(SELECT 1 FROM enterprise_projects project
                            JOIN enterprise_organizations organization ON organization.id=project.tenant
                            WHERE project.tenant=policy.definition->>'tenant' AND project.id=policy.definition->>'project'
                            AND project.active=1 AND CASE least(""" + ORIGIN_TERMS + """)
                            WHEN 2 THEN true
                            WHEN 1 THEN EXISTS(SELECT 1 FROM enterprise_memberships membership
                                WHERE membership.tenant=project.tenant AND membership.project=project.id
                                AND membership.principal=? AND membership.active=1 AND """ + ORIGIN_MEMBERS + """)
                            ELSE false END))"""
CEILINGS = """SELECT 1 FROM enterprise_projects project
                    JOIN enterprise_organizations organization ON organization.id=project.tenant
                    JOIN enterprise_principals author ON author.tenant=project.tenant
                        AND author.id=policy.definition->>'owner'
                    WHERE project.tenant=? AND project.id=policy.definition->>'project' AND project.active=1"""

# One policy row shares its redacted passages with a reader who is not the owner:
# the current level reaches them. team = an active member of the source project;
# organization = also any active principal of the tenant that is not under an
# operator quarantine. Mirrored by ProjectSharingMixin._passage_shared and by
# search_passage_shared, called from search_document_allowed (migration 045). Lowering any ceiling hides passages
# on the next request; raising one never exposes a source stored lower.
SHARE = """(policy.definition->>'sharing' IN ('team','organization') AND EXISTS(
                    """ + CEILINGS + """
                    AND CASE """ + LEVEL + """
                    WHEN 2 THEN EXISTS(SELECT 1 FROM enterprise_principals reader
                            WHERE reader.tenant=? AND reader.id=? AND reader.active=1
                            AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                                WHERE held.tenant=reader.tenant AND held.principal=reader.id))
                        OR EXISTS(SELECT 1 FROM enterprise_memberships membership
                            WHERE membership.tenant=? AND membership.project=project.id
                            AND membership.principal=? AND membership.active=1 AND """ + ORIGIN_MEMBERS + """)
                    WHEN 1 THEN EXISTS(SELECT 1 FROM enterprise_memberships membership
                            WHERE membership.tenant=? AND membership.project=project.id
                            AND membership.principal=? AND membership.active=1 AND """ + ORIGIN_MEMBERS + """)
                    ELSE false END))"""


def share_condition(ctx):
    tenant, actor = ctx['tenant'], ctx['actor']
    return SHARE, (tenant, tenant, actor, tenant, actor, tenant, actor)



# One source (alias {source}, its policy row {policy}) is granted to the reader by
# an explicit owner grant (agenthub.owner_controls): an active grant of the
# passage tier from the source's owner, for the source, its capture session or
# its connection, to the reader or a project the reader is an active member of.
# Never while the operator restricts owner sharing (owner-issued grants), in an
# inactive project, or to a quarantined reader. A grant stands in for visibility
# and the sharing level only: the origin (ORIGIN_REACHES) and every other ordinary
# rule still apply. Mirrors search_passage_granted (migration 047) and
# ProjectSharingMixin._passage_granted.
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
                            OR (grant_row.grantee_kind='project' AND EXISTS(
                                SELECT 1 FROM enterprise_memberships grantee_member
                                JOIN enterprise_projects grantee_project ON grantee_project.tenant=grantee_member.tenant
                                    AND grantee_project.id=grantee_member.project AND grantee_project.active=1
                                WHERE grantee_member.tenant={source}.tenant AND grantee_member.project=grant_row.grantee
                                AND grantee_member.principal={A} AND grantee_member.active=1)))
                        ))"""

# A passage document (alias ed) every source of which is granted to the reader.
# raw_granted is only a prefilter maintained by grants and passage indexing.
DOCUMENT_GRANTED = """(ed.representation='raw' AND ed.raw_granted=1 AND ed.raw_owner IS DISTINCT FROM {A}
                    AND EXISTS(SELECT 1 FROM enterprise_dependencies granted_dep WHERE granted_dep.document_id=ed.id)
                    AND NOT EXISTS(SELECT 1 FROM enterprise_dependencies granted_dep
                        JOIN enterprise_sources granted ON granted.id=granted_dep.source_id
                        LEFT JOIN search_source_policies granted_mapping ON granted_mapping.source_id=granted.id
                        LEFT JOIN search_policies granted_policy ON granted_policy.id=granted_mapping.policy_id
                        WHERE granted_dep.document_id=ed.id AND NOT coalesce(""" + SOURCE_GRANTED.format(
                            source='granted', policy='granted_policy', T='{T}', A='{A}') + """,false)))"""


def _bind(template, ctx):
    """Replace {T}/{A} placeholders, in order, with bound tenant/actor parameters."""
    import re
    args = []
    def replace(match):
        args.append(ctx['tenant'] if match.group(1) == 'T' else ctx['actor'])
        return '?'
    return re.sub(r'\{([TA])\}', replace, template), tuple(args)


def source_granted_condition(ctx, source='src', policy='policy'):
    return _bind(SOURCE_GRANTED.format(source=source, policy=policy, T='{T}', A='{A}'), ctx)


def document_granted_condition(ctx):
    return _bind(DOCUMENT_GRANTED, ctx)


def policy_condition(ctx, *, raw=False, granted=None):
    """One policy expression for indexed document and raw-source authorization.

    `raw=True` is the redacted passage tier (passages, cited evidence and
    conversation context) for one source (alias `src`): the source owner, a reader
    the current level reaches who also passes every ordinary rule below, or a
    reader the source is explicitly granted to (agenthub.owner_controls), who
    passes every ordinary rule except visibility. Retained originals never use
    this; they need source_read and ownership, their own level or a grant
    (agenthub.raw_access).

    `granted` (document level, raw=False) is an SQL condition, with its
    arguments, that stands in for the visibility rule.
    """
    delegated = ctx.get('delegated_projects')
    delegated = sorted(delegated) if delegated is not None else None
    now = time.time()
    common = """
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
                            WHERE reader.value=?))))"""
    common_args = (ctx['tenant'], delegated, delegated, now, ctx['tenant'], now, ctx['actor'], ctx['actor'])
    visibility = """(policy.definition->>'visibility'='organization'
                        OR (policy.definition->>'visibility'='private' AND policy.definition->>'owner'=?)
                        OR (policy.definition->>'visibility'='team' AND EXISTS(
                            SELECT 1 FROM enterprise_memberships membership
                            JOIN enterprise_projects project ON project.tenant=membership.tenant AND project.id=membership.project
                            WHERE membership.tenant=? AND membership.project=policy.definition->>'project'
                                AND membership.principal=? AND membership.active=1 AND project.active=1)))"""
    visibility_args = (ctx['actor'], ctx['tenant'], ctx['actor'])
    # Rules visibility never replaces: the operator quarantine, and the origin
    # (migration 046), which reaches a non-owner only as far as it allows.
    quarantine = """(coalesce((policy.definition->>'quarantined')::boolean,false)=false
                        OR policy.definition->>'owner'=?)
                    AND (policy.definition->>'owner'=? OR """ + ORIGIN_REACHES + """)"""
    quarantine_args = (ctx['actor'], ctx['actor'], ctx['actor'])
    if not raw:
        if granted is not None:
            visibility, visibility_args = ('(' + visibility + ' OR ' + granted[0] + ')',
                                           visibility_args + tuple(granted[1]))
        return '(' + common + """
                    AND """ + visibility + """
                    AND """ + quarantine + """
                )""", common_args + visibility_args + quarantine_args
    share, share_args = share_condition(ctx)
    source_granted, source_granted_args = source_granted_condition(ctx)
    return ('(' + common + """
                    AND (policy.definition->>'owner'=? OR (""" + visibility + """
                    AND """ + quarantine + """
                    AND """ + share + """) OR (""" + quarantine + """ AND """ + source_granted + """))
                )""", common_args + (ctx['actor'],) + visibility_args + quarantine_args + share_args
            + quarantine_args + source_granted_args)


def predicate(ctx):
    """Indexed document authorization; mirrors EnterpriseStore._document_allowed.

    One correlated pass over the document's policies checks every ordinary source
    rule and the sharing rule: a passage is the owner's, shared with the reader
    at its current level, or explicitly granted (which stands in for visibility);
    a derived document registered after migration 047 (level_capped) needs every
    dependency to be the reader's own or shared with the reader at its level.
    """
    granted = document_granted_condition(ctx)
    condition,args=policy_condition(ctx,granted=granted)
    share,share_args=share_condition(ctx)
    # raw_shared is a stable prefilter: the level stored for the passage's own
    # source (0 private, 1 team, 2 organization); raw_granted marks passages an
    # active grant covers. They keep other people's private passages as cheap to
    # exclude as before. The policy pass below still decides.
    return """AND (ed.representation!='raw' OR (? AND (ed.raw_owner=? OR ed.raw_shared>0 OR ed.raw_granted=1)))
        AND (SELECT bool_and(required.dependency_count>0 AND """+condition+""" IS TRUE
            AND ((CASE WHEN ed.representation='raw' THEN ed.raw_owner=?
                       WHEN ed.level_capped=0 THEN true
                       ELSE policy.definition->>'owner'=? END) IS TRUE
                 OR """+share+""" IS TRUE OR """+granted[0]+"""))
        FROM search_document_policies required
        LEFT JOIN search_policies policy ON policy.id=required.policy_id
        LEFT JOIN backend_connections connection ON connection.id=policy.definition->>'connection'
        WHERE required.document_id=d.document_id)""",('read' in ctx['actions'],ctx['actor'],*args,ctx['actor'],
                                                       ctx['actor'],*share_args,*granted[1])


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
    condition,args=policy_condition(ctx,raw=True)
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
