"""Owner self-service: find, delete, re-share and grant one's own content.

Every operation acts only on sources the caller owns (`enterprise_sources.owner`,
the represented person for delegated capture) in the caller's tenant, and needs
no operator:

* **find** (`mine`, `read`): the caller's sources or capture sessions, filtered
  by project, session, capture date and current sharing level, with each
  source's current effective level and the terms of the minimum that bind it;
* **delete** (`forget`, `withdraw`): one or more sources, a capture session, or
  everything in a project or date range, through the canonical purge
  (`EnterpriseStore._expire_source_tx`): the same stores, the same deletion
  journal. A legal hold still wins: the source is hidden, preserved, and
  reported as held;
* **change sharing** (`change_sharing`): set the stored level of the caller's
  sources. Lowering needs `withdraw` or `share` and applies on the next request.
  Raising needs `share`, is refused while the operator restricts owner sharing,
  is never stored above the source's origin ceiling, and is capped again at
  query time by every current ceiling; the response says which term capped it.
  Every change is recorded in `enterprise_sharing_changes` and the audit log;
* **grants** (`grant`, `revoke`, `grants`): name a principal or a project that
  may read the passages (and, optionally, the originals) of a source, a capture
  session or a connection. Granting needs `share` (or the operator `policy`
  action); revoking needs `share`, `withdraw` or `policy`;
* **moderation** (`moderate`, `moderate` action): a project member withdraws
  (hides) other people's sources in that project. It never deletes, never reads
  originals, and is audited; it is the hook for per-project roles.

Bulk and widening operations are two-step: without `confirm` they return a
preview with a `confirm_token` bound to the exact selection; the same request
with `confirm` set to that token applies it. A selection that changed in between
is previewed again instead of applied. Responses carry identifiers, counts and
levels, never source text beyond the owner's own short previews.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time

from agenthub.project_sharing import RANK, SHARING, lowest, origin_ceiling, origin_projects

MAX_SELECTION = 200
MAX_LIST = 200
SESSION_HANDLE = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[A-Za-z0-9_-]{1,120}')
SELECTORS = ('source_ids', 'session', 'project', 'since', 'until')
TERMS = ('source', 'person', 'project', 'tenant', 'connection', 'origin')


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _timestamp(value, name):
    """Epoch seconds from a number, a YYYY-MM-DD date (UTC midnight) or an ISO time."""
    if value is None:
        return None
    if type(value) in (int, float) and 0 < value < 1e11:
        return float(value)
    if isinstance(value, str) and len(value) <= 40:
        try:
            moment = datetime.datetime.fromisoformat(value)
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            return moment.timestamp()
    raise ValueError('invalid_' + name)


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _level(value, name, *, optional=False):
    if value is None and optional:
        return None
    if value not in SHARING:
        raise ValueError('invalid_' + name)
    return value


def confirm_token(ctx, operation, parameters, rows):
    """A non-secret binding of one preview to its exact selection and parameters."""
    return _digest(json.dumps([ctx['tenant'], ctx['actor'], operation, parameters,
                               [[row['id'], int(row['source_version'])] for row in rows]],
                              sort_keys=True, separators=(',', ':')))[:32]


def refresh_grant_flags(db, source_ids=None, *, scope=None):
    """Recompute `enterprise_documents.raw_granted` for the passages of some sources.

    `source_ids` is a list, or `scope` a (tenant, owner, scope_kind, scope_id)
    grant scope whose sources are selected in SQL. The flag is only a prefilter.
    """
    if scope is not None:
        tenant, owner, kind, ident = scope
        selected = {'source': "SELECT id FROM enterprise_sources WHERE tenant=? AND owner=? AND id=?",
                    'session': '''SELECT s.id FROM enterprise_sources s JOIN memories m ON m.id=s.id
                        WHERE s.tenant=? AND s.owner=? AND m.session=?''',
                    'connection': '''SELECT s.id FROM enterprise_sources s JOIN backend_source_revisions r
                        ON r.source_id=s.id WHERE s.tenant=? AND s.owner=? AND r.connection=?'''}[kind]
        args = (tenant, owner, ident)
    else:
        if not source_ids:
            return 0
        selected, args = 'SELECT unnest(?::text[])', (list(source_ids),)
    return db.execute('''UPDATE enterprise_documents ed SET raw_granted=CASE WHEN EXISTS(
            SELECT 1 FROM enterprise_dependencies dep JOIN enterprise_sources s ON s.id=dep.source_id
            JOIN raw_source_grants g ON g.tenant=s.tenant AND g.granted_by=s.owner AND g.revoked IS NULL AND g.passages=1
            WHERE dep.document_id=ed.id AND ((g.scope_kind='source' AND g.scope_id=s.id)
                OR (g.scope_kind='session' AND g.scope_id=(SELECT m.session FROM memories m WHERE m.id=s.id))
                OR (g.scope_kind='connection' AND g.scope_id=(SELECT r.connection FROM backend_source_revisions r
                    WHERE r.source_id=s.id)))) THEN 1 ELSE 0 END
        WHERE ed.representation='raw' AND ed.id IN (SELECT document_id FROM enterprise_dependencies
            WHERE source_id IN (''' + selected + '''))''', args).rowcount


class OwnerControlsMixin:
    """Owner self-service over the caller's own sources (see module docstring)."""

    # Selection ------------------------------------------------------------------

    def _owner_scope(self, ctx):
        """(tenant, owner) the caller acts for; a delegated credential stays in its projects."""
        return ctx['tenant'], ctx['actor']

    def _session_candidates(self, db, ctx, value, project):
        """Session digests a `session` selector names: 'current', a handle, or a host conversation ID."""
        if value == 'current':
            if not project:
                raise ValueError('current_session_requires_project')
            row = db.execute('''SELECT m.session FROM enterprise_sources s JOIN memories m ON m.id=s.id
                WHERE s.tenant=? AND s.owner=? AND s.external_project=? AND m.session IS NOT NULL
                ORDER BY s.created DESC,s.id DESC LIMIT 1''', (ctx['tenant'], ctx['actor'], project)).fetchone()
            return [row['session']] if row else []
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError('invalid_session')
        if SESSION_HANDLE.fullmatch(value):
            return [value]
        # A host conversation ID (for example a Claude Code session ID) as captured
        # through each of the caller's connections and enrollments.
        connections = [r[0] for r in db.execute('SELECT id FROM backend_connections WHERE tenant=? AND owner=?',
                                                (ctx['tenant'], ctx['actor']))]
        enrollments = [r[0] for r in db.execute('''SELECT DISTINCT enrollment FROM enterprise_credentials
            WHERE tenant=? AND coalesce(acting_for,principal)=?''', (ctx['tenant'], ctx['actor']))]
        return sorted({_digest(json.dumps([ctx['tenant'], c, value])) for c in connections}
                      | {_digest(json.dumps([ctx['tenant'], e, value])) for e in enrollments})

    def _selection_sql(self, db, ctx, value, *, require=True):
        """WHERE clause (alias s, memories m) and arguments for the caller's own sources."""
        if not isinstance(value, dict):
            raise ValueError('invalid_request')
        if require and not any(value.get(key) not in (None, '', []) for key in SELECTORS):
            raise ValueError('selection_required')
        where, args = ['s.tenant=?', 's.owner=?'], [ctx['tenant'], ctx['actor']]
        if ctx.get('delegated_projects') is not None:
            where.append('s.external_project=ANY(?::text[])')
            args.append(sorted(ctx['delegated_projects']))
        project = value.get('project')
        if project is not None:
            if not isinstance(project, str) or not NAME.fullmatch(project):
                raise ValueError('invalid_project')
            where.append('s.external_project=?')
            args.append(project)
        ids = value.get('source_ids')
        if ids is not None:
            if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SELECTION
                    or any(not isinstance(i, str) or not 1 <= len(i) <= 128 for i in ids)):
                raise ValueError('invalid_source_ids')
            where.append('s.id=ANY(?::text[])')
            args.append(sorted(set(ids)))
        session = value.get('session')
        resolved = None
        if session is not None:
            resolved = self._session_candidates(db, ctx, session, project)
            where.append('m.session=ANY(?::text[])')
            args.append(resolved)
        for key, operator in (('since', '>='), ('until', '<')):
            moment = _timestamp(value.get(key), key)
            if moment is not None:
                where.append('s.created' + operator + '?')
                args.append(moment)
        return ' AND '.join(where), args, resolved

    def _select(self, db, ctx, value, *, include_inactive, limit=MAX_SELECTION):
        from agenthub.data_lifecycle import DEFERRED, HELD, PURGED
        where, args, resolved = self._selection_sql(db, ctx, value)
        state = '' if include_inactive else ' AND s.active=1'
        rows = db.execute(f'''SELECT s.*,m.session,{HELD.format('s')} AS held
            FROM enterprise_sources s LEFT JOIN memories m ON m.id=s.id
            WHERE {where}{state} AND NOT {PURGED.format('s')}
            AND NOT ({DEFERRED.format('s')} AND {HELD.format('s')})
            ORDER BY s.created,s.id LIMIT ?''', (*args, limit + 1)).fetchall()
        return [dict(row) for row in rows[:limit]], len(rows) > limit, resolved

    # Levels -----------------------------------------------------------------------

    def _terms(self, db, source, cache):
        """Current value of each term of the minimum for one source, and the effective level."""
        key = (source['tenant'], source['external_project'], source['owner'])
        if key not in cache:
            cache[key] = self._ceilings(db, *key) or {'person': 'private', 'project': 'private',
                                                      'tenant': 'private', 'inactive': True}
        ceilings = cache[key]
        terms = {'source': source.get('sharing') or 'private', 'person': ceilings['person'],
                 'project': 'private' if ceilings.get('inactive') else ceilings['project'],
                 'tenant': ceilings['tenant'], 'connection': source['visibility'],
                 'origin': self._origin_level(db, source)}
        return terms, lowest(*terms.values())

    @staticmethod
    def _binding(terms, effective):
        """The terms that hold the effective level where it is (none at organization)."""
        if effective == 'organization':
            return []
        return [name for name in TERMS if terms[name] == effective]

    # Find ---------------------------------------------------------------------------

    def mine(self, ctx, value):
        """The caller's own sources (or sessions) with their effective sharing and why."""
        from agenthub.data_lifecycle import DEFERRED, PURGED
        self._need(ctx, 'read')
        value = dict(value or {})
        if set(value) - {*SELECTORS, 'sharing', 'state', 'view', 'limit', 'cursor'}:
            raise ValueError('invalid_request')
        level = _level(value.get('sharing'), 'sharing', optional=True)
        state = value.get('state', 'active')
        view = value.get('view', 'sources')
        limit = value.get('limit', 50)
        if state not in ('active', 'all') or view not in ('sources', 'sessions') or type(limit) is not int \
                or not 1 <= limit <= MAX_LIST:
            raise ValueError('invalid_request')
        with self.open() as opened:
            db = opened.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            where, args, resolved = self._selection_sql(db, ctx, value, require=False)
            if state == 'active':
                where += ' AND s.active=1'
            if view == 'sessions':
                return self._mine_sessions(db, ctx, where, args, limit, resolved)
            cursor = value.get('cursor')
            if cursor is not None:
                match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?):([0-9a-f]{1,128})', cursor) if isinstance(cursor, str) else None
                if not match:
                    raise ValueError('invalid_cursor')
                where += ' AND (s.created,s.id)<(?,?)'
                args += [float(match[1]), match[2]]
            items, cache, scanned, last, more = [], {}, 0, None, False
            # Filtering by the current level is applied after the minimum is computed;
            # each call scans a bounded number of rows and returns a cursor.
            batch = db.execute(f'''SELECT s.*,m.session,m.kind,left(coalesce(m.body,''),160) AS preview,
                    {PURGED.format('s')} AS purged,{DEFERRED.format('s')} AS deferred,
                    (SELECT count(*) FROM raw_source_grants g WHERE g.tenant=s.tenant AND g.granted_by=s.owner
                        AND g.revoked IS NULL AND ((g.scope_kind='source' AND g.scope_id=s.id)
                        OR (g.scope_kind='session' AND g.scope_id=m.session))) AS grants
                FROM enterprise_sources s LEFT JOIN memories m ON m.id=s.id
                WHERE {where} ORDER BY s.created DESC,s.id DESC LIMIT ?''',
                (*args, 2000 if level else limit + 1)).fetchall()
            for row in batch:
                row = dict(row)
                scanned += 1
                if len(items) == limit:
                    more = True
                    break
                last = row
                terms, effective = self._terms(db, row, cache)
                if level and effective != level:
                    continue
                items.append(self._mine_item(row, terms, effective))
            if level and scanned == len(batch) and len(batch) == 2000:
                more = True
        result = {'principal': ctx['actor'], 'view': 'sources', 'items': items, 'more': more,
                  'next_cursor': f"{last['created']}:{last['id']}" if more and last else None}
        if resolved is not None:
            result['sessions_matched'] = len(resolved)
        return result

    @staticmethod
    def _state(row):
        if row['active']:
            return 'active'
        if row.get('purged'):
            return 'deleted'
        if row.get('deferred'):
            return 'deletion_held'
        if row.get('moderated_by'):
            return 'hidden_by_moderator'
        return 'withdrawn'

    def _mine_item(self, row, terms, effective):
        state = self._state(row)
        item = {'source_id': row['id'], 'project': row['external_project'], 'session': row.get('session'),
                'captured_at': _iso(row['created']), 'occurred_at': row['occurred_at'], 'kind': row.get('kind'),
                'state': state, 'source_version': row['source_version'],
                'sharing': {'effective': effective if state == 'active' else 'private',
                            'stored': row['sharing'], 'requested': row['sharing_requested'],
                            'originals': row['originals_sharing'],
                            'binding': self._binding(terms, effective), 'terms': terms},
                'grants': int(row.get('grants') or 0)}
        if state == 'active':
            item['preview'] = (row.get('preview') or '')[:120]
        if state == 'hidden_by_moderator':
            item['moderation'] = {'by': row['moderated_by'], 'at': _iso(row['moderated_at']),
                                  'reason': row['moderation_reason']}
        return item

    def _mine_sessions(self, db, ctx, where, args, limit, resolved):
        rows = db.execute(f'''SELECT m.session,s.external_project project,count(*) sources,
                min(s.created) first_captured,max(s.created) last_captured,
                count(*) FILTER (WHERE s.active=1) active,
                count(*) FILTER (WHERE s.sharing='private') stored_private,
                count(*) FILTER (WHERE s.sharing='team') stored_team,
                count(*) FILTER (WHERE s.sharing='organization') stored_organization
            FROM enterprise_sources s JOIN memories m ON m.id=s.id
            WHERE {where} AND m.session IS NOT NULL
            GROUP BY m.session,s.external_project ORDER BY max(s.created) DESC LIMIT ?''',
            (*args, limit + 1)).fetchall()
        sessions = [{'session': r['session'], 'project': r['project'], 'sources': r['sources'],
                     'active_sources': r['active'], 'first_captured': _iso(r['first_captured']),
                     'last_captured': _iso(r['last_captured']),
                     'stored_levels': {'private': r['stored_private'], 'team': r['stored_team'],
                                       'organization': r['stored_organization']}} for r in rows[:limit]]
        result = {'principal': ctx['actor'], 'view': 'sessions', 'sessions': sessions, 'more': len(rows) > limit}
        if resolved is not None:
            result['sessions_matched'] = len(resolved)
        return result

    # Delete -------------------------------------------------------------------------

    def forget(self, ctx, value):
        """Delete the caller's selected sources through the canonical purge.

        Without `confirm` (or with a stale token) this is a preview; with the
        preview's `confirm_token` the selection is deleted. Legal holds win: a held
        source is hidden and preserved, and reported under `held`.
        """
        self._need(ctx, 'withdraw')
        value = dict(value or {})
        confirm = value.pop('confirm', None)
        if set(value) - set(SELECTORS) or (confirm is not None and confirm is not False
                                            and not isinstance(confirm, str)):
            raise ValueError('invalid_request')
        with self.open() as opened:
            if hasattr(self, 'current_identity'):
                self.current_identity(opened.db, ctx)
            rows, more, resolved = self._select(opened.db, ctx, value, include_inactive=True)
        parameters = {key: value[key] for key in sorted(value)}
        token = confirm_token(ctx, 'forget', parameters, rows)
        preview = {'operation': 'forget', 'dry_run': True, 'sources': len(rows), 'more': more,
                   'active': sum(1 for r in rows if r['active']),
                   'held': sum(1 for r in rows if r['held']),
                   'sessions': len({r['session'] for r in rows if r.get('session')}),
                   'projects': sorted({r['external_project'] for r in rows}),
                   'first_captured': _iso(rows[0]['created']) if rows else None,
                   'last_captured': _iso(rows[-1]['created']) if rows else None,
                   'confirm_token': token if rows else None,
                   'note': 'Deletion purges the sources, everything derived from them and their originals. '
                           'Sources under a legal hold are hidden and preserved until the hold is released.'}
        if resolved is not None:
            preview['sessions_matched'] = len(resolved)
        if not confirm or not rows:
            return preview
        if confirm != token:
            return dict(preview, status='selection_changed')
        counts = {'deleted': 0, 'held': 0, 'already_deleted': 0, 'skipped': 0}
        for row in rows:
            with self.delivery_lock(), self.open() as opened, opened.db:
                db = opened.db
                current = db.execute('SELECT tenant,owner,source_version FROM enterprise_sources WHERE id=? FOR UPDATE',
                                     (row['id'],)).fetchone()
                if (not current or current['tenant'] != ctx['tenant'] or current['owner'] != ctx['actor']
                        or current['source_version'] != row['source_version']):
                    counts['skipped'] += 1
                    continue
                counts[self._expire_source_tx(db, row['id'], ctx['actor'], 'owner_delete')] += 1
        objects = getattr(self, '_objects', lambda: None)()
        if objects is not None and counts['deleted']:
            try:
                self._drain_bounded(objects, counts['deleted'])
            except Exception:
                # The purge committed; queued original keys are retried by maintenance.
                pass
        return {'operation': 'forget', 'dry_run': False, 'more': more, **counts,
                'note': 'Held sources are hidden and preserved under a legal hold.' if counts['held'] else None}

    # Change sharing -------------------------------------------------------------------

    def change_sharing(self, ctx, value):
        """Set the stored sharing level of the caller's selected sources.

        Lowering applies on the next request. Raising needs `share`, is refused
        while the operator restricts owner sharing, is never stored above the
        origin ceiling and is capped at query time by every current ceiling.
        """
        from agenthub.enterprise import Denied
        if not {'withdraw', 'share'} & set(ctx['actions']):
            raise Denied()
        value = dict(value or {})
        confirm = value.pop('confirm', None)
        requested = _level(value.pop('sharing', None), 'sharing')
        originals = _level(value.pop('originals', None), 'originals', optional=True)
        if set(value) - set(SELECTORS) or (confirm is not None and confirm is not False
                                            and not isinstance(confirm, str)):
            raise ValueError('invalid_request')
        with self.open() as opened:
            db = opened.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            rows, more, resolved = self._select(db, ctx, value, include_inactive=False)
            tenant = db.execute('SELECT owner_sharing FROM enterprise_organizations WHERE id=?',
                                (ctx['tenant'],)).fetchone()
            plan, cache = [], {}
            for row in rows:
                plan.append(self._sharing_plan(db, row, requested, originals, cache))
        raising = [p for p in plan if p['raises']]
        refusal = None
        if raising:
            if 'share' not in ctx['actions']:
                refusal = 'raising_needs_the_share_action'
            elif ctx.get('acting_for'):
                refusal = 'delegated_credentials_cannot_raise_sharing'
            elif not tenant or tenant['owner_sharing'] != 'allowed':
                refusal = 'operator_restricts_owner_sharing_to_lowering'
        parameters = {'sharing': requested, 'originals': originals, **{k: value[k] for k in sorted(value)}}
        token = confirm_token(ctx, 'sharing', parameters, rows)
        summary = self._sharing_summary(plan, requested)
        preview = {'operation': 'change_sharing', 'dry_run': True, 'requested': requested,
                   'originals_requested': originals, 'sources': len(rows), 'more': more, **summary,
                   'confirm_token': token if rows and not refusal else None,
                   'note': 'Lowering applies on the next request. Raising applies to these sources only and is '
                           'still capped at every request by your own setting and the project, tenant, '
                           'connection and origin ceilings.'}
        if resolved is not None:
            preview['sessions_matched'] = len(resolved)
        if refusal:
            preview.update(status='refused', reason=refusal)
            return preview
        if not confirm or not rows:
            return preview
        if confirm != token:
            return dict(preview, status='selection_changed')
        changed = 0
        with self.delivery_lock(), self.open() as opened, opened.db:
            db = opened.db
            now = time.time()
            for item in plan:
                current = db.execute('''SELECT tenant,owner,active,source_version,sharing,originals_sharing
                    FROM enterprise_sources WHERE id=? FOR UPDATE''', (item['id'],)).fetchone()
                if (not current or current['tenant'] != ctx['tenant'] or current['owner'] != ctx['actor']
                        or not current['active'] or current['source_version'] != item['version']):
                    continue
                if (current['sharing'], current['originals_sharing']) == (item['stored'], item['originals']):
                    continue
                db.execute('''UPDATE enterprise_sources SET sharing=?,originals_sharing=?,policy_version=policy_version+1
                    WHERE id=?''', (item['stored'], item['originals'], item['id']))
                # Passages of this source: the stored-level prefilter. Derived documents
                # that depend on it are capped like passages from now on (migration 047).
                db.execute('''UPDATE enterprise_documents SET raw_shared=? WHERE representation='raw'
                    AND id IN (SELECT document_id FROM enterprise_dependencies WHERE source_id=?)''',
                    (RANK[item['stored']], item['id']))
                db.execute('''UPDATE enterprise_documents SET level_capped=1,policy_version=policy_version+1
                    WHERE representation!='raw' AND level_capped=0
                    AND id IN (SELECT document_id FROM enterprise_dependencies WHERE source_id=?)''', (item['id'],))
                for tier, before, after, asked in (('passages', current['sharing'], item['stored'], requested),
                                                   ('originals', current['originals_sharing'], item['originals'],
                                                    originals or item['originals'])):
                    if before == after:
                        continue
                    capped = ','.join(item['capped_by']) if tier == 'passages' else ''
                    db.execute('''INSERT INTO enterprise_sharing_changes
                        (tenant,actor,source_id,tier,from_level,to_level,requested,capped_by,request_id,created)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''', (ctx['tenant'], ctx['actor'], item['id'], tier, before, after,
                                                         asked, capped, ctx.get('request_id'), now))
                    self._audit(db, ctx, 'source_sharing' if tier == 'passages' else 'source_originals_sharing',
                                before + '>' + after, item['id'])
                changed += 1
        return {'operation': 'change_sharing', 'dry_run': False, 'requested': requested,
                'originals_requested': originals, 'changed': changed, 'more': more, **summary}

    def _sharing_plan(self, db, row, requested, originals, cache):
        """What a change would store for one source and what the request would then reach."""
        stored = lowest(requested, origin_ceiling(row))
        originals_stored = lowest(originals or row['originals_sharing'], stored)
        terms, _ = self._terms(db, dict(row, sharing=stored), cache)
        effective = lowest(*terms.values())
        capped_by = [name for name in TERMS if name != 'source' and RANK[terms[name]] < RANK[requested]]
        if stored != requested and 'origin' not in capped_by:
            capped_by.append('origin')
        return {'id': row['id'], 'version': row['source_version'], 'stored': stored, 'originals': originals_stored,
                'effective': effective, 'capped_by': capped_by,
                'raises': RANK[stored] > RANK[row['sharing']] or RANK[originals_stored] > RANK[row['originals_sharing']],
                'lowers': RANK[stored] < RANK[row['sharing']] or RANK[originals_stored] < RANK[row['originals_sharing']]}

    @staticmethod
    def _sharing_summary(plan, requested):
        effective = {level: sum(1 for p in plan if p['effective'] == level) for level in SHARING}
        capped = {}
        for item in plan:
            for name in item['capped_by']:
                capped[name] = capped.get(name, 0) + 1
        summary = {'raises': sum(1 for p in plan if p['raises']), 'lowers': sum(1 for p in plan if p['lowers']),
                   'unchanged': sum(1 for p in plan if not p['raises'] and not p['lowers']),
                   'effective_after': effective, 'capped_by': capped,
                   'stored_below_request_by_origin': sum(1 for p in plan if p['stored'] != requested)}
        if capped:
            summary['capped'] = ('Some sources will be readable less widely than ' + requested + ': '
                                 + ', '.join(f'{name} ({count})' for name, count in sorted(capped.items()))
                                 + '. A source tainted by its origin can never be raised above that origin.')
        return summary

    # Grants ---------------------------------------------------------------------------

    def _grant_target(self, db, ctx, value):
        scope = value.get('scope')
        ids = value.get('ids')
        if scope not in ('source', 'session', 'connection') or not isinstance(ids, list) \
                or not 1 <= len(ids) <= (MAX_SELECTION if scope == 'source' else 1) \
                or any(not isinstance(i, str) or not 1 <= len(i) <= 256 for i in ids):
            raise ValueError('invalid_grant_scope')
        grantee_kind, grantee = value.get('grantee_kind'), value.get('grantee')
        if grantee_kind not in ('principal', 'project') or not isinstance(grantee, str) or not NAME.fullmatch(grantee):
            raise ValueError('invalid_grantee')
        ids = sorted(set(ids))
        if scope == 'session':
            # 'current', a session handle, or a host conversation ID (one per connection).
            ids = [c for c in self._session_candidates(db, ctx, ids[0], value.get('project'))
                   if db.execute('''SELECT 1 FROM memories m JOIN enterprise_sources s ON s.id=m.id
                       WHERE m.session=? AND s.tenant=? AND s.owner=? LIMIT 1''',
                       (c, ctx['tenant'], ctx['actor'])).fetchone()]
            if not ids:
                raise ValueError('unknown_session')
        return scope, ids, grantee_kind, grantee

    def grant(self, ctx, value):
        """Grant a named principal or project the passages (and optionally originals)
        of the caller's source(s), capture session or connection. Preview first."""
        from agenthub.enterprise import Denied
        from agenthub.raw_access import _owned_scope, scope_sources, upsert_grant
        if not {'share', 'policy'} & set(ctx['actions']):
            raise Denied()
        value = dict(value or {})
        confirm = value.pop('confirm', None)
        include_originals = value.pop('originals', False)
        if type(include_originals) is not bool or set(value) - {'scope', 'ids', 'grantee_kind', 'grantee', 'project'} \
                or (confirm is not None and confirm is not False and not isinstance(confirm, str)):
            raise ValueError('invalid_request')
        issued_with = 'share' if 'share' in ctx['actions'] else 'policy'
        with self.open() as opened:
            db = opened.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            scope, ids, grantee_kind, grantee = self._grant_target(db, ctx, value)
            refusal = None
            if issued_with == 'share':
                tenant = db.execute('SELECT owner_sharing FROM enterprise_organizations WHERE id=?',
                                    (ctx['tenant'],)).fetchone()
                if ctx.get('acting_for'):
                    refusal = 'delegated_credentials_cannot_grant'
                elif not tenant or tenant['owner_sharing'] != 'allowed':
                    refusal = 'operator_restricts_owner_sharing_to_lowering'
            if not all(_owned_scope(db, ctx, scope, ident) for ident in ids):
                raise Denied()
            if grantee_kind == 'principal':
                known = db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=? AND active=1',
                                   (ctx['tenant'], grantee)).fetchone()
                if grantee == ctx['actor']:
                    raise ValueError('cannot_grant_to_yourself')
            else:
                known = db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=? AND id=? AND active=1',
                                   (ctx['tenant'], grantee)).fetchone()
            if not known:
                raise ValueError('unknown_grantee')
            covered = []
            for ident in ids:
                covered += scope_sources(db, ctx['tenant'], ctx['actor'], scope, ident, limit=5000)
            origins = self._grant_origins(db, ctx, covered, grantee_kind, grantee)
            rows = [{'id': i, 'source_version': 0} for i in ids]
        parameters = {'scope': scope, 'grantee_kind': grantee_kind, 'grantee': grantee, 'originals': include_originals}
        token = confirm_token(ctx, 'grant', parameters, rows)
        preview = {'operation': 'grant', 'dry_run': True, 'scope': scope, 'scopes': len(ids),
                   'grantee_kind': grantee_kind, 'grantee': grantee,
                   'tiers': ['passages'] + (['originals'] if include_originals else []),
                   'sources_covered': len(covered), **origins,
                   'confirm_token': None if refusal else token,
                   'note': ('A grant lets the named reader read these passages' +
                            (' and originals' if include_originals else '') + ' even where your sharing level '
                            'would not reach them. It never carries a source beyond its origin: tainted sources '
                            'stay with the people their origin allows. Originals also need the reader\'s '
                            'source_read action. Session and connection grants cover later captures too. '
                            'Revoke at any time.')}
        if refusal:
            return dict(preview, status='refused', reason=refusal)
        if confirm != token:
            return preview if not confirm else dict(preview, status='selection_changed')
        with self.delivery_lock(), self.open() as opened, opened.db:
            db = opened.db
            for ident in ids:
                if not _owned_scope(db, ctx, scope, ident):
                    raise Denied()
                upsert_grant(self, db, ctx, scope, ident, grantee_kind, grantee, passages=True,
                             originals=include_originals, issued_with=issued_with)
                refresh_grant_flags(db, scope=(ctx['tenant'], ctx['actor'], scope, ident))
                self._audit(db, ctx, 'owner_grant', 'granted', json.dumps(
                    [scope, ident, grantee_kind, grantee, 'passages+originals' if include_originals else 'passages']))
        return dict(preview, dry_run=False, confirm_token=None, granted=len(ids))

    def _grant_origins(self, db, ctx, covered, grantee_kind, grantee):
        """How many covered sources the origin keeps from this grantee (reported, enforced at query time)."""
        private = limited = 0
        for ident in covered[:5000]:
            source = db.execute('SELECT * FROM enterprise_sources WHERE id=?', (ident,)).fetchone()
            if not source:
                continue
            level = self._origin_level(db, dict(source))
            if level == 'private':
                private += 1
            elif level == 'team':
                if grantee_kind == 'project':
                    limited += 1
                elif not all(self._active_member(db, dict(ctx, actor=grantee), name, project_active=False)
                             for name in (source['external_project'], *origin_projects(dict(source)))):
                    private += 1
        result = {'kept_by_origin': private}
        if limited:
            result['limited_by_origin'] = limited
        if private or limited:
            result['origin'] = ('Some covered sources came from content restricted by its origin (for example '
                                'recalled from a private or another project\'s memory). The grant does not reach '
                                'them' + (' for readers outside the source projects' if limited else '') + '.')
        return result

    def revoke(self, ctx, value):
        """Revoke grants the caller issued. Needs `confirm: true`."""
        from agenthub.enterprise import Denied
        if not {'share', 'withdraw', 'policy'} & set(ctx['actions']):
            raise Denied()
        value = dict(value or {})
        if value.pop('confirm', None) is not True:
            raise ValueError('confirm_required')
        if set(value) - {'scope', 'ids', 'grantee_kind', 'grantee', 'project'}:
            raise ValueError('invalid_request')
        revoked = 0
        with self.delivery_lock(), self.open() as opened, opened.db:
            db = opened.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            scope, ids, grantee_kind, grantee = self._grant_target(db, ctx, value)
            for ident in ids:
                changed = db.execute('''UPDATE raw_source_grants SET revoked=?,revoked_by=? WHERE tenant=?
                    AND scope_kind=? AND scope_id=? AND grantee_kind=? AND grantee=? AND granted_by=?
                    AND revoked IS NULL''', (time.time(), ctx['actor'], ctx['tenant'], scope, ident,
                                             grantee_kind, grantee, ctx['actor'])).rowcount
                if changed:
                    refresh_grant_flags(db, scope=(ctx['tenant'], ctx['actor'], scope, ident))
                revoked += changed
                self._audit(db, ctx, 'owner_grant', 'revoked' if changed else 'absent',
                            json.dumps([scope, ident, grantee_kind, grantee]))
        return {'operation': 'revoke', 'revoked': revoked, 'scope': scope, 'grantee_kind': grantee_kind,
                'grantee': grantee}

    def grants(self, ctx, value=None):
        """Grants the caller issued, and grants that name the caller or a project of theirs."""
        self._need(ctx, 'read')
        value = dict(value or {})
        if set(value) - {'include_revoked'} or type(value.get('include_revoked', False)) is not bool:
            raise ValueError('invalid_request')
        revoked = '' if value.get('include_revoked') else ' AND revoked IS NULL'
        with self.open() as opened:
            db = opened.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            issued = db.execute('''SELECT scope_kind,scope_id,grantee_kind,grantee,passages,originals,issued_with,
                    created,revoked,revoked_by FROM raw_source_grants WHERE tenant=? AND granted_by=?''' + revoked +
                ' ORDER BY created DESC LIMIT 500', (ctx['tenant'], ctx['actor'])).fetchall()
            received = db.execute('''SELECT g.granted_by,g.scope_kind,g.grantee_kind,g.grantee,g.passages,g.originals,
                    g.created FROM raw_source_grants g WHERE g.tenant=? AND g.revoked IS NULL
                AND ((g.grantee_kind='principal' AND g.grantee=?) OR (g.grantee_kind='project' AND g.grantee IN
                    (SELECT project FROM enterprise_memberships WHERE tenant=? AND principal=? AND active=1)))
                ORDER BY g.created DESC LIMIT 500''', (ctx['tenant'], ctx['actor'], ctx['tenant'], ctx['actor'])).fetchall()
        def tiers(row):
            return [name for name in ('passages', 'originals') if row[name]]
        return {'issued': [{'scope': r['scope_kind'], 'id': r['scope_id'], 'grantee_kind': r['grantee_kind'],
                            'grantee': r['grantee'], 'tiers': tiers(r), 'issued_with': r['issued_with'],
                            'granted_at': _iso(r['created']), 'active': r['revoked'] is None,
                            **({'revoked_at': _iso(r['revoked']), 'revoked_by': r['revoked_by']}
                               if r['revoked'] is not None else {})} for r in issued],
                'received': [{'from': r['granted_by'], 'scope': r['scope_kind'], 'via': r['grantee_kind'],
                              'tiers': tiers(r), 'granted_at': _iso(r['created'])} for r in received]}

    # Moderation ----------------------------------------------------------------------

    def moderate(self, ctx, value):
        """A project member with `moderate` withdraws (hides) sources in that project.

        Never deletes and never reads originals. The owner sees the source as
        hidden by a moderator, with who, when and the reason. Preview first.
        """
        from agenthub.enterprise import Denied
        from agentclient.enterprise_capture import redact
        self._need(ctx, 'moderate')
        if ctx.get('acting_for'):
            raise Denied()
        value = dict(value or {})
        confirm = value.pop('confirm', None)
        project, ids, reason = value.get('project'), value.get('source_ids'), value.get('reason')
        if (set(value) - {'project', 'source_ids', 'reason'} or not isinstance(project, str)
                or not NAME.fullmatch(project) or not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(i, str) or not 1 <= len(i) <= 128 for i in ids)
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200
                or redact(reason)[0] != reason
                or (confirm is not None and confirm is not False and not isinstance(confirm, str))):
            raise ValueError('invalid_moderation_request')
        ids = sorted(set(ids))
        with self.open() as opened:
            db = opened.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            if not self._active_member(db, ctx, project):
                raise Denied()
            rows = [dict(r) for r in db.execute('''SELECT id,source_version,owner FROM enterprise_sources
                WHERE tenant=? AND external_project=? AND active=1 AND id=ANY(?::text[]) ORDER BY id''',
                (ctx['tenant'], project, ids)).fetchall()]
        token = confirm_token(ctx, 'moderate', {'project': project, 'reason': reason}, rows)
        preview = {'operation': 'moderate', 'dry_run': True, 'project': project, 'sources': len(rows),
                   'not_found_or_inactive': len(ids) - len(rows),
                   'owners': len({r['owner'] for r in rows}), 'confirm_token': token if rows else None,
                   'note': 'Moderation withdraws (hides) the sources and everything derived from them. It does '
                           'not delete them; their owners can still see and delete them.'}
        if not confirm or not rows:
            return preview
        if confirm != token:
            return dict(preview, status='selection_changed')
        hidden = 0
        with self.delivery_lock(), self.open() as opened, opened.db:
            db = opened.db
            if not self._active_member(db, ctx, project):
                raise Denied()
            now = time.time()
            for row in rows:
                current = db.execute('''SELECT tenant,external_project,active FROM enterprise_sources
                    WHERE id=? FOR UPDATE''', (row['id'],)).fetchone()
                if not current or current['tenant'] != ctx['tenant'] or current['external_project'] != project \
                        or not current['active']:
                    continue
                db.execute('''UPDATE enterprise_sources SET active=0,source_version=source_version+1,
                    moderated_by=?,moderated_at=?,moderation_reason=? WHERE id=?''',
                    (ctx['actor'], now, reason, row['id']))
                db.execute('UPDATE memories SET active=0 WHERE id=?', (row['id'],))
                db.execute('DELETE FROM memory_fts WHERE id=?', (row['id'],))
                db.execute('''INSERT INTO enterprise_deletion_journal(tenant,source_id,operation,created)
                    VALUES(?,?,'withdraw',?)''', (ctx['tenant'], row['id'], now))
                self._block_dependents_tx(db, row['id'], 'withdraw')
                self._audit(db, ctx, 'moderate_withdraw', 'applied', row['id'])
                hidden += 1
        return {'operation': 'moderate', 'dry_run': False, 'project': project, 'hidden': hidden}
