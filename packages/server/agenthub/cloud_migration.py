"""Explicit immutable SQLite-backup migration and same-authority rollback proof.

SQLite is only read as a selected migration input. Canonical runtime never opens
a SQLite corpus. A static table allowlist maps preserved columns directly into
the already migrated PostgreSQL schema; SQL statements are not translated.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import time

from agenthub.processing.knowledge import refresh_index, index_ready
from agenthub.processing.storage import columns
from agenthub.source_objects import object_key, verify


TABLES = (
 'backend_capture_gaps','backend_connections','backend_connector_items','backend_connectors',
 'backend_ingest_receipts','backend_jobs','backend_native_artifacts','backend_native_spans',
 'backend_observer_dependencies','backend_observers','backend_part_hashes','backend_parts','backend_provider_returns',
 'backend_processing_dependencies','backend_releases','backend_source_heads','backend_source_links',
 'backend_source_revisions','backend_worker_receipts','codex_backfill_events','codex_backfill_runs',
 'codex_backfill_sessions','consolidation_decisions','context_boundaries','context_receipts',
 'context_retained','context_state','contribution_reviews','counters','curation_decisions',
 'curation_episode_jobs','curation_episode_receipts','enterprise_audit','enterprise_credentials',
 'enterprise_delegations','enterprise_deletion_journal','enterprise_dependencies','enterprise_documents',
 'enterprise_lifecycle','enterprise_legacy_ids','enterprise_memberships','enterprise_model_inputs','enterprise_organizations',
 'enterprise_principals','enterprise_projects','enterprise_receipts','enterprise_sources',
 'episode_candidate_history','episode_candidates','events','health','historical_sources',
 'knowledge_document_members','knowledge_documents','knowledge_embedding_build','knowledge_embedding_state',
 'knowledge_embeddings','knowledge_generation_documents','knowledge_generation_state','knowledge_generations',
 'knowledge_relations','knowledge_revisions','knowledge_search_shadow_comparisons','knowledge_shadow_comparisons',
 'knowledge_support','knowledge_temporal_assertions','knowledge_temporal_evidence','knowledge_temporal_relations',
 'memories','memory_access','memory_candidates','memory_exclusions','memory_feedback','memory_metadata',
 'memory_outcome_support','memory_reviews','model_call_details','model_calls','observation_sources',
 'observer_jobs','observer_progress','observer_seen','offers','outbox','outcomes','remote_offers','source_event_metadata')

# Existing enterprise identity constraints are deliberately immediate. Import
# their parents first; deferred canonical constraints then validate at commit.
_IDENTITY_FIRST = ('enterprise_organizations','enterprise_principals','enterprise_projects',
    'enterprise_memberships','enterprise_credentials','enterprise_delegations',
    'enterprise_sources','enterprise_documents','enterprise_dependencies')
TABLES = _IDENTITY_FIRST + tuple(table for table in TABLES if table not in _IDENTITY_FIRST)
DERIVED = {'enterprise_schema','knowledge_temporal_schema','knowledge_index_state',
    'knowledge_index_rows','knowledge_index_build','memory_fts','knowledge_fts'}


def _canonical(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'binary_base64': base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _row_digest(rows):
    encoded = [json.dumps(_canonical(row), sort_keys=True, separators=(',', ':'), ensure_ascii=True) for row in rows]
    return hashlib.sha256('\n'.join(sorted(encoded)).encode()).hexdigest()


def _marker(store, ready, reason):
    path = store.home / 'restore-readiness.json'
    path.write_text(json.dumps({'ready_for_use': ready, 'reason': reason}, sort_keys=True))
    path.chmod(0o600)


def migrate_snapshot(snapshot, store, *, expected_sha256, objects, allow_missing_originals=False):
    """Preserve IDs/content/state from one tenant's immutable authorized backup.

    New credential expiry remains zero: old indefinite plugin credentials cannot
    silently become new expiring cloud credentials. Explicit enrollment is needed.
    Missing historical original binaries are a gap, never reconstructed originals.
    """
    from psycopg import sql
    snapshot = Path(snapshot).expanduser().resolve(strict=True)
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError('explicit_snapshot_checksum_required')
    if snapshot.stat().st_mode & 0o222:
        raise ValueError('immutable_snapshot_required')
    if Path(str(snapshot) + '-wal').exists() or Path(str(snapshot) + '-journal').exists():
        raise ValueError('closed_checkpointed_snapshot_required')
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    if before != expected_sha256:
        raise ValueError('snapshot_checksum_mismatch')
    source = sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True)
    source.row_factory = sqlite3.Row
    receipt_id = hashlib.sha256((store.tenant_id + before).encode()).hexdigest()
    locators = []; gaps = []; copied = {}; omitted = []
    _marker(store, False, 'migration_in_progress')
    try:
        integrity = source.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise ValueError('snapshot_integrity_failed')
        present = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(present - set(TABLES)):
            if table.startswith(('memory_fts_', 'knowledge_fts_', 'sqlite_')) or table in DERIVED:
                omitted.append({'table':table,'reason':'rebuildable_index_or_backend_schema_receipt'})
                continue
            # Do not discard a newly introduced authoritative table silently.
            escaped = table.replace('"', '""')
            count = source.execute('SELECT count(*) FROM "' + escaped + '"').fetchone()[0]
            if count:
                raise ValueError('unmapped_nonempty_snapshot_table:' + table)
            omitted.append({'table':table,'reason':'unmapped_empty_table'})
        if 'enterprise_organizations' not in present:
            raise ValueError('enterprise_snapshot_required')
        tenants = {r[0] for r in source.execute('SELECT id FROM enterprise_organizations')}
        if tenants != {store.tenant_id}:
            raise ValueError('single_selected_tenant_snapshot_required')
        # Externalize only retained source representations present in the backup.
        # No path or URL from the backup is opened or requested.
        for row in source.execute('SELECT id,source_version,active FROM enterprise_sources'):
            structured = (source.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?', (row['id'],)).fetchone()
                if 'backend_source_revisions' in present else None)
            memory = source.execute('SELECT body FROM memories WHERE id=?', (row['id'],)).fetchone()
            raw = (structured[0] if structured else memory[0] if memory else '')
            if not raw or raw == '{}':
                omitted.append({'source_id': row['id'], 'reason': 'tombstoned_or_missing_retained_payload'})
                continue
            representation = 'retained_structured_source_json' if structured else 'retained_sqlite_source_text'
            raw = raw.encode('utf-8'); digest = hashlib.sha256(raw).hexdigest()
            key = object_key(store.tenant_id, row['id'], str(row['source_version']), digest)
            info = objects.put(key, io.BytesIO(raw), expected_sha256=digest)
            verify(objects.head(key), expected_sha256=digest, expected_length=len(raw))
            complete = bool(structured and json.loads(structured[0]).get('source_type') in {'agent','conversation','record'})
            locators.append({'source_id': row['id'], 'key': key, 'sha256': digest, 'length': info.length,
                'representation': representation, 'original_complete': complete})
        if 'backend_native_artifacts' in present:
            gaps = [{'source_id': r[0], 'reason': 'historical_original_bytes_not_present_in_sqlite_backup'}
                for r in source.execute('SELECT DISTINCT source_id FROM backend_native_artifacts')]
        if gaps and not allow_missing_originals:
            raise ValueError('historical_original_gap_requires_explicit_disposition')
        with store.open() as state, state.db:
            db = state.db
            if db.execute('SELECT 1 FROM enterprise_sources LIMIT 1').fetchone() or db.execute('SELECT 1 FROM knowledge_documents LIMIT 1').fetchone() or db.execute('SELECT 1 FROM enterprise_organizations LIMIT 1').fetchone():
                raise ValueError('empty_migration_target_required')
            db.execute('SET CONSTRAINTS ALL DEFERRED')
            for table in TABLES:
                if table not in present:
                    omitted.append({'table': table, 'reason': 'not_present_in_selected_schema'})
                    continue
                names = [r[1] for r in source.execute('PRAGMA table_info(' + table + ')')]
                target_names = columns(db, table)
                if set(names) - set(target_names):
                    raise ValueError('migration_column_not_supported:' + table)
                rows = [{name: r[name] for name in names} for r in source.execute('SELECT * FROM ' + table)]
                query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),
                    sql.SQL(',').join(map(sql.Identifier, names)), sql.SQL(',').join(sql.Placeholder() for _ in names))
                if rows:
                    with db.connection.cursor() as cursor:
                        cursor.executemany(query, [tuple(row[name] for name in names) for row in rows])
                target = [dict(r) for r in db.connection.execute(sql.SQL('SELECT {} FROM {}').format(
                    sql.SQL(',').join(map(sql.Identifier, names)), sql.Identifier(table)))]
                if len(target) != len(rows) or _row_digest(target) != _row_digest(rows):
                    raise ValueError('migration_content_reconciliation_failed:' + table)
                copied[table] = {'count': len(rows), 'sha256': _row_digest(rows)}
                if 'id' in names:
                    serial = db.execute('SELECT pg_get_serial_sequence(?,?)', (table, 'id')).fetchone()[0]
                    if serial:
                        largest = max((row['id'] for row in rows), default=0)
                        db.execute('SELECT setval(?::regclass,?,?)', (serial, max(1, largest), bool(largest)))
            for item in locators:
                db.execute('INSERT INTO cloud_migrated_source_objects VALUES(?,?,?,?,?,?)',
                    (item['source_id'], item['key'], item['sha256'], item['length'], item['representation'], int(item['original_complete'])))
            refresh_index(db)
            if not index_ready(db):
                raise ValueError('migrated_index_not_ready')
            receipt = {'id': receipt_id, 'source_sha256': before, 'tenant': store.tenant_id,
                'status': 'migrated_with_original_gaps' if gaps else 'migrated', 'counts': copied,
                'objects': locators, 'exclusions': omitted, 'original_gaps': gaps,
                'credential_disposition': 'preserved_expiry_zero_requires_explicit_cloud_enrollment',
                'source_models_called': 0, 'sqlite_runtime_created': False}
            db.execute('INSERT INTO cloud_migration_receipts VALUES(?,?,?,?,?,?,?,?)',
                (receipt_id, before, store.tenant_id, receipt['status'], json.dumps(copied), json.dumps(locators),
                 json.dumps({'exclusions': omitted, 'original_gaps': gaps}), time.time()))
            if hashlib.sha256(snapshot.read_bytes()).hexdigest() != before:
                raise ValueError('snapshot_changed_during_migration')
        _marker(store, True, receipt['status'])
        return receipt
    except Exception:
        _marker(store, False, 'migration_failed_reconciliation_required')
        raise
    finally:
        source.close()


def rollback_same_authority(store, *, migration_id, previous_build, expected_database_identity):
    """Record/test a code rollback while retaining all accepted database writes.

    It cannot restore a SQLite snapshot or switch a profile. The operator still
    must install a PostgreSQL-compatible previous application image.
    """
    if not previous_build or not expected_database_identity:
        raise ValueError('explicit_rollback_build_and_database_required')
    with store.open() as state:
        db = state.db
        database = db.execute('SELECT current_database()').fetchone()[0]
        if database != expected_database_identity:
            raise ValueError('rollback_database_identity_changed')
        receipt = db.execute('SELECT source_sha256 FROM cloud_migration_receipts WHERE id=?', (migration_id,)).fetchone()
        if not receipt:
            raise ValueError('unknown_migration_receipt')
        lifecycle = [dict(row) for row in db.execute('SELECT key,target_id,operation,result FROM enterprise_lifecycle ORDER BY key')]
        sources = [dict(row) for row in db.execute('SELECT id,active,source_version,policy_version,payload_hash FROM enterprise_sources ORDER BY id')]
        return {'strategy': 'previous_postgresql_application_same_authority', 'previous_build': previous_build,
            'database': database, 'migration_id': migration_id, 'post_migration_sources': len(sources),
            'source_state_sha256': _row_digest(sources), 'lifecycle_sha256': _row_digest(lifecycle),
            'lifecycle_count': len(lifecycle), 'sqlite_restored': False,
            'installed_previous_build': False}
