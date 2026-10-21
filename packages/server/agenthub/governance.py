"""Operator controls against shared-memory poisoning, and the capture notice.

* Ingest marks instruction-shaped text (`instruction_like`) and holds new content
  from quarantined principals. Nothing is deleted or rewritten at ingest.
* Quarantine is an operator switch per principal. Held sources stay readable by
  their owner and remain in PostgreSQL for review; every other reader loses them,
  and every document derived from them, through the compiled search policy.
* Bulk withdrawal removes a principal's sources in a time window from retrieval.
  It is a dry run unless explicitly applied, and it withdraws (retains) rather
  than deletes, so retained evidence and any later legal hold are unaffected.
* The capture notice is the tenant's operator-configured wording that clients
  show before and while capturing.

Operator methods take no request context; their audit rows use the
`local_operator` actor, like other operator-only policy changes.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from agenthub.instruction_shape import instruction_like

OPERATOR = 'local_operator'
DEFAULT_NOTICE = (
    'This project is enrolled in Vaelius shared memory. While capture is on, your prompts, '
    'the assistant\'s replies, and tool calls with their results are sent to your organization\'s '
    'Vaelius service after local secret redaction. Who can read them is decided by the service: '
    'private sources only by you, team sources by members of this project, organization sources '
    'by everyone in your organization. Pause with `vaelius-client pause`; withdraw an item you '
    'captured with the withdraw_memory tool.')
CAPTURED = ['user_prompts', 'assistant_replies', 'tool_calls', 'tool_results']
_NOTICE_KEYS = {'text', 'policy_url'}
_WITHDRAW_BATCH = 25


def _notice_entry(value):
    if not isinstance(value, dict) or set(value) - _NOTICE_KEYS:
        raise ValueError('runtime_capture_notice_configuration')
    text = value.get('text')
    url = value.get('policy_url')
    if text is not None and (not isinstance(text, str) or not 40 <= len(text) <= 2000):
        raise ValueError('runtime_capture_notice_text')
    if url is not None and (not isinstance(url, str) or len(url) > 512
                            or not re.fullmatch(r'https://[^\s<>"]+', url)):
        raise ValueError('runtime_capture_notice_policy_url')
    return {'text': text, 'policy_url': url}


def notice_from_settings(value):
    """{'text','policy_url','tenants':{tenant:{'text','policy_url'}}}; all optional."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - (_NOTICE_KEYS | {'tenants'}):
        raise ValueError('runtime_capture_notice_configuration')
    tenants = value.get('tenants', {})
    if not isinstance(tenants, dict) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(t)) for t in tenants):
        raise ValueError('runtime_capture_notice_configuration')
    return {'default': _notice_entry({k: value[k] for k in _NOTICE_KEYS if k in value}),
            'tenants': {tenant: _notice_entry(entry) for tenant, entry in tenants.items()}}


