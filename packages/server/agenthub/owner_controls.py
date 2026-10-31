"""Owner self-service: find and delete one's own content, and workspace moderation.

Every operation acts only on sources the caller owns (`enterprise_sources.owner`,
the represented person for delegated capture) in the caller's tenant, and needs
no operator:

* **find** (`mine`, `read`): the caller's sources or capture sessions, filtered
  by workspace (`project`), session, capture date and whether they are private,
  with each source's current audience and what keeps it private;
* **delete** (`forget`, `withdraw`): one or more sources, a capture session, or
  everything in a project or date range, through the canonical purge
  (`EnterpriseStore._expire_source_tx`): the same stores, the same deletion
  journal. A legal hold still wins: the source is hidden, preserved, and
  reported as held;
* **moderation** (`moderate`): a workspace admin withdraws (hides) other people's
  sources in that workspace. It needs the `admin` role there and a credential
  holding `manage` (or the older `moderate` action). It never deletes, never
  reads originals, and is audited.

There is no per-item sharing (removed with migration 055): an item is read by its
workspace's audience, or only by its owner when it is personal memory. Owners
cannot widen single items or share them with chosen people.

Bulk operations are two-step: without `confirm` they return a
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

from agenthub.workspace_access import ADMIN, origin_ceiling, origin_projects

MAX_SELECTION = 200
MAX_LIST = 200
SESSION_HANDLE = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[A-Za-z0-9_-]{1,120}')
SELECTORS = ('source_ids', 'session', 'project', 'since', 'until')
PRIVACY = ('private', 'shared')
LEGACY_SHARED = ('team', 'organization')


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


def _privacy(value, name, *, optional=False):
    """'private' or 'shared'; the legacy levels team/organization mean shared."""
    if value is None and optional:
        return None
    if value in LEGACY_SHARED:
        return 'shared'
    if value not in PRIVACY:
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

    # Audience -----------------------------------------------------------------------

    def _why_private(self, db, row, cache):
        """What keeps one source from its workspace audience (empty when shared)."""
        key = (row['tenant'], row['external_project'])
        if key not in cache:
            cache[key] = db.execute('SELECT active FROM enterprise_projects WHERE tenant=? AND id=?', key).fetchone()
        reasons = []
        if int(row.get('private') or 0):
            reasons.append('private')
        if origin_ceiling(row) == 'private':
            reasons.append('origin')
        if not cache[key] or not cache[key]['active']:
            reasons.append('workspace_archived')
        return reasons

    # Find ---------------------------------------------------------------------------

    def mine(self, ctx, value):
        """The caller's own sources (or sessions) with their effective sharing and why."""
        from agenthub.data_lifecycle import DEFERRED, PURGED
        self._need(ctx, 'read')
        value = dict(value or {})
        if set(value) - {*SELECTORS, 'sharing', 'state', 'view', 'limit', 'cursor'}:
            raise ValueError('invalid_request')
        level = _privacy(value.get('sharing'), 'sharing', optional=True)
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
            # Filtering by privacy is applied after the reasons are computed; each
            # call scans a bounded number of rows and returns a cursor.
            batch = db.execute(f'''SELECT s.*,m.session,m.kind,left(coalesce(m.body,''),160) AS preview,
                    {PURGED.format('s')} AS purged,{DEFERRED.format('s')} AS deferred
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
                reasons = self._why_private(db, row, cache)
                if level and ('private' if reasons else 'shared') != level:
                    continue
                items.append(self._mine_item(row, reasons))
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

    def _mine_item(self, row, reasons):
        state = self._state(row)
        item = {'source_id': row['id'], 'project': row['external_project'], 'session': row.get('session'),
                'captured_at': _iso(row['created']), 'occurred_at': row['occurred_at'], 'kind': row.get('kind'),
                'state': state, 'source_version': row['source_version'],
                'sharing': {'private': bool(int(row.get('private') or 0)),
                            'effective': 'workspace' if state == 'active' and not reasons else 'private',
                            'reasons': reasons, 'requested': row['sharing_requested'],
                            'carried': list(origin_projects(row))}}
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
                count(*) FILTER (WHERE s.private=1) stored_private,
                count(*) FILTER (WHERE s.private=0) stored_shared
            FROM enterprise_sources s JOIN memories m ON m.id=s.id
            WHERE {where} AND m.session IS NOT NULL
            GROUP BY m.session,s.external_project ORDER BY max(s.created) DESC LIMIT ?''',
            (*args, limit + 1)).fetchall()
        sessions = [{'session': r['session'], 'project': r['project'], 'sources': r['sources'],
                     'active_sources': r['active'], 'first_captured': _iso(r['first_captured']),
                     'last_captured': _iso(r['last_captured']),
                     'stored': {'private': r['stored_private'], 'shared': r['stored_shared']}} for r in rows[:limit]]
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

    # Moderation ----------------------------------------------------------------------

    def moderate(self, ctx, value):
        """A workspace admin withdraws (hides) sources in that workspace.

        Moderation follows the admin role: it needs the `admin` role in the
        workspace and a credential holding `manage` (credentials that carry the
        older `moderate` action keep working). Never deletes and never reads
        originals. The owner sees the source as hidden by a moderator, with who,
        when and the reason. Preview first.
        """
        from agenthub.enterprise import Denied
        from agentclient.enterprise_capture import redact
        if not {'manage', 'moderate'} & set(ctx['actions']) or ctx.get('acting_for'):
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
            if self._workspace_role(db, ctx, project)[0] < ADMIN:
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
            if self._workspace_role(db, ctx, project)[0] < ADMIN:
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
