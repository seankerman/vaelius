"""Data lifecycle governance on the PostgreSQL authority.

Operator retention policies, legal holds, subject erasure and deletion
verification reuse the canonical deletion path (``_purge_deleted_source``) so a
policy expiry, an owner deletion and an erasure remove the same stores. Every
job is finite, dry-run by default, idempotent and resumable from database state.
Reports and audit rows hold identifiers and counts only, never source content.
"""
from __future__ import annotations

import json
import re
import secrets
import time

from agenthub.processing.storage import table_exists

SOURCE_TYPES = ('agent', 'conversation', 'document', 'record')
HOLD_SCOPES = ('tenant', 'project', 'principal', 'source')
DAY = 86400
# Purged sources keep a 'delete' journal row. A hold-deferred deletion keeps
# 'delete_held' until a later run completes it after the hold is released.
PURGED = "EXISTS(SELECT 1 FROM enterprise_deletion_journal j WHERE j.source_id={0}.id AND j.operation='delete')"
DEFERRED = "EXISTS(SELECT 1 FROM enterprise_deletion_journal j WHERE j.source_id={0}.id AND j.operation='delete_held')"
HELD = ("EXISTS(SELECT 1 FROM enterprise_legal_holds h WHERE h.tenant={0}.tenant AND h.released IS NULL "
        "AND ((h.scope='tenant' AND h.target={0}.tenant) OR (h.scope='project' AND h.target={0}.external_project) "
        "OR (h.scope='principal' AND h.target={0}.owner) OR (h.scope='source' AND h.target={0}.id)))")


def legal_hold_covers(db, source):
    """Return active hold IDs covering one source row (empty when not held)."""
    if not source or not table_exists(db, 'enterprise_legal_holds'):
        return []
    rows = db.execute('''SELECT id FROM enterprise_legal_holds WHERE tenant=? AND released IS NULL
        AND ((scope='tenant' AND target=?) OR (scope='project' AND target=?)
          OR (scope='principal' AND target=?) OR (scope='source' AND target=?))
        ORDER BY id LIMIT 10''', (source['tenant'], source['tenant'], source['external_project'],
                                  source['owner'], source['id'])).fetchall()
    return [row['id'] for row in rows]


def purge_retained_derivatives(db, source_id, documents):
    """Remove derived stores the original purge did not reach, in its transaction.

    Original objects are not deleted here: their keys are queued and the
    database references removed, so a rollback cannot lose a live original and a
    committed purge cannot leave a reference that a backup would copy again.
    """
    for document in documents:
        db.execute('DELETE FROM cloud_document_vectors WHERE document_id=?', (document,))
        db.execute('DELETE FROM knowledge_candidate_keys WHERE document_id=?', (document,))
        db.execute('''DELETE FROM memory_metadata WHERE memory_id IN
            (SELECT origin_memory_id FROM knowledge_documents WHERE document_id=?)''', (document,))
    db.execute('DELETE FROM memory_metadata WHERE memory_id=?', (source_id,))
    # An episode summary derives from every source of its curation job, not
    # only the cited ones; both links are indexed (migration 037).
    db.execute('''UPDATE episode_revisions SET payload_json='{}' WHERE payload_json!='{}' AND
        (revision_id IN (SELECT revision_id FROM episode_revision_evidence WHERE source_id=?)
         OR job_id IN (SELECT id FROM curation_episode_jobs WHERE position(? in source_ids)>0)
         OR job_id IN (SELECT episode_job FROM backend_processing_dependencies WHERE source_id=?))''',
               (source_id, source_id, source_id))
    revision = db.execute('SELECT connection,external_id,revision FROM backend_source_revisions WHERE source_id=?',
                          (source_id,)).fetchone()
    if revision:
        db.execute('DELETE FROM backend_parts WHERE connection=? AND external_id=? AND revision=?',
                   (revision['connection'], revision['external_id'], revision['revision']))
    tenant = db.execute('SELECT tenant FROM enterprise_sources WHERE id=?', (source_id,)).fetchone()
    tenant = tenant['tenant'] if tenant else ''
    now = time.time()
    for row in db.execute('SELECT object_key FROM backend_document_versions WHERE source_id=?', (source_id,)).fetchall():
        _queue_object(db, tenant, source_id, row['object_key'], 'document_original', now)
        db.execute('DELETE FROM backend_document_versions WHERE source_id=? AND object_key=?', (source_id, row['object_key']))
        db.execute("UPDATE backend_object_uploads SET status='removed',updated=? WHERE object_key=?", (now, row['object_key']))
    for row in db.execute('SELECT object_key FROM cloud_migrated_source_objects WHERE source_id=?', (source_id,)).fetchall():
        _queue_object(db, tenant, source_id, row['object_key'], 'migrated_original', now)
        db.execute('DELETE FROM cloud_migrated_source_objects WHERE source_id=?', (source_id,))


