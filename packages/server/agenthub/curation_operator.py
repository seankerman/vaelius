"""Finite canonical curation phases and provider-free saved-output review.

Original private campaigns remain frozen. This operator uses their existing
authority and ledgers; it does not seed data, activate a generation, or enroll
new sources. Review is a transaction rollback, never an alternate writer.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import time

from agenthub.backend_worker import Worker
from agenthub.cloud_local import runtime, worker_config


class _PreviewRollback(Exception):pass


@contextmanager
def _review_transaction(db,persist):
    try:
        with db:
            yield
            if not persist:raise _PreviewRollback()
    except _PreviewRollback:pass


def saved_review(store, config, job_id, *, persist=False):
    from agenthub.processing.accounting import job_context
    from agenthub.processing.episode_pipeline import _run_durable
    cfg=dict(config,_pipeline_phase='extract',_observer_reconstruct=True)
    worker=Worker.__new__(Worker);worker.store=store;worker.config=cfg
    with store.delivery_lock(),store.open() as state:
        transaction=_review_transaction(state.db,persist)
        if getattr(state.db,'dialect',None)!='postgres' and not persist:
            raise ValueError('provider_free_review_requires_rollback_transaction')
        with transaction:
            job=state.db.execute('SELECT * FROM backend_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or (job['status']=='running' and job['lease_until']>time.time()):
                raise ValueError('review_job_unavailable')
            observer=state.db.execute('SELECT * FROM backend_observers WHERE id=?',(job['observer_id'],)).fetchone()
            connection=state.db.execute('SELECT * FROM backend_connections WHERE id=?',(observer['connection'],)).fetchone()
            store.processing_identity(state.db,connection)
            row,current,previous=worker._context(state,dict(job),deadline=time.monotonic()+60)
            before=row['progress'];old_stage=row['stage']
            def no_provider(*args,**kwargs):raise ValueError('saved_extraction_incomplete')
            def authorized(db,episode,phase):
                store.processing_identity(db,connection)
                dependencies=db.execute('''SELECT s.active,s.policy_version,d.policy_version expected
                    FROM backend_processing_dependencies d LEFT JOIN enterprise_sources s ON s.id=d.source_id
                    WHERE d.episode_job=?''',(row['id'],)).fetchall()
                if any(not v['active'] or v['policy_version']!=v['expected'] for v in dependencies):
                    raise ValueError('observer_dependency_changed')
            authorized(state.db,row,'before')
            _run_durable(state,cfg,row,current,no_provider,job_context('episode_curation',row,current),
                context_sources=previous,guard=authorized)
            progress=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',(row['id'],)).fetchone()[0])
            if persist:
                # Existing completed installs remain completed; migration only
                # adds a validated first-pass envelope, without reinstalling.
                state.db.execute('UPDATE curation_episode_jobs SET stage=? WHERE id=?',(old_stage,row['id']))
            return {'job_id':job_id,'episode_job':row['id'],'extraction':progress['extraction'],
                'profile':worker.context_profile,'before_progress_sha256':hashlib.sha256(before.encode()).hexdigest(),
                'persisted':persist,'new_model_attempts':0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','review','migrate-saved','extract','consolidate','resume','pause'])
    parser.add_argument('--profile',required=True);parser.add_argument('--tenant',default='acme')
    parser.add_argument('--job-id');parser.add_argument('--episode-id',action='append',default=[])
    parser.add_argument('--output');parser.add_argument('--phase',choices=['extract','consolidate','full'],default='full')
    parser.add_argument('--live',action='store_true');parser.add_argument('--ledger')
    parser.add_argument('--max-calls',type=int,default=6);parser.add_argument('--max-retries',type=int,default=2)
    parser.add_argument('--max-input-tokens',type=int,default=2_000_000)
    parser.add_argument('--max-jobs',type=int,default=1);parser.add_argument('--max-seconds',type=int,default=300)
    args=parser.parse_args(argv);os.umask(0o077)
    out=Path(args.output).expanduser().resolve() if args.output else None
    if out and out.exists():raise ValueError('operator_receipt_exists')
    profile=Path(args.profile).expanduser().resolve();_,registry=runtime(profile);store=registry.resolve(args.tenant)
    if args.action=='status':
        worker=Worker.__new__(Worker);worker.store=store
        value=worker.status()
    elif args.action in {'review','migrate-saved'}:
        if not args.job_id or not args.output or args.live:parser.error('saved review requires --job-id and --output, without --live')
        value=saved_review(store,worker_config(profile,provider_free=False),args.job_id,persist=args.action=='migrate-saved')
    elif args.action=='pause':
        from agenthub.cloud_ops import Meter
        with store.open() as state:admission=dict(state.db.execute('SELECT * FROM cloud_admission WHERE tenant=?',(store.tenant_id,)).fetchone())
        Meter(store).configure(max_parallel=admission['max_parallel'],max_attempts=admission['max_attempts'],enabled=False)
        value={'processing_enabled':False,'inflight_not_cancelled':True,'graceful_drain':True}
    else:
        if not args.episode_id:parser.error('finite phases require explicit --episode-id selection')
        if not (args.max_calls>=1 and args.max_retries>=0 and args.max_jobs>=1 and args.max_seconds>=1):
            parser.error('finite operator bounds exceeded')
        phase=args.phase if args.action=='resume' else args.action
        cfg=dict(worker_config(profile,provider_free=False),_pipeline_phase=phase)
        def no_provider(*args,**kwargs):raise ValueError('provider_free_saved_outputs_only')
        worker=Worker(store,cfg,runner=None if args.live else no_provider,live=args.live,ledger_path=args.ledger)
        previous=signal.signal(signal.SIGTERM,lambda *_:setattr(worker,'stop',True))
        try:
            value=worker.run(max_jobs=args.max_jobs,max_calls=args.max_calls,max_retries=args.max_retries,
                max_seconds=args.max_seconds,max_input_tokens=args.max_input_tokens,refresh_queue=False,episode_job_ids=args.episode_id)
        finally:signal.signal(signal.SIGTERM,previous)
    if args.output:
        out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with os.fdopen(os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            json.dump(value,stream,sort_keys=True,indent=2);stream.write('\n')
        print(json.dumps({'receipt':str(out),'action':args.action,'new_model_attempts':value.get('new_model_attempts',value.get('calls',0))}))
    else:print(json.dumps(value))


if __name__=='__main__':main()
