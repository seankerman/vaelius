"""Operator scan of retained content with the shared credential detector.

Reports counts by location, rule and owning principal. It never prints, logs or
returns a matched value, a source ID or any stored text. Scanning is read-only.

``--rewrite-redacted`` plans (default, dry run) or, with ``--apply``, writes a
redacted replacement revision for typed captured sources through the normal
``ingest_general`` revision path, authorized by the owner's own credential. The
superseded revision is retired by that path, not deleted: purging it remains an
explicit lifecycle ``delete`` decision. Original documents and v1 sources are
immutable here and are reported for lifecycle review instead.

Usage::

    python -m agenthub.secret_scan --profile PROFILE --tenant TENANT [--objects]
    python -m agenthub.secret_scan --profile PROFILE --tenant TENANT \\
        --rewrite-redacted --token-file OWNER_TOKEN [--apply] [--limit 100]
"""
import json

from agentclient.secret_detection import RULE_IDS, findings
from agenthub.intake_redaction import redact_capture

BATCH = 500


def _rules(text):
    return sorted({item.rule for item in findings(text)}) if text else []


def _tally(report, location, rules, owner=None):
    entry = report['locations'].setdefault(location, {'rows_scanned': 0, 'rows_with_findings': 0, 'rules': {}})
    entry['rows_scanned'] += 1
    if not rules:
        return
    entry['rows_with_findings'] += 1
    for rule in rules:
        entry['rules'][rule] = entry['rules'].get(rule, 0) + 1
        report['rules'][rule] = report['rules'].get(rule, 0) + 1
    if owner is not None:
        report['by_owner'][owner] = report['by_owner'].get(owner, 0) + 1


def _batches(db, sql, args=()):
    after = ''
    while True:
        rows = db.execute(sql, (*args, after, BATCH)).fetchall()
        if not rows:
            return
        yield rows
        after = rows[-1]['k']


def scan(store, *, objects=None):
    """Read-only counts over every retained content location in this tenant."""
    tenant = store.tenant_id
    report = {'tenant': tenant, 'detector_rules': list(RULE_IDS), 'locations': {}, 'rules': {},
              'by_owner': {}, 'values_reported': False, 'model_calls': 0}
    with store.open() as state:
        db = state.db
        # Canonical typed source payloads (the authoritative original projection).
        for rows in _batches(db, '''SELECT r.source_id k,r.payload,s.owner FROM backend_source_revisions r
                JOIN enterprise_sources s ON s.id=r.source_id WHERE s.tenant=? AND r.source_id>?
                ORDER BY r.source_id LIMIT ?''', (tenant,)):
            for row in rows:
                _tally(report, 'source_payloads', _rules(row['payload']), row['owner'])
        # Source bodies, passages and derived observations in the memories table.
        for rows in _batches(db, '''SELECT m.id k,m.body,s.owner FROM memories m
                LEFT JOIN enterprise_sources s ON s.id=m.id AND s.tenant=? WHERE m.id>?
                ORDER BY m.id LIMIT ?''', (tenant,)):
            for row in rows:
                location = 'source_bodies' if row['owner'] is not None else 'passages_and_derived'
                _tally(report, location, _rules(row['body']), row['owner'])
        for rows in _batches(db, '''SELECT revision_id k,claim_json FROM knowledge_revisions
                WHERE revision_id>? ORDER BY revision_id LIMIT ?'''):
            for row in rows:
                _tally(report, 'knowledge_revisions', _rules(row['claim_json']))
        # Multi-part transport staging held until reassembly completes.
        for row in db.execute('SELECT data FROM backend_parts').fetchall():
            _tally(report, 'staged_transport_parts', _rules(bytes(row['data']).decode('utf-8', 'ignore')))
        for table in ('backend_jobs', 'outbox'):
            for row in db.execute(f'SELECT last_error FROM {table} WHERE last_error IS NOT NULL').fetchall():
                _tally(report, table + '_errors', _rules(row['last_error']))
        located = []
        if objects is not None:
            located += [('segment_objects', r['object_key'], r['owner'], None) for r in db.execute(
                '''SELECT c.object_key,s.owner FROM cloud_conversation_segments c
                JOIN enterprise_sources s ON s.id=c.source_id WHERE c.tenant=? AND c.status='active'
                ORDER BY c.source_id''', (tenant,)).fetchall()]
            located += [('document_originals', r['object_key'], r['owner'], r['filename']) for r in db.execute(
                '''SELECT v.object_key,v.filename,s.owner FROM backend_document_versions v
                JOIN enterprise_sources s ON s.id=v.source_id WHERE v.tenant=? ORDER BY v.source_id''',
                (tenant,)).fetchall()]
    if objects is not None:
        from agenthub.document_ingest import prepare_document
        from agenthub.source_objects import FILE_LIMIT, ObjectMissing
        for location, key, owner, filename in located:
            try:
                with objects.open(key) as stream:
                    raw = stream.read(FILE_LIMIT + 1)
            except ObjectMissing:
                report['locations'].setdefault(location, {'rows_scanned': 0, 'rows_with_findings': 0, 'rules': {}})
                report['locations'][location]['missing'] = report['locations'][location].get('missing', 0) + 1
                continue
            text = raw.decode('utf-8', 'ignore')
            if filename:
                text += '\n' + prepare_document(raw, filename).text
            _tally(report, location, _rules(text), owner)
    report['rules'] = dict(sorted(report['rules'].items()))
    report['by_owner'] = dict(sorted(report['by_owner'].items()))
    return report


