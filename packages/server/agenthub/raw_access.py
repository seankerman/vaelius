"""Least-privilege reads of retained originals.

Two tiers, both rechecked at delivery:

* Redacted passages, search results, cited evidence and conversation context are
  read with ``read``. Raw-representation passages stay owner-only unless the
  source's sharing level reaches the reader (agenthub.project_sharing).
* Retained originals (the complete canonical source payload or object, the v1
  source body and original document bytes) additionally need the credential's
  ``source_read`` action AND one of: source ownership; the original's own
  sharing level (``originals_sharing``, default private, never above the
  passage level) reaching the reader under every current ceiling; or an
  explicit, active, owner-issued grant to the reading principal or to a project
  it belongs to. Sharing passages never shares originals.

Grants never bypass tenant, lifecycle, delegation or connection-reader checks.
``source_read`` is not part of default enrollment or default OAuth scope
mapping; it must be granted explicitly. Existing credentials keep their actions.
"""
import json
import re
import time

GRANTEE_KINDS = ('principal', 'project')
SCOPE_KINDS = ('source', 'connection')


def original_allowed(store, db, ctx, source):
    """True when ctx may read this retained original right now."""
    if not source or source['tenant'] != ctx['tenant'] or not source['active']:
        return False
    if (ctx.get('delegated_projects') is not None and
            source['external_project'] not in ctx['delegated_projects']):
        return False
    if not store._general_source_allowed(db, ctx, source):
        return False
    if 'source_read' not in ctx['actions']:
        return False
    if source['owner'] == ctx['actor']:
        return True
    if store._originals_shared(db, ctx, source):
        return True
    return _granted(store, db, ctx, source)


def _granted(store, db, ctx, source):
    rows = db.execute('''SELECT grantee_kind,grantee FROM raw_source_grants
        WHERE tenant=? AND revoked IS NULL AND granted_by=?
        AND ((scope_kind='source' AND scope_id=?) OR (scope_kind='connection' AND scope_id IN
            (SELECT connection FROM backend_source_revisions WHERE source_id=?)))''',
        (ctx['tenant'], source['owner'], source['id'], source['id'])).fetchall()
    for row in rows:
        if row['grantee_kind'] == 'principal' and row['grantee'] == ctx['actor']:
            return True
        if row['grantee_kind'] == 'project' and store._project_member(db, ctx, row['grantee']):
            return True
    return False


def _validate(scope_kind, scope_id, grantee_kind, grantee):
    if scope_kind not in SCOPE_KINDS or grantee_kind not in GRANTEE_KINDS:
        raise ValueError('invalid_raw_grant')
    for value in (scope_id, grantee):
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError('invalid_raw_grant')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', grantee):
        raise ValueError('invalid_raw_grantee')


def _owned_scope(db, ctx, scope_kind, scope_id):
    if scope_kind == 'source':
        row = db.execute('SELECT tenant,owner,active FROM enterprise_sources WHERE id=?', (scope_id,)).fetchone()
        return bool(row and row['tenant'] == ctx['tenant'] and row['owner'] == ctx['actor'] and row['active'])
    row = db.execute('SELECT tenant,owner,active FROM backend_connections WHERE id=?', (scope_id,)).fetchone()
    return bool(row and row['tenant'] == ctx['tenant'] and row['owner'] == ctx['actor'] and row['active'])


def grant(store, ctx, scope_kind, scope_id, grantee_kind, grantee):
    """Owner-issued grant. Requires the policy action; audited."""
    from agenthub.enterprise import Denied
    store._need(ctx, 'policy')
    _validate(scope_kind, scope_id, grantee_kind, grantee)
    with store.delivery_lock(), store.open() as state, state.db:
        db = state.db
        if not _owned_scope(db, ctx, scope_kind, scope_id):
            raise Denied()
        if grantee_kind == 'principal':
            known = db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=? AND active=1',
                (ctx['tenant'], grantee)).fetchone()
        else:
            known = db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=? AND id=? AND active=1',
                (ctx['tenant'], grantee)).fetchone()
        if not known:
            raise ValueError('unknown_raw_grantee')
        db.execute('''INSERT INTO raw_source_grants
            (tenant,scope_kind,scope_id,grantee_kind,grantee,granted_by,created,revoked)
            VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(tenant,scope_kind,scope_id,grantee_kind,grantee)
            DO UPDATE SET granted_by=excluded.granted_by,created=excluded.created,revoked=NULL''',
            (ctx['tenant'], scope_kind, scope_id, grantee_kind, grantee, ctx['actor'], time.time()))
        store._audit(db, ctx, 'raw_grant', 'granted', json.dumps([scope_kind, scope_id, grantee_kind, grantee]))
    return {'scope_kind': scope_kind, 'scope_id': scope_id, 'grantee_kind': grantee_kind,
            'grantee': grantee, 'active': True}