def _queue_object(db, tenant, source_id, key, kind, now):
    db.execute('''INSERT INTO enterprise_object_purges(object_key,tenant,source_id,kind,queued)
        VALUES(?,?,?,?,?) ON CONFLICT(object_key) DO UPDATE SET purged=NULL,queued=excluded.queued''',
               (key, tenant, source_id, kind, now))


def inventory(db, tenant, *, owner=None, source_ids=None):
    """Content-free counts of what remains for a selected source set, per store."""
    if (owner is None) == (source_ids is None):
        raise ValueError('inventory_selection_required')
    if owner is not None:
        selected, args = 'SELECT id FROM enterprise_sources WHERE tenant=? AND owner=?', [tenant, owner]
    else:
        selected, args = 'SELECT id FROM enterprise_sources WHERE tenant=? AND id=ANY(?::text[])', [tenant, list(source_ids)]
    row = db.execute(f'''WITH selected AS MATERIALIZED ({selected}),
        docs AS MATERIALIZED (SELECT DISTINCT document_id FROM enterprise_dependencies
            WHERE source_id IN (SELECT id FROM selected))
        SELECT (SELECT count(*) FROM selected) AS sources,
          (SELECT count(*) FROM enterprise_sources WHERE id IN (SELECT id FROM selected) AND active=1) AS active_sources,
          (SELECT count(*) FROM memories WHERE id IN (SELECT id FROM selected) AND body!='') AS source_text,
          (SELECT count(*) FROM memory_fts WHERE id IN (SELECT id FROM selected)) AS source_text_index,
          (SELECT count(*) FROM events WHERE id IN (SELECT id FROM selected) AND payload NOT IN ('','{{}}')) AS event_payloads,
          (SELECT count(*) FROM backend_source_revisions WHERE source_id IN (SELECT id FROM selected) AND payload!='{{}}') AS source_revisions,
          (SELECT count(*) FROM source_event_metadata WHERE source_id IN (SELECT id FROM selected)) AS source_metadata,
          (SELECT count(*) FROM docs) AS dependent_documents,
          (SELECT count(*) FROM enterprise_documents WHERE id IN (SELECT document_id FROM docs) AND active=1) AS searchable_documents,
          (SELECT count(*) FROM knowledge_revisions WHERE document_id IN (SELECT document_id FROM docs) AND claim_json!='{{}}') AS derived_revisions,
          (SELECT count(*) FROM memories WHERE id IN (SELECT origin_memory_id FROM knowledge_documents
              WHERE document_id IN (SELECT document_id FROM docs)) AND body!='') AS derived_text,
          (SELECT count(*) FROM knowledge_fts WHERE document_id IN (SELECT document_id FROM docs)) AS derived_text_index,
          (SELECT count(*) FROM cloud_document_vectors WHERE document_id IN (SELECT document_id FROM docs)) AS vectors,
          (SELECT count(*) FROM knowledge_embeddings WHERE document_id IN (SELECT document_id FROM docs)) AS legacy_vectors,
          (SELECT count(*) FROM knowledge_temporal_assertions WHERE document_id IN (SELECT document_id FROM docs)) AS temporal_assertions,
          (SELECT count(*) FROM knowledge_candidate_keys WHERE document_id IN (SELECT document_id FROM docs)) AS candidate_keys,
          (SELECT count(DISTINCT e.revision_id) FROM episode_revision_evidence e JOIN episode_revisions r
              ON r.revision_id=e.revision_id WHERE e.source_id IN (SELECT id FROM selected) AND r.payload_json!='{{}}') AS episode_summaries,
          (SELECT count(*) FROM cloud_conversation_segments WHERE source_id IN (SELECT id FROM selected) AND status='active') AS conversation_originals,
          (SELECT count(*) FROM backend_document_versions WHERE source_id IN (SELECT id FROM selected)) AS document_originals,
          (SELECT count(*) FROM cloud_migrated_source_objects WHERE source_id IN (SELECT id FROM selected)) AS migrated_originals,
          (SELECT count(*) FROM enterprise_object_purges WHERE source_id IN (SELECT id FROM selected) AND purged IS NULL) AS pending_object_deletes,
          (SELECT count(*) FROM enterprise_sources s WHERE id IN (SELECT id FROM selected) AND {PURGED.format('s')}) AS deletion_tombstones,
          (SELECT count(*) FROM enterprise_sources s WHERE id IN (SELECT id FROM selected) AND {HELD.format('s')}) AS held_sources
        ''', args).fetchone()
    return {key: int(value) for key, value in dict(row).items()}


