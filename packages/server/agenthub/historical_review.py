"""Explicit, count-only review of an isolated historical curation generation.

This operator command never calls a model. Finalization preserves failed turns
as unprocessed gaps before making only a fully drained generation searchable.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

from agenthub.processing.episode_pipeline import (
    activate_generation, generation_status, skip_held_job,
)


def review_generation(store, generation, *, allowed_connections, finalize=False):
    if not generation or not allowed_connections or len(set(allowed_connections)) != len(allowed_connections):
        raise ValueError('historical_review_scope')
    store.require_ready()
    with store.open() as state:
        db = state.db
        rows = [dict(row) for row in db.execute('''
            SELECT e.id AS episode_id,e.source_ids,e.status AS episode_status,e.error AS episode_error,e.progress,
                   j.status AS worker_status,j.last_error AS worker_error,
                   o.connection AS connection
            FROM curation_episode_jobs e
            LEFT JOIN backend_jobs j ON j.episode_job=e.id
            LEFT JOIN backend_observers o ON o.id=j.observer_id
            WHERE e.generation_id=? ORDER BY e.created,e.id
        ''', (generation,))]
        for row in rows:
            if row['worker_status'] is not None:
                continue
            # An incomplete captured turn is held before a model worker is
            # queued. Its original source revisions still establish scope.
            try:
                source_ids = json.loads(row['source_ids'])
                connections = {db.execute('SELECT connection FROM backend_source_revisions '
                    'WHERE source_id=?', (source_id,)).fetchone()[0] for source_id in source_ids}
            except (TypeError,ValueError,KeyError,IndexError):
                raise ValueError('historical_review_scope') from None
            if not source_ids or len(connections) != 1:
                raise ValueError('historical_review_scope')
            row['connection'] = next(iter(connections))
        if (not rows or len({row['episode_id'] for row in rows}) != len(rows)
                or any(row['connection'] not in allowed_connections for row in rows)):
            raise ValueError('historical_review_scope')
        if any(row['worker_status'] not in ('complete','held') and not (
                row['worker_status'] is None and row['episode_status'] in ('held','unprocessed'))
                for row in rows):
            raise ValueError('historical_review_jobs_not_drained')
        if any(row['episode_status'] not in ('done','no_learning','withdrawn')
               for row in rows if row['worker_status']=='complete'):
            raise ValueError('historical_review_complete_mismatch')
        held = [row for row in rows if row['worker_status'] in ('held',None)]
        if any(row['episode_status'] not in ('pending','held','unprocessed')
               or not (row['episode_error'] or row['worker_error']) for row in held):
            raise ValueError('historical_review_hold_mismatch')
        reasons = Counter(row['episode_error'] or row['worker_error'] for row in held)
        processed=[row for row in rows if row['worker_status']=='complete']
        partial=sum(json.loads(row['progress'] or '{}').get('coverage',{}).get(
            'source_capture',{}).get('completion')=='partial' for row in processed)
        marked = 0
        if finalize:
            for row in held:
                if row['episode_status']=='unprocessed':
                    continue
                if row['episode_status']=='pending':
                    with db:
                        db.execute('''UPDATE curation_episode_jobs SET status='held',
                            error=coalesce(error,?),updated=? WHERE id=? AND status='pending' ''',
                            (row['worker_error'],time.time(),row['episode_id']))
                skip_held_job(db,row['episode_id'])
                marked += 1
            status = activate_generation(db,generation)
            # Enterprise registration deliberately excludes building generations.
            # Activation alone therefore leaves valid claims invisible to every
            # authorized search until their source links are registered.
            # Scope registration to this generation and commit each document.
            # A corpus-wide INSERT can exceed the statement timeout while its
            # provenance links are checked. Committed rows let a restart resume
            # without repeating activation or discarding the original gaps.
            documents = db.execute('''SELECT gd.document_id
                FROM knowledge_generation_documents gd
                JOIN knowledge_documents d ON d.document_id=gd.document_id
                LEFT JOIN enterprise_documents ed ON ed.id=d.document_id
                WHERE gd.generation_id=? AND d.lifecycle='active'
                AND (ed.id IS NULL OR ed.revision!=d.active_revision_id
                    OR ed.blocked_reason='building' OR NOT EXISTS(
                        SELECT 1 FROM enterprise_dependencies x
                        WHERE x.document_id=d.document_id))
                ORDER BY gd.document_id''', (generation,)).fetchall()
            for document in documents:
                with db:
                    store.refresh_documents(db=db, document_id=document['document_id'])
            missing = db.execute('''SELECT count(*) FROM knowledge_generation_documents gd
                JOIN knowledge_documents d ON d.document_id=gd.document_id
                LEFT JOIN enterprise_documents ed ON ed.id=d.document_id
                WHERE gd.generation_id=? AND d.lifecycle='active'
                AND (ed.id IS NULL OR NOT EXISTS(SELECT 1 FROM enterprise_dependencies x
                    WHERE x.document_id=d.document_id))''',(generation,)).fetchone()[0]
            if missing:
                raise ValueError('historical_review_registration_incomplete')
        else:
            status = generation_status(db,generation)
        return {'generation': generation, 'status': status['status'],
                'jobs': dict(Counter(row['worker_status'] or 'capture_held' for row in rows)),
                'held_reasons': dict(sorted(reasons.items())),
                'processed_partial_capture_jobs':partial,
                'processed_fully_captured_jobs':len(processed)-partial,
                'gaps_marked': marked, 'knowledge_documents': status['documents'],
                'provider_calls': 0, 'private_source_text_in_report': False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--tenant',required=True)
    parser.add_argument('--generation',required=True)
    parser.add_argument('--connection',action='append',required=True)
    parser.add_argument('--finalize',action='store_true')
    args=parser.parse_args(argv)
    from agenthub.cloud_local import runtime, receipt
    _,registry=runtime(Path(args.profile).expanduser().resolve(strict=True))
    result=review_generation(registry.resolve(args.tenant),args.generation,
                             allowed_connections=args.connection,finalize=args.finalize)
    print(json.dumps(receipt(args.profile,'historical-review',result),sort_keys=True))


if __name__=='__main__':
    main()
