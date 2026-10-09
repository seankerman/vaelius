"""Owner-operated draft evaluation, within a rolled-back PostgreSQL transaction.

No HTTP/MCP argument enables this capability. It is bound to a Python store and
an exact authenticated identity, document/revision and source manifest. Ordinary
clients continue to deny building generations. Only derivative registration and
index rows are staged, and all of them roll back on exit.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from agenthub.enterprise import Denied

_REVIEW=ContextVar('agentnetwork_operator_draft_review',default=None)


def _identity(ctx):
    return tuple(ctx.get(k) for k in ('tenant','principal','actor','enrollment','credential_digest'))


def selected_documents(store,ctx=None):
    review=_REVIEW.get()
    if not review or review['store'] is not store:return None
    if ctx is not None and _identity(ctx)!=review['identity']:return []
    return list(review['documents'])


def allowed_revision(store,ctx,document,revision):
    review=_REVIEW.get()
    return bool(review and review['store'] is store and _identity(ctx)==review['identity'] and
        review['documents'].get(document)==revision)


def borrowed_state(store):
    review=_REVIEW.get()
    return review['state'] if review and review['store'] is store else None


class _Rollback(Exception):pass


@contextmanager
def review_session(store,ctx,documents,*,source_ids,source_digests=None,strict=True):
    if _REVIEW.get() is not None or not getattr(store,'dsn',None):raise ValueError('isolated_postgres_review_required')
    if not isinstance(documents,dict) or not 1<=len(documents)<=1000 or not source_ids:
        raise ValueError('bounded_review_manifest_required')
    sources=set(source_ids)
    if len(sources)>100000:raise ValueError('review_source_bound')
    with store.delivery_lock(),store.open() as state:
        store.current_identity(state.db,ctx);store._need(ctx,'read');store._need(ctx,'source_read')
        token=_REVIEW.set({'store':store,'identity':_identity(ctx),'state':state,'documents':dict(documents)})
        try:
            try:
                with state.db:
                    # Canonical batch registration and authorization; never
                    # rerun the per-document source-policy N+1 bottleneck.
                    store.refresh_documents(db=state.db)
                    allowed=set(store._authorized_documents(state,ctx))
                    rows=state.db.execute('SELECT document_id,active_revision_id,lifecycle FROM knowledge_documents WHERE document_id=ANY(?::text[])',(list(documents),)).fetchall()
                    revisions={r['document_id']:r['active_revision_id'] for r in rows if r['lifecycle']=='active'}
                    dependencies=state.db.execute("""SELECT d.document_id,s.id,r.digest source_digest
                        FROM enterprise_dependencies d JOIN enterprise_sources s ON s.id=d.source_id
                        LEFT JOIN backend_source_revisions r ON r.source_id=s.id
                        WHERE d.document_id=ANY(?::text[])""",(list(documents),)).fetchall()
                    denied=set(documents)-allowed
                    for ident,revision in documents.items():
                        if revisions.get(ident)!=revision:denied.add(ident)
                    for v in dependencies:
                        if (v['id'] not in sources or
                            (source_digests is not None and v['source_digest']!=source_digests.get(v['id']))):
                            denied.add(v['document_id'])
                    if denied and strict:raise Denied()
                    _REVIEW.get()['documents']={k:v for k,v in documents.items() if k not in denied}
                    _REVIEW.get()['denied_documents']=sorted(denied)
                    yield state
                    raise _Rollback()
            except _Rollback:pass
        finally:_REVIEW.reset(token)