def revoke(store, ctx, scope_kind, scope_id, grantee_kind, grantee):
    store._need(ctx, 'policy')
    _validate(scope_kind, scope_id, grantee_kind, grantee)
    with store.delivery_lock(), store.open() as state, state.db:
        changed = state.db.execute('''UPDATE raw_source_grants SET revoked=? WHERE tenant=? AND scope_kind=?
            AND scope_id=? AND grantee_kind=? AND grantee=? AND granted_by=? AND revoked IS NULL''',
            (time.time(), ctx['tenant'], scope_kind, scope_id, grantee_kind, grantee, ctx['actor'])).rowcount
        store._audit(state.db, ctx, 'raw_grant', 'revoked' if changed else 'absent',
            json.dumps([scope_kind, scope_id, grantee_kind, grantee]))
    return {'scope_kind': scope_kind, 'scope_id': scope_id, 'grantee_kind': grantee_kind,
            'grantee': grantee, 'active': False, 'changed': bool(changed)}


def grants(store, ctx):
    """Active grants issued by the caller."""
    store._need(ctx, 'policy')
    with store.open() as state:
        rows = state.db.execute('''SELECT scope_kind,scope_id,grantee_kind,grantee,created FROM raw_source_grants
            WHERE tenant=? AND granted_by=? AND revoked IS NULL ORDER BY scope_kind,scope_id,grantee_kind,grantee''',
            (ctx['tenant'], ctx['actor'])).fetchall()
    return [dict(row) for row in rows]


def source_read_report(store):
    """Operator inventory: active credentials still holding source_read, per principal.

    Counts only; never returns token digests. Use before revoking/regranting.
    """
    with store.open() as state:
        rows = state.db.execute('''SELECT principal,acting_for,actions FROM enterprise_credentials
            WHERE tenant=? AND active=1''', (store.tenant_id,)).fetchall()
        active_grants = state.db.execute('SELECT count(*) FROM raw_source_grants WHERE tenant=? AND revoked IS NULL',
            (store.tenant_id,)).fetchone()[0]
    holders = {}
    for row in rows:
        if 'source_read' in json.loads(row['actions']):
            name = row['acting_for'] and row['principal'] + '->' + row['acting_for'] or row['principal']
            holders[name] = holders.get(name, 0) + 1
    return {'tenant': store.tenant_id, 'credentials_with_source_read': sum(holders.values()),
            'by_principal': dict(sorted(holders.items())), 'active_raw_grants': active_grants}


def strip_source_read(store, principal, *, apply=False):
    """Remove source_read from one principal's active credentials (dry run by default).

    Credentials stay valid for every other action; no source or knowledge changes.
    Restore by enrolling a new credential that explicitly lists source_read.
    """
    if not isinstance(principal, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', principal):
        raise ValueError('invalid_principal')
    with store.delivery_lock(), store.open() as state, state.db:
        rows = state.db.execute('''SELECT digest,actions FROM enterprise_credentials
            WHERE tenant=? AND principal=? AND active=1''', (store.tenant_id, principal)).fetchall()
        selected = [row for row in rows if 'source_read' in json.loads(row['actions'])]
        if apply:
            for row in selected:
                actions = sorted(set(json.loads(row['actions'])) - {'source_read'})
                if not actions:
                    state.db.execute('UPDATE enterprise_credentials SET active=0 WHERE digest=?', (row['digest'],))
                else:
                    state.db.execute('UPDATE enterprise_credentials SET actions=? WHERE digest=?',
                        (json.dumps(actions), row['digest']))
            store._audit(state.db, {'tenant': store.tenant_id, 'actor': 'local_operator'},
                'source_read_strip', 'applied', principal)
    return {'principal': principal, 'credentials': len(selected), 'applied': bool(apply)}


def main(argv=None):
    """Explicit local operator commands; never prints tokens, sources or values."""
    import argparse
    from pathlib import Path
    from agenthub.cloud_maintenance import _operator
    from agenthub.cloud_local import runtime, receipt
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=['report', 'strip-source-read', 'grant', 'revoke', 'list'])
    parser.add_argument('--profile', required=True); parser.add_argument('--tenant', required=True)
    parser.add_argument('--principal'); parser.add_argument('--apply', action='store_true')
    parser.add_argument('--token-file', help='owner credential for grant/revoke/list')
    parser.add_argument('--scope-kind', choices=SCOPE_KINDS); parser.add_argument('--scope-id')
    parser.add_argument('--grantee-kind', choices=GRANTEE_KINDS); parser.add_argument('--grantee')
    args = parser.parse_args(argv)
    profile, _, _ = _operator(args.profile, args.tenant)
    _, registry = runtime(profile); store = registry.resolve(args.tenant)
    if args.command == 'report':
        result = source_read_report(store)
    elif args.command == 'strip-source-read':
        if not args.principal:
            parser.error('--principal required')
        result = strip_source_read(store, args.principal, apply=args.apply)
    else:
        if not args.token_file:
            parser.error('--token-file owner credential required')
        token = Path(args.token_file).expanduser().resolve()
        if not token.is_file() or token.stat().st_mode & 0o077:
            raise ValueError('owner_token_permissions')
        ctx = store.authenticate(token.read_text().strip())
        if args.command == 'list':
            result = {'grants': grants(store, ctx)}
        else:
            if not all((args.scope_kind, args.scope_id, args.grantee_kind, args.grantee)):
                parser.error('--scope-kind, --scope-id, --grantee-kind and --grantee required')
            action = grant if args.command == 'grant' else revoke
            result = action(store, ctx, args.scope_kind, args.scope_id, args.grantee_kind, args.grantee)
    print(json.dumps(receipt(profile, 'raw-access-' + args.command, result), sort_keys=True))


if __name__ == '__main__':
    main()
