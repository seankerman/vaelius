"""Restricted PostgreSQL role for authenticated search transactions.

The service is the trusted identity broker. Clients never receive SQL credentials
or control session settings. Workers keep their independent write authority.
"""
import hashlib
import json
from contextlib import contextmanager

PROTECTED = ('enterprise_documents', 'knowledge_documents', 'knowledge_revisions', 'knowledge_fts', 'cloud_document_vectors')
# Metadata needed by ranking only. No raw sources, credentials or observer text.
METADATA = ('cloud_vector_state',)


def reader_role(database, schema, worker):
    return 'agenthub_reader_' + hashlib.sha256((database+'\0'+schema+'\0'+worker).encode()).hexdigest()[:24]


def provision(db, worker, schema):
    """Operator-only, idempotent role grants; no new login or credential."""
    from psycopg import sql
    database = db.execute('SELECT current_database()').fetchone()[0]
    reader = reader_role(database, schema, worker)
    if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (reader,)).fetchone():
        db.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT').format(sql.Identifier(reader)))
    flags=db.execute('SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=%s',(reader,)).fetchone()
    if any(flags.values()) or db.execute('SELECT pg_has_role(%s,%s,\'MEMBER\')',(reader,worker)).fetchone()[0]:
        raise ValueError('search_reader_role_has_unsafe_privileges')
    # The reader never inherits the worker. The worker may inherit its strictly
    # smaller privileges; the reverse grant would defeat row-level security.
    role = db.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s', (worker,)).fetchone()
    if any(role.values()):
        raise ValueError('search_worker_role_must_be_unprivileged')
    db.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(reader), sql.Identifier(worker)))
    db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(reader)))
    for table in PROTECTED + METADATA:
        db.execute(sql.SQL('GRANT SELECT ON {}.{} TO {}').format(sql.Identifier(schema), sql.Identifier(table), sql.Identifier(reader)))
    db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.search_document_allowed(text) TO {}').format(sql.Identifier(schema), sql.Identifier(reader)))
    db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.search_revision_allowed(text,text) TO {}').format(sql.Identifier(schema), sql.Identifier(reader)))
    db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.search_lexical_matches(text) TO {}').format(sql.Identifier(schema), sql.Identifier(reader)))
    for table in PROTECTED:
        relation = sql.SQL('{}.{}').format(sql.Identifier(schema), sql.Identifier(table))
        db.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(relation))
        column='id' if table=='enterprise_documents' else 'document_id'
        check=('search_revision_allowed(document_id,revision_id)' if table in
            ('knowledge_revisions','knowledge_fts','cloud_document_vectors') else 'search_document_allowed('+column+')')
        for suffix, role_name, predicate in [('worker', worker, 'true'), ('reader', reader, check)]:
            policy = sql.Identifier(reader+'_'+suffix)
            db.execute(sql.SQL('DROP POLICY IF EXISTS {} ON {}').format(policy, relation))
            db.execute(sql.SQL('CREATE POLICY {} ON {} TO {} USING ({})').format(
                policy, relation, sql.Identifier(role_name), sql.SQL(predicate)))


@contextmanager
def restrict(store, state, ctx, project=None):
    """Switch one existing transaction; RESET/rollback cannot leak user context."""
    from psycopg import sql
    from agenthub.draft_review import selected_documents
    store.current_identity(state.db, ctx)
    store._need(ctx, 'read')
    scopes = store._readable_scopes(state.db, ctx, project)
    releases = [r[0] for r in state.db.execute('SELECT document_id FROM backend_releases WHERE tenant=?', (ctx['tenant'],))]
    releases = [doc for doc in releases if store._document_allowed(state.db, ctx, doc)]
    context = dict(tenant=ctx['tenant'], actor=ctx['actor'], scopes=scopes, project=project,
        source_read='source_read' in ctx['actions'],
        delegated=sorted(ctx['delegated_projects']) if ctx.get('delegated_projects') is not None else None,
        review=selected_documents(store, ctx), releases=releases)
    connection = state.db.connection
    database, schema, worker = connection.execute('SELECT current_database(),current_schema(),current_user').fetchone().values()
    role = reader_role(database, schema, worker)
    connection.execute("SELECT set_config('agenthub.search_context',%s,true)", (json.dumps(context),))
    connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    try:
        yield state
    finally:
        # On a failed SQL transaction PostgreSQL rejects RESET; rollback clears
        # both role and context. Successful reads restore a borrowed worker state.
        from psycopg.pq import TransactionStatus
        if connection.info.transaction_status == TransactionStatus.INERROR:
            connection.rollback()
        else:
            connection.execute('RESET ROLE')
            connection.execute("SELECT set_config('agenthub.search_context','',true)")