CONTENT_STORES = ('active_sources', 'source_text', 'source_text_index', 'event_payloads', 'source_revisions',
                  'searchable_documents', 'derived_revisions', 'derived_text', 'derived_text_index', 'vectors',
                  'legacy_vectors', 'temporal_assertions', 'candidate_keys', 'episode_summaries',
                  'conversation_originals', 'document_originals', 'migrated_originals', 'pending_object_deletes')


def _validate_bound(value, name, upper):
    if type(value) is not int or not 1 <= value <= upper:
        raise ValueError(name + '_bound')
    return value


class DataLifecycleMixin:
    """Holds, retention, erasure and journal replay for one tenant store."""
    source_objects = None

    def _objects(self, objects=None):
        if objects is not None:
            return objects
        if self.source_objects is not None:
            return self.source_objects
        segments = getattr(self, 'conversation_segments', None)
        return getattr(segments, 'objects', None)

    def lifecycle(self, ctx, value):
        result = super().lifecycle(ctx, value)
        if value.get('operation') == 'delete' and self._objects() is not None:
            try:
                self.drain_object_purges()
            except Exception:
                # The purge already committed; queued keys stay pending and
                # are reported by verification until a maintenance run drains.
                pass
        return result

    # Legal holds ---------------------------------------------------------
    def _hold_admin(self, db, ctx):
        from agenthub.enterprise import Denied
        self._need(ctx, 'legal_hold')
        if hasattr(self, 'current_identity'):
            self.current_identity(db, ctx)
        self._need(ctx, 'legal_hold')
        if ctx.get('acting_for'):
            raise Denied()
        principal = db.execute('SELECT settings_admin,active FROM enterprise_principals WHERE tenant=? AND id=?',
                               (ctx['tenant'], ctx['principal'])).fetchone()
        if not principal or not principal['settings_admin'] or not principal['active']:
            raise Denied()

    @staticmethod
    def _hold_reason(reason):
        from agentclient.enterprise_capture import redact
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500 or redact(reason)[0] != reason:
            raise ValueError('legal_hold_reason_required')
        return reason

    def create_legal_hold(self, ctx, scope, target, reason):
        """Preserve covered sources from retention, deletion and erasure."""
        from agenthub.enterprise import Denied
        if scope not in HOLD_SCOPES or not isinstance(target, str) or not 1 <= len(target) <= 256:
            raise ValueError('invalid_legal_hold_scope')
        reason = self._hold_reason(reason)
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            self._hold_admin(db, ctx)
            exists = {
                'tenant': lambda: target == ctx['tenant'],
                'project': lambda: db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=? AND id=?',
                                              (ctx['tenant'], target)).fetchone(),
                'principal': lambda: db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=?',
                                                (ctx['tenant'], target)).fetchone(),
                'source': lambda: db.execute('SELECT 1 FROM enterprise_sources WHERE tenant=? AND id=?',
                                             (ctx['tenant'], target)).fetchone(),
            }[scope]()
            if not exists:
                raise Denied()
            ident = 'hold_' + secrets.token_hex(12)
            db.execute('''INSERT INTO enterprise_legal_holds(id,tenant,scope,target,reason,actor,created)
                VALUES(?,?,?,?,?,?,?)''', (ident, ctx['tenant'], scope, target, reason, ctx['actor'], time.time()))
            self._audit(db, ctx, 'legal_hold', 'created', ident)
        return {'hold_id': ident, 'scope': scope, 'target': target, 'active': True}

    def release_legal_hold(self, ctx, hold_id, reason):
        """Release never deletes; the next retention or erasure run resumes."""
        from agenthub.enterprise import Denied
        reason = self._hold_reason(reason)
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            self._hold_admin(db, ctx)
            row = db.execute('SELECT * FROM enterprise_legal_holds WHERE id=? AND tenant=? FOR UPDATE',
                             (hold_id, ctx['tenant'])).fetchone()
            if not row:
                raise Denied()
            if row['released'] is not None:
                return {'hold_id': hold_id, 'active': False, 'disposition': 'already_released'}
            db.execute('''UPDATE enterprise_legal_holds SET released=?,released_by=?,release_reason=?
                WHERE id=?''', (time.time(), ctx['actor'], reason, hold_id))
            self._audit(db, ctx, 'legal_hold', 'released', hold_id)
        return {'hold_id': hold_id, 'active': False, 'disposition': 'released'}

    def legal_holds(self, ctx, *, include_released=False):
        with self.open() as state:
            db = state.db
            self._hold_admin(db, ctx)
            rows = db.execute('''SELECT id,scope,target,reason,actor,created,released,released_by
                FROM enterprise_legal_holds WHERE tenant=?''' + ('' if include_released else ' AND released IS NULL') +
                ' ORDER BY created,id LIMIT 1000', (ctx['tenant'],)).fetchall()
        return [dict(row) for row in rows]

    # Retention policy ----------------------------------------------------
    def set_retention_policies(self, policies, *, actor='operator'):
        """Replace the tenant's declarative policy set in one transaction."""
        if not isinstance(policies, list) or len(policies) > 1000:
            raise ValueError('retention_policy_list_required')
        normalized = {}
        for item in policies:
            if not isinstance(item, dict) or set(item) - {'project', 'source_type', 'max_age_days'}:
                raise ValueError('invalid_retention_policy_fields')
            project = item.get('project', '*')
            source_type = item.get('source_type', '*')
            days = item.get('max_age_days')
            if (not isinstance(project, str) or not re.fullmatch(r'\*|[A-Za-z0-9_-]{1,120}', project)
                    or source_type not in ('*',) + SOURCE_TYPES or type(days) is not int or not 1 <= days <= 36500):
                raise ValueError('invalid_retention_policy')
            if (project, source_type) in normalized:
                raise ValueError('duplicate_retention_policy')
            normalized[(project, source_type)] = days
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            for project, _ in normalized:
                if project != '*' and not db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=? AND id=?',
                                                     (self.tenant_id, project)).fetchone():
                    raise ValueError('unknown_retention_project')
            db.execute('DELETE FROM enterprise_retention_policies WHERE tenant=?', (self.tenant_id,))
            now = time.time()
            for (project, source_type), days in sorted(normalized.items()):
                db.execute('''INSERT INTO enterprise_retention_policies
                    (tenant,project,source_type,max_age_seconds,actor,updated) VALUES(?,?,?,?,?,?)''',
                           (self.tenant_id, project, source_type, days * DAY, actor, now))
            # A changed policy restarts the bounded scan from the oldest source.
            db.execute('''UPDATE enterprise_lifecycle_runs SET status='superseded',updated=?
                WHERE tenant=? AND kind='retention' AND status='partial' ''', (now, self.tenant_id))
            self._audit(db, {'tenant': self.tenant_id, 'actor': actor}, 'retention_policy', 'replaced')
        return self.retention_policies()

    def retention_policies(self):
        with self.open() as state:
            rows = state.db.execute('''SELECT project,source_type,max_age_seconds,actor,updated
                FROM enterprise_retention_policies WHERE tenant=? ORDER BY project,source_type''',
                                    (self.tenant_id,)).fetchall()
        return [{'project': r['project'], 'source_type': r['source_type'],
                 'max_age_days': int(r['max_age_seconds'] // DAY), 'actor': r['actor'], 'updated': r['updated']}
                for r in rows]

    @staticmethod
    def _policy_age(policies, project, source_type):
        """Most specific match wins: project+type, project, type, tenant default."""
        for key in ((project, source_type), (project, '*'), ('*', source_type), ('*', '*')):
            if key in policies:
                return policies[key]
        return None

    # Shared execution ----------------------------------------------------
    def _expire(self, source_ids, actor, reason):
        counts = {'deleted': 0, 'held': 0, 'already_deleted': 0}
        for source_id in source_ids:
            with self.delivery_lock(), self.open() as state, state.db:
                counts[self._expire_source_tx(state.db, source_id, actor, reason)] += 1
        return counts

    def _record_run(self, kind, subject, applied, status, report, *, cursor=(None, None)):
        ident = kind + '_' + secrets.token_hex(12)
        now = time.time()
        with self.open() as state, state.db:
            if applied and kind == 'retention':
                # Only the newest applied run carries the resume position.
                state.db.execute('''UPDATE enterprise_lifecycle_runs SET status='resumed',updated=?
                    WHERE tenant=? AND kind=? AND applied=1 AND status='partial' ''', (now, self.tenant_id, kind))
            state.db.execute('''INSERT INTO enterprise_lifecycle_runs
                (id,tenant,kind,subject,applied,status,cursor_created,cursor_id,report,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (ident, self.tenant_id, kind, subject, int(applied), status,
                                                   cursor[0], cursor[1], json.dumps(report, sort_keys=True), now, now))
            self._audit(state.db, {'tenant': self.tenant_id, 'actor': 'operator'}, kind + '_run',
                        status if applied else 'dry_run', ident)
        return ident

    def drain_object_purges(self, objects=None, *, limit=100):
        """Delete queued originals whose references were purged; safe to repeat."""
        from agenthub.source_objects import validate_key
        _validate_bound(limit, 'object_purge', 1000)
        objects = self._objects(objects)
        if objects is None:
            raise ValueError('source_object_adapter_required')
        deleted = skipped = 0
        with self.open() as state:
            keys = [r['object_key'] for r in state.db.execute('''SELECT object_key FROM enterprise_object_purges
                WHERE tenant=? AND purged IS NULL ORDER BY queued,object_key LIMIT ?''', (self.tenant_id, limit))]
        for key in keys:
            with self.open() as state, state.db:
                db = state.db
                # The upload row lock serializes a concurrent re-upload of the
                # identical version, which reuses the same content-addressed key.
                upload = db.execute('SELECT status FROM backend_object_uploads WHERE object_key=? FOR UPDATE', (key,)).fetchone()
                referenced = (upload and upload['status'] in ('uploading', 'uploaded', 'accepted')) or db.execute(
                    '''SELECT 1 FROM backend_document_versions WHERE object_key=?
                       UNION ALL SELECT 1 FROM cloud_migrated_source_objects WHERE object_key=?''', (key, key)).fetchone()
                if referenced:
                    db.execute('DELETE FROM enterprise_object_purges WHERE object_key=?', (key,))
                    skipped += 1
                    continue
                objects.delete(validate_key(key))
                db.execute('UPDATE enterprise_object_purges SET purged=? WHERE object_key=?', (time.time(), key))
                deleted += 1
        with self.open() as state:
            pending = state.db.execute('''SELECT count(*) FROM enterprise_object_purges
                WHERE tenant=? AND purged IS NULL''', (self.tenant_id,)).fetchone()[0]
        return {'deleted': deleted, 'reference_restored': skipped, 'pending': int(pending)}

    def verify_objects_absent(self, objects=None, *, owner=None, source_ids=None, limit=1000):
        """HEAD every original key recorded for the selection; report presence counts."""
        from agenthub.source_objects import ObjectMissing
        objects = self._objects(objects)
        if objects is None:
            return {'checked': 0, 'present': 0, 'adapter': False}
        where = 'owner=?' if owner is not None else 'id=ANY(?::text[])'
        value = owner if owner is not None else list(source_ids)
        with self.open() as state:
            keys = [r['object_key'] for r in state.db.execute(f'''
                SELECT object_key FROM enterprise_object_purges WHERE source_id IN
                    (SELECT id FROM enterprise_sources WHERE tenant=? AND {where})
                UNION SELECT object_key FROM cloud_segment_uploads WHERE source_id IN
                    (SELECT id FROM enterprise_sources WHERE tenant=? AND {where}) AND status='removed'
                ORDER BY 1 LIMIT ?''', (self.tenant_id, value, self.tenant_id, value, limit + 1))]
        present = 0
        for key in keys[:limit]:
            try:
                objects.head(key)
                present += 1
            except ObjectMissing:
                pass
        return {'checked': min(len(keys), limit), 'present': present, 'adapter': True, 'more': len(keys) > limit}

    # Scheduled retention --------------------------------------------------
    def run_retention(self, *, apply=False, now=None, max_sources=100, max_scan=1000, objects=None):
        """One finite pass of operator retention policies; dry-run by default."""
        if type(apply) is not bool:
            raise ValueError('explicit_retention_apply_boolean')
        _validate_bound(max_sources, 'retention_source', 1000)
        _validate_bound(max_scan, 'retention_scan', 10000)
        now = time.time() if now is None else now
        with self.open() as state:
            db = state.db
            policies = {(r['project'], r['source_type']): r['max_age_seconds'] for r in db.execute(
                'SELECT project,source_type,max_age_seconds FROM enterprise_retention_policies WHERE tenant=?',
                (self.tenant_id,))}
            resume = db.execute('''SELECT cursor_created,cursor_id FROM enterprise_lifecycle_runs
                WHERE tenant=? AND kind='retention' AND applied=1 AND status='partial'
                ORDER BY updated DESC,id DESC LIMIT 1''', (self.tenant_id,)).fetchone()
            deferred = [r['id'] for r in db.execute(f'''SELECT s.id FROM enterprise_sources s
                WHERE s.tenant=? AND {DEFERRED.format('s')} AND NOT {PURGED.format('s')} AND NOT {HELD.format('s')}
                ORDER BY s.id LIMIT ?''', (self.tenant_id, max_sources))]
            rows = []
            cursor = ((resume['cursor_created'], resume['cursor_id'])
                      if resume and resume['cursor_created'] is not None else (-1.0, ''))
            if policies:
                horizon = now - min(policies.values())
                rows = db.execute(f'''SELECT s.id,s.created,s.external_project,
                    coalesce(r.payload::jsonb->>'source_type','agent') AS source_type,
                    {PURGED.format('s')} AS purged,{HELD.format('s')} AS held
                    FROM enterprise_sources s LEFT JOIN backend_source_revisions r ON r.source_id=s.id
                    WHERE s.tenant=? AND s.created<? AND (s.created,s.id)>(?,?)
                    ORDER BY s.created,s.id LIMIT ?''', (self.tenant_id, horizon, cursor[0], cursor[1], max_scan)).fetchall()
        budget = max_sources - len(deferred)
        expired, held, last, examined = [], 0, None, 0
        for row in rows:
            if len(expired) >= budget:
                break
            last, examined = row, examined + 1
            age = self._policy_age(policies, row['external_project'], row['source_type'])
            if row['purged'] or age is None or row['created'] >= now - age:
                continue
            if row['held']:
                held += 1
                continue
            expired.append(row['id'])
        # A cycle completes when the scan reached the policy horizon; otherwise
        # the next applied run resumes after the last examined source.
        complete = examined == len(rows) and len(rows) < max_scan
        selected = deferred + expired
        report = {'kind': 'retention', 'dry_run': not apply, 'policies': len(policies), 'scanned': len(rows),
                  'eligible': len(expired), 'held_skipped': held, 'deferred_eligible': len(deferred),
                  'cycle_complete': complete, 'provider_calls': 0}
        if not apply:
            report['run_id'] = self._record_run('retention', None, False, 'dry_run', report)
            return report
        with self.open() as state:
            before = inventory(state.db, self.tenant_id, source_ids=selected)
        counts = self._expire(selected, 'operator', 'retention_expiry')
        objects_result = self._drain_bounded(objects, len(selected))
        with self.open() as state:
            after = inventory(state.db, self.tenant_id, source_ids=selected)
        report.update(deleted=counts['deleted'], held_during_run=counts['held'],
                      already_deleted=counts['already_deleted'], objects=objects_result,
                      before=before, after=after,
                      verified=all(after[key] == 0 for key in CONTENT_STORES if key != 'pending_object_deletes'))
        cursor = (None, None) if complete else (last['created'], last['id']) if last else cursor
        report['run_id'] = self._record_run('retention', None, True, 'complete' if complete else 'partial', report,
                                            cursor=cursor)
        return report

    def _drain_bounded(self, objects, count):
        objects = self._objects(objects)
        if objects is None:
            return {'adapter': False}
        result = {'deleted': 0, 'reference_restored': 0, 'pending': 0}
        for _ in range(max(1, (count * 2 + 99) // 100)):
            step = self.drain_object_purges(objects, limit=100)
            result = {k: result.get(k, 0) + step[k] for k in ('deleted', 'reference_restored')} | {'pending': step['pending']}
            if not step['pending'] or not step['deleted']:
                break
        return result | {'adapter': True}

    # Subject erasure ------------------------------------------------------
    def erase_subject(self, principal, *, apply=False, max_sources=100, objects=None):
        """Erase content owned by one principal across every store, honoring holds.

        Owned content is what the principal authored or captured for itself
        (``owner`` is the represented actor for delegated capture). Bounded and
        idempotent: rerun until ``more`` is false. Held sources are hidden from
        retrieval and their purge is deferred until the hold is released.
        """
        if type(apply) is not bool:
            raise ValueError('explicit_erasure_apply_boolean')
        _validate_bound(max_sources, 'erasure_source', 1000)
        if not isinstance(principal, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', principal):
            raise ValueError('invalid_principal')
        with self.open() as state:
            db = state.db
            if not db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=?',
                              (self.tenant_id, principal)).fetchone():
                raise ValueError('unknown_principal')
            before = inventory(db, self.tenant_id, owner=principal)
            rows = db.execute(f'''SELECT s.id FROM enterprise_sources s
                WHERE s.tenant=? AND s.owner=? AND NOT {PURGED.format('s')}
                AND NOT ({DEFERRED.format('s')} AND {HELD.format('s')})
                ORDER BY s.id LIMIT ?''', (self.tenant_id, principal, max_sources + 1)).fetchall()
            delegated = db.execute('''SELECT count(*) FROM enterprise_sources s WHERE s.tenant=? AND s.owner!=?
                AND s.enrollment IN (SELECT enrollment FROM enterprise_credentials WHERE tenant=? AND principal=?)''',
                                   (self.tenant_id, principal, self.tenant_id, principal)).fetchone()[0]
        selected = [r['id'] for r in rows[:max_sources]]
        report = {'kind': 'erasure', 'principal': principal, 'dry_run': not apply, 'candidates': len(selected),
                  'more': len(rows) > max_sources, 'before': before,
                  'captured_for_other_owners': int(delegated), 'provider_calls': 0}
        if not apply:
            report['run_id'] = self._record_run('erasure', principal, False, 'dry_run', report)
            return report
        counts = self._expire(selected, 'operator', 'subject_erasure')
        report['objects'] = self._drain_bounded(objects, len(selected))
        with self.open() as state:
            after = inventory(state.db, self.tenant_id, owner=principal)
        report.update(deleted=counts['deleted'], held=after['held_sources'], already_deleted=counts['already_deleted'],
                      after=after, object_check=self.verify_objects_absent(objects, owner=principal))
        unheld_remaining = after['sources'] - after['deletion_tombstones'] - after['held_sources']
        report['verified'] = (not report['more'] and unheld_remaining == 0 and after['held_sources'] == 0
                              and all(after[key] == 0 for key in CONTENT_STORES)
                              and report['object_check']['present'] == 0)
        status = 'complete' if report['verified'] else 'held' if after['held_sources'] and not report['more'] else 'partial'
        report['run_id'] = self._record_run('erasure', principal, True, status, report)
        return report

    def lifecycle_report(self, run_id):
        with self.open() as state:
            row = state.db.execute('SELECT * FROM enterprise_lifecycle_runs WHERE id=? AND tenant=?',
                                   (run_id, self.tenant_id)).fetchone()
        if not row:
            raise ValueError('unknown_lifecycle_run')
        return {'run_id': row['id'], 'kind': row['kind'], 'subject': row['subject'], 'applied': bool(row['applied']),
                'status': row['status'], 'created': row['created'], 'report': json.loads(row['report'])}

    # Deletion journal for recovery ---------------------------------------
    def deletion_journal(self, *, after_id=0, limit=1000):
        """Content-free journal page; restores replay it before use."""
        if type(after_id) is not int or after_id < 0:
            raise ValueError('deletion_journal_cursor')
        _validate_bound(limit, 'deletion_journal', 10000)
        with self.open() as state:
            rows = state.db.execute('''SELECT id,source_id,operation,created FROM enterprise_deletion_journal
                WHERE tenant=? AND id>? ORDER BY id LIMIT ?''', (self.tenant_id, after_id, limit + 1)).fetchall()
        entries = [{'id': int(r['id']), 'source_id': r['source_id'], 'operation': r['operation'],
                    'created': r['created']} for r in rows[:limit]]
        return {'tenant': self.tenant_id, 'entries': entries, 'more': len(rows) > limit,
                'next_after_id': entries[-1]['id'] if entries else after_id}

    def replay_deletion_journal(self, entries, *, apply=False, objects=None):
        """Re-apply authority deletions/withdrawals to a restored copy.

        A snapshot older than a deletion would otherwise resurrect the content.
        Deletions purge even if the copy's own (older) hold rows cover them: the
        authority already decided the deletion after evaluating its holds.
        """
        if type(apply) is not bool:
            raise ValueError('explicit_replay_apply_boolean')
        if not isinstance(entries, list) or len(entries) > 100000:
            raise ValueError('deletion_journal_entries_required')
        latest = {}
        for item in entries:
            if (not isinstance(item, dict) or not isinstance(item.get('source_id'), str)
                    or item.get('operation') not in ('withdraw', 'delete', 'correct', 'delete_held')):
                raise ValueError('invalid_deletion_journal_entry')
            current = latest.get(item['source_id'])
            latest[item['source_id']] = 'delete' if 'delete' in (current, item['operation']) else 'withdraw'
        counts = {'deleted': 0, 'hidden': 0, 'already': 0, 'absent': 0}
        with self.open() as state:
            known = {r['id']: r for r in state.db.execute(f'''SELECT s.id,s.active,{PURGED.format('s')} AS purged
                FROM enterprise_sources s WHERE s.tenant=? AND s.id=ANY(?::text[])''',
                                                           (self.tenant_id, list(latest)))}
        work = []
        for source_id, operation in sorted(latest.items()):
            row = known.get(source_id)
            if row is None:
                counts['absent'] += 1
            elif row['purged'] or (operation == 'withdraw' and not row['active']):
                counts['already'] += 1
            else:
                work.append((source_id, operation))
        report = {'kind': 'replay', 'dry_run': not apply, 'entries': len(entries), 'pending': len(work),
                  'provider_calls': 0} | counts
        if not apply:
            return report
        for source_id, operation in work:
            with self.delivery_lock(), self.open() as state, state.db:
                if operation == 'delete':
                    self._force_purge_tx(state.db, source_id)
                    counts['deleted'] += 1
                else:
                    self._hide_source_tx(state.db, source_id, 'withdraw')
                    counts['hidden'] += 1
        report.update(counts, objects=self._drain_bounded(objects, len(work)))
        report['run_id'] = self._record_run('replay', None, True, 'complete', report)
        return report

    def _hide_source_tx(self, db, source_id, operation):
        source = db.execute('SELECT * FROM enterprise_sources WHERE id=? FOR UPDATE', (source_id,)).fetchone()
        if source['active']:
            db.execute('UPDATE enterprise_sources SET active=0,source_version=source_version+1 WHERE id=?', (source_id,))
            db.execute('UPDATE memories SET active=0 WHERE id=?', (source_id,))
            db.execute('DELETE FROM memory_fts WHERE id=?', (source_id,))
            db.execute('INSERT INTO enterprise_deletion_journal(tenant,source_id,operation,created) VALUES(?,?,?,?)',
                       (source['tenant'], source_id, operation, time.time()))
        self._block_dependents_tx(db, source_id, 'withdraw')
        self._audit(db, {'tenant': source['tenant'], 'actor': 'operator'}, 'journal_replay', 'hidden', source_id)

    def _force_purge_tx(self, db, source_id):
        source = db.execute('SELECT * FROM enterprise_sources WHERE id=? FOR UPDATE', (source_id,)).fetchone()
        db.execute('UPDATE enterprise_sources SET active=0,source_version=source_version+1 WHERE id=?', (source_id,))
        db.execute('UPDATE memories SET active=0 WHERE id=?', (source_id,))
        db.execute('DELETE FROM memory_fts WHERE id=?', (source_id,))
        db.execute("INSERT INTO enterprise_deletion_journal(tenant,source_id,operation,created) VALUES(?,?,'delete',?)",
                   (source['tenant'], source_id, time.time()))
        self._block_dependents_tx(db, source_id, 'delete')
        self._audit(db, {'tenant': source['tenant'], 'actor': 'operator'}, 'journal_replay', 'deleted', source_id)


def active_holds(db):
    """Count unreleased holds; tenant retirement refuses to drop held data."""
    if not table_exists(db, 'enterprise_legal_holds'):
        return 0
    return int(db.execute('SELECT count(*) FROM enterprise_legal_holds WHERE released IS NULL').fetchone()[0])