def rewrite_redacted(store, ctx, *, apply=False, limit=100):
    """Plan or write redacted replacement revisions for the caller's own sources."""
    if type(apply) is not bool or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid_rewrite_bound')
    store._need(ctx, 'ingest')
    planned = []; skipped = {}
    with store.open() as state:
        for rows in _batches(state.db, '''SELECT r.source_id k,r.payload,r.connection,c.enrollment
                FROM backend_source_heads h JOIN backend_source_revisions r ON r.source_id=h.source_id
                JOIN enterprise_sources s ON s.id=r.source_id JOIN backend_connections c ON c.id=r.connection
                WHERE s.tenant=? AND s.owner=? AND s.active=1 AND r.source_id>?
                ORDER BY r.source_id LIMIT ?''', (ctx['tenant'], ctx['actor'])):
            for row in rows:
                if not findings(row['payload']):
                    continue
                payload = json.loads(row['payload'])
                if row['enrollment'] != ctx['enrollment']:
                    skipped['other_enrollment'] = skipped.get('other_enrollment', 0) + 1
                    continue
                try:
                    safe, added = redact_capture(payload)
                except ValueError:
                    skipped['identity_or_invalid'] = skipped.get('identity_or_invalid', 0) + 1
                    continue
                if added and len(planned) < limit:
                    planned.append(dict(safe, revision=str(int(payload['revision']) + 1)))
    written = 0
    if apply:
        for value in planned:
            store.ingest_general(ctx, value)
            written += 1
    return {'planned': len(planned), 'rewritten': written, 'applied': apply, 'skipped': skipped,
            'superseded_revisions_retained': written, 'values_reported': False}


def main(argv=None):
    import argparse
    from pathlib import Path
    from agenthub.cloud_maintenance import _operator
    from agenthub.cloud_local import runtime, receipt
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--profile', required=True); parser.add_argument('--tenant', required=True)
    parser.add_argument('--objects', action='store_true', help='also read retained object bodies')
    parser.add_argument('--rewrite-redacted', action='store_true')
    parser.add_argument('--apply', action='store_true', help='write replacements (default: dry run)')
    parser.add_argument('--token-file'); parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args(argv)
    if args.apply and not args.rewrite_redacted:
        parser.error('--apply only applies to --rewrite-redacted')
    profile, _, _ = _operator(args.profile, args.tenant)
    _, registry = runtime(profile); store = registry.resolve(args.tenant)
    if args.rewrite_redacted:
        if not args.token_file:
            parser.error('--token-file owner credential required')
        token = Path(args.token_file).expanduser().resolve()
        if not token.is_file() or token.stat().st_mode & 0o077:
            raise ValueError('owner_token_permissions')
        ctx = store.authenticate(token.read_text().strip())
        result = rewrite_redacted(store, ctx, apply=args.apply, limit=args.limit)
        command = 'secret-rewrite-' + ('apply' if args.apply else 'plan')
    else:
        adapter = getattr(getattr(store, 'conversation_segments', None), 'objects', None)
        if args.objects and adapter is None:
            raise ValueError('object_adapter_required')
        result = scan(store, objects=adapter if args.objects else None)
        command = 'secret-scan'
    print(json.dumps(receipt(profile, command, result), sort_keys=True))


if __name__ == '__main__':
    main()