def parse_time(value):
    """Epoch seconds or an ISO-8601 time with an explicit offset (Z accepted)."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid_time')
    if re.fullmatch(r'\d+(?:\.\d+)?', value):
        return float(value)
    if not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', value):
        raise ValueError('time_requires_offset')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).timestamp()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if value else None


class GovernanceMixin:
    capture_notice_config = None

    # Ingest ------------------------------------------------------------------

    def _governance_mark(self, db, ctx, source_id, text):
        """Label instruction-shaped text and hold new content from a quarantined principal."""
        flagged = instruction_like(text)
        held = db.execute('SELECT 1 FROM enterprise_principal_quarantine WHERE tenant=? AND principal=?',
                          (ctx['tenant'], ctx['actor'])).fetchone() is not None
        if not flagged and not held:
            return
        db.execute('UPDATE enterprise_sources SET instruction_like=?,principal_quarantine=? WHERE id=?',
                   (int(flagged), int(held), source_id))
        if flagged:
            self._audit(db, ctx, 'instruction_like', 'flagged', source_id)
        if held:
            self._audit(db, ctx, 'ingest_quarantine', 'held', source_id)

    # Quarantine --------------------------------------------------------------

    def _operator_audit(self, db, operation, disposition, target):
        db.execute('''INSERT INTO enterprise_audit(tenant,actor,operation,disposition,target_id,created)
            VALUES(?,?,?,?,?,?)''', (self.tenant_id, OPERATOR, operation, disposition, target, time.time()))

    def _known_principal(self, db, principal):
        if not isinstance(principal, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', principal):
            raise ValueError('invalid_principal')
        if not db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=?',
                          (self.tenant_id, principal)).fetchone():
            raise ValueError('unknown_principal')

    def quarantine_principal(self, principal, *, reason, since=None):
        """Hold the principal's new content, and existing content captured at or after `since`."""
        if not isinstance(reason, str) or not 3 <= len(reason) <= 500:
            raise ValueError('quarantine_reason_required')
        now = time.time()
        since = now if since is None else parse_time(since)
        if since > now:
            raise ValueError('quarantine_since_in_future')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            self._known_principal(db, principal)
            db.execute('''INSERT INTO enterprise_principal_quarantine(tenant,principal,since,reason,created)
                VALUES(?,?,?,?,?) ON CONFLICT(tenant,principal) DO UPDATE
                SET since=least(enterprise_principal_quarantine.since,excluded.since),reason=excluded.reason''',
                (self.tenant_id, principal, since, reason, now))
            held = db.execute('''UPDATE enterprise_sources SET principal_quarantine=1
                WHERE tenant=? AND owner=? AND created>=? AND principal_quarantine=0''',
                (self.tenant_id, principal, since)).rowcount
            self._operator_audit(db, 'principal_quarantine', 'applied', principal)
        return {'principal': principal, 'quarantined': True, 'since': _iso(since),
                'held_existing_sources': held, 'new_content': 'held_from_other_readers'}

    def release_principal(self, principal, *, reason):
        """End a quarantine after review; held sources become readable under normal policy."""
        if not isinstance(reason, str) or not 3 <= len(reason) <= 500:
            raise ValueError('release_reason_required')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            self._known_principal(db, principal)
            existed = db.execute('DELETE FROM enterprise_principal_quarantine WHERE tenant=? AND principal=?',
                                 (self.tenant_id, principal)).rowcount
            released = db.execute('''UPDATE enterprise_sources SET principal_quarantine=0
                WHERE tenant=? AND owner=? AND principal_quarantine=1''', (self.tenant_id, principal)).rowcount
            self._operator_audit(db, 'principal_quarantine', 'released' if existed else 'not_quarantined', principal)
        return {'principal': principal, 'quarantined': False, 'released_sources': released}

    def governance_status(self):
        with self.open() as state:
            db = state.db
            held = [{'principal': r['principal'], 'since': _iso(r['since']), 'created': _iso(r['created']),
                     'held_sources': r['held']} for r in db.execute('''SELECT q.principal,q.since,q.created,
                    (SELECT count(*) FROM enterprise_sources s WHERE s.tenant=q.tenant AND s.owner=q.principal
                        AND s.principal_quarantine=1) held
                    FROM enterprise_principal_quarantine q WHERE q.tenant=? ORDER BY q.principal''',
                    (self.tenant_id,))]
            flagged = db.execute('''SELECT count(*) FROM enterprise_sources
                WHERE tenant=? AND active=1 AND instruction_like=1''', (self.tenant_id,)).fetchone()[0]
        return {'quarantined_principals': held, 'active_instruction_like_sources': flagged}

    # Bulk withdrawal ---------------------------------------------------------

    def withdraw_principal_content(self, principal, *, since, until=None, apply=False, limit=200,
                                   reason='operator bulk withdrawal'):
        """Withdraw a principal's active sources captured in [since, until). Dry run by default."""
        since = parse_time(since)
        until = time.time() if until is None else parse_time(until)
        if since is None or since >= until or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid_bulk_withdrawal_window')
        if not isinstance(reason, str) or not 3 <= len(reason) <= 500:
            raise ValueError('withdrawal_reason_required')
        with self.open() as state:
            db = state.db
            self._known_principal(db, principal)
            rows = db.execute('''SELECT id,instruction_like FROM enterprise_sources
                WHERE tenant=? AND owner=? AND active=1 AND created>=? AND created<?
                ORDER BY created,id LIMIT ?''', (self.tenant_id, principal, since, until, limit + 1)).fetchall()
            truncated = len(rows) > limit
            ids = [r['id'] for r in rows[:limit]]
            dependents = db.execute('''SELECT count(DISTINCT x.document_id) FROM enterprise_dependencies x
                JOIN enterprise_documents d ON d.id=x.document_id
                WHERE x.source_id=ANY(?::text[]) AND d.active=1''', (ids,)).fetchone()[0] if ids else 0
            held = self._legal_hold_count(db, ids)
        report = {'principal': principal, 'since': _iso(since), 'until': _iso(until),
                  'candidate_sources': len(ids), 'instruction_like_sources': sum(r['instruction_like'] for r in rows[:limit]),
                  'active_dependent_documents': dependents, 'truncated': truncated,
                  'dry_run': not apply, 'withdrawn': 0, 'operation': 'withdraw',
                  'retained_for_review': True, 'legal_hold_covered_sources': held}
        if not apply:
            return report
        withdrawn = 0
        for offset in range(0, len(ids), _WITHDRAW_BATCH):
            batch = ids[offset:offset + _WITHDRAW_BATCH]
            # Short policy transactions: searches wait on this lock at delivery.
            with self.delivery_lock(), self.open() as state, state.db:
                db = state.db
                for source_id in batch:
                    withdrawn += self._operator_withdraw_source(db, source_id, principal)
        with self.open() as state, state.db:
            self._operator_audit(state.db, 'principal_bulk_withdraw', 'applied', json.dumps(
                {'principal': principal, 'since': _iso(since), 'until': _iso(until), 'withdrawn': withdrawn,
                 'reason_sha256': hashlib.sha256(reason.encode()).hexdigest()[:16]}, sort_keys=True))
        report.update(withdrawn=withdrawn)
        return report

    @staticmethod
    def _legal_hold_count(db, ids):
        """Sources under a legal hold, when the data-lifecycle module provides holds.

        Withdrawal retains content, so a hold never blocks it; the count only tells
        the operator that held evidence is involved. None means no hold API exists.
        """
        try:
            from agenthub.data_lifecycle import legal_hold_covers
        except ImportError:
            return None
        rows = db.execute('SELECT * FROM enterprise_sources WHERE id=ANY(?::text[])', (ids,)).fetchall() if ids else []
        return sum(1 for row in rows if legal_hold_covers(db, row))

    def _operator_withdraw_source(self, db, source_id, principal):
        """Same effect as an owner `withdraw` lifecycle operation, applied by the operator."""
        source = db.execute('SELECT * FROM enterprise_sources WHERE id=? FOR UPDATE', (source_id,)).fetchone()
        if not source or source['tenant'] != self.tenant_id or source['owner'] != principal or not source['active']:
            return 0
        now = time.time()
        db.execute('UPDATE enterprise_sources SET active=0,source_version=source_version+1 WHERE id=?', (source_id,))
        db.execute('UPDATE memories SET active=0 WHERE id=?', (source_id,))
        db.execute('DELETE FROM memory_fts WHERE id=?', (source_id,))
        db.execute('INSERT INTO enterprise_deletion_journal(tenant,source_id,operation,created) VALUES(?,?,?,?)',
                   (self.tenant_id, source_id, 'withdraw', now))
        for row in db.execute('SELECT document_id FROM enterprise_dependencies WHERE source_id=?', (source_id,)).fetchall():
            db.execute('''UPDATE enterprise_documents SET active=0,blocked_reason='withdraw',
                policy_version=policy_version+1 WHERE id=?''', (row[0],))
            db.execute('DELETE FROM knowledge_fts WHERE document_id=?', (row[0],))
            db.execute('DELETE FROM knowledge_embeddings WHERE document_id=?', (row[0],))
        self._invalidate_processing_source(db, source_id, 'source_withdraw')
        self._operator_audit(db, 'withdraw', 'applied_by_operator', source_id)
        return 1

    # Delivery ----------------------------------------------------------------

    def decorate_delivery(self, path, result, request=None):
        """Attach per-item provenance after authorization; never widens delivery."""
        from agenthub.provenance import decorate_cards, bound_search, document_provenance, decorate
        if not isinstance(result, dict):
            return result
        with self.open() as state:
            db = state.db
            if path == '/enterprise/v3/search':
                cards = decorate_cards(db, result.get('results', []))
                bound = 1500 if (request or {}).get('mode') == 'automatic' else 4000
                # Source discovery adds `notice`/`truncated`, which the v3 client
                # contract rejects; deliver the documented envelope instead.
                envelope = {key: value for key, value in result.items()
                            if key in ('answerable', 'support', 'coverage_gaps', 'retrieval')}
                if result.get('truncated'):
                    envelope['coverage_gaps'] = list(dict.fromkeys([*envelope.get('coverage_gaps', []),
                                                                    'delivery_evidence_omitted']))
                return bound_search(dict(envelope, results=cards), bound=bound)
            ident = result.get('id')
            if not isinstance(ident, str):
                return result
            block, flagged = document_provenance(db, [ident]).get(ident, (None, False))
            keys = ('claim',) if 'claim' in result else ('episodes',)
            return decorate(result, block, flagged, keys)

    # Capture notice ----------------------------------------------------------

    def capture_notice(self, ctx, value):
        if not isinstance(value, dict) or set(value) - {'project', 'connection'}:
            raise ValueError('invalid_capture_notice_request')
        for key in ('project', 'connection'):
            if key in value and (not isinstance(value[key], str) or not 1 <= len(value[key]) <= 256):
                raise ValueError('invalid_capture_notice_request')
        config = self.capture_notice_config or {'default': {}, 'tenants': {}}
        tenant = config['tenants'].get(ctx['tenant'], {})
        text = tenant.get('text') or config['default'].get('text') or DEFAULT_NOTICE
        url = tenant.get('policy_url') or config['default'].get('policy_url')
        result = {'notice': text, 'policy_url': url, 'captured': CAPTURED,
                  'notice_version': hashlib.sha256(json.dumps([text, url]).encode()).hexdigest()[:16],
                  'source': 'operator' if (tenant.get('text') or config['default'].get('text')) else 'service_default'}
        if 'connection' in value:
            with self.open() as state:
                row = state.db.execute('''SELECT project,visibility,reader_ids,active FROM backend_connections
                    WHERE id=? AND tenant=? AND owner=?''', (value['connection'], ctx['tenant'], ctx['actor'])).fetchone()
            if row is None or ('project' in value and row['project'] != value['project']):
                from agenthub.enterprise import Denied
                raise Denied()
            readers = len(json.loads(row['reader_ids'] or '[]'))
            describe = {'private': 'only you',
                        'team': 'members of project ' + row['project'],
                        'organization': 'everyone in your organization'}[row['visibility']]
            if readers:
                describe += ', limited to ' + str(readers) + ' listed reader' + ('s' if readers != 1 else '') + ' and you'
            result['readers'] = {'visibility': row['visibility'], 'description': describe,
                                 'connection_active': bool(row['active'])}
        return result
