"""Durable, bounded backend observers using the canonical episode pipeline.

One continuing curator per tenant/connection/conversation/policy/generation.
No SQLite write transaction or delivery lock is held over inference. Every
installed result and session checkpoint commits under a valid fencing token.
"""
from agenthub.source_objects import ObjectMissing, ObjectCorrupt
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from agenthub.processing.episode_pipeline import create_jobs, run_once, sources_for_job
from agenthub.processing.evaluation_budget import reserve, finish
from agenthub.cloud_execution import execution_from_config, execution_identity, execution_model, CodexExecution


def initialize(db):
    db.require_schema()


def _hash(value): return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def _session_checkpoint_matches(directory, handle):
    if not handle:
        return False
    path=directory/'usage.json'
    try:
        if path.stat().st_size>16384:
            return False
        checkpoint=json.loads(path.read_text())
        return (checkpoint.get('session_id')==handle and isinstance(checkpoint.get('cumulative'),dict)
                and all(type(v) is int and v>=0 for v in checkpoint['cumulative'].values()))
    except (OSError,ValueError,AttributeError):
        return False


def _is_model_retry(db, job, purpose, *, request_hash=None, reviewed_retry=False):
    """Charge retry budget for a repeated dispatch, not a prior job claim.

    A deterministic pre-dispatch hold has no billed call to retry. A crash after
    dispatch but before its receipt remains uncertain and conservatively counts.
    """
    if job['attempts'] <= 1:
        return False
    previous = db.execute('''SELECT 1 FROM backend_worker_receipts
        WHERE job_id=? AND purpose=? AND attempt_id IS NOT NULL AND is_live=1 LIMIT 1''',
        (job['id'], purpose)).fetchone()
    last = job.get('dispatch_attempt')
    if last and not db.execute('SELECT 1 FROM backend_worker_receipts WHERE attempt_id=? LIMIT 1',
                               (last,)).fetchone():
        return True
    if request_hash is None:
        return bool(previous)
    if reviewed_retry and previous:
        return True
    # A bounded continuation may dispatch the next stage for the first time.
    # Successful earlier stages are saved returns, not retries of this request.
    # Failed/uncertain dispatches remain conservative until explicit recovery.
    if db.execute('''SELECT 1 FROM backend_worker_receipts WHERE job_id=?
            AND purpose=? AND is_live=1 AND status!='returned' LIMIT 1''',
            (job['id'],purpose)).fetchone():
        return True
    return bool(db.execute('''SELECT 1 FROM backend_provider_returns
        WHERE job_id=? AND purpose=? AND request_hash=? LIMIT 1''',
        (job['id'],purpose,request_hash)).fetchone())


class Worker:
    def __init__(self,store,config,*,runner=None,live=True,ledger_path=None):
        store.require_ready()
        self.store=store;self.config=dict(config);self.runner=runner;self.live=live;self.ledger_path=ledger_path
        self.config['observer']=dict(self.config.get('observer',{}))
        self.config['observer']['model']=execution_model(self.config)
        self.execution_key=execution_identity(self.config)
        if self.live and self.runner is None:self.runner=execution_from_config(self.config)
        allowed=config.get('backend_worker',{}).get('allowed_connections')
        if allowed is not None and (not isinstance(allowed,list) or not 1<=len(allowed)<=100 or any(not isinstance(v,str) or not v or len(v)>128 for v in allowed)):raise ValueError('invalid_worker_connection_scope')
        self.allowed_connections=allowed
        partial=config.get('backend_worker',{}).get('allow_partial_capture',False)
        if type(partial) is not bool:raise ValueError('invalid_partial_capture_option')
        if config.get('knowledge_backend',{}).get('mode')!='enterprise_local': raise ValueError('enterprise_worker_config_required')
        if config.get('episode_curation',{}).get('policy')!='durable_memory': raise ValueError('continuous_worker_requires_canonical_durable_policy')
        if live and not ledger_path and config.get('backend_execution',{}).get('kind','codex')=='codex':
            raise ValueError('shared_campaign_ledger_required')
        if live and ledger_path:
            accounting=Path(config.get('accounting_home',Path.home()/'.local/share/agentnetwork')).resolve()
            if Path(ledger_path).resolve()!=accounting/'knowledge-evaluation.sqlite' or not Path(ledger_path).is_file():raise ValueError('existing_shared_campaign_ledger_required')
        if not live and runner is None: self.dry_run=True
        else: self.dry_run=False
        with self.store.open() as state: initialize(state.db)
        if getattr(self.store,'dsn',None):
            with self.store.open() as state,state.db:
                state.db.execute('INSERT INTO cloud_admission VALUES(?,?,?,1) ON CONFLICT(tenant) DO NOTHING',
                    (self.store.tenant_id,4,100000))
        config_path=self.store.home/'config.json'
        temporary=config_path.with_name('config-'+uuid.uuid4().hex+'.new')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w') as output:output.write(json.dumps(self.config,sort_keys=True))
            temporary.replace(config_path)
        finally:temporary.unlink(missing_ok=True)
        self.calls=0;self.retries=0;self.input_tokens=0;self.stop=False

    def prepare(self):
        if not getattr(self.store,'curation_enabled',False):return 0
        from agenthub.processing.durable_memory import VERSION
        queued=0
        with self.store.open() as state,state.db:
            db=state.db
            scopes=[r[0] for r in db.execute('''SELECT DISTINCT s.internal_project FROM enterprise_sources s
                JOIN backend_source_revisions r ON r.source_id=s.id
                JOIN backend_connections c ON c.id=r.connection WHERE s.active=1 AND c.active=1
                AND c.permission_observed+c.freshness_seconds>?'''+((' AND c.id IN ('+','.join('?' for _ in self.allowed_connections)+')') if self.allowed_connections is not None else ''),
                (time.time(),*(self.allowed_connections or [])))]
            for scope in scopes: create_jobs(state,self.config,project_scope=scope,connection_ids=self.allowed_connections)
            rows=db.execute('SELECT * FROM curation_episode_jobs WHERE generation_id=? AND status=\'pending\' AND next_attempt<=? ORDER BY created,id',
                (self.config['episode_curation']['generation_id'],time.time())).fetchall()
            for row in rows:
                ids=json.loads(row['source_ids']);source_ids=[];connections=set();conversation=''
                for ident in ids:
                    link=db.execute('SELECT * FROM backend_source_revisions WHERE source_id=?',(ident,)).fetchone()
                    if not link: break
                    connections.add(link['connection']);source_ids.append(ident)
                    conversation=json.loads(link['payload'])['conversation']
                if len(source_ids)!=len(ids) or len(connections)!=1: continue
                connection=next(iter(connections))
                if self.allowed_connections is not None and connection not in self.allowed_connections:continue
                policy=db.execute('SELECT * FROM backend_connections WHERE id=?',(connection,)).fetchone()
                if not policy or not policy['active']: continue
                if not self.store._connection_executor_authorized(db,policy):continue
                # Explicit gaps in the turn prevent describing it as completely captured.
                gap_ids=[v[0] for v in db.execute("SELECT m.id FROM memories m JOIN backend_source_revisions r ON r.source_id=m.id WHERE m.project=? AND m.session=? AND m.turn=? AND m.kind='gap' AND m.active=1 AND r.connection=? AND r.disposition IN ('unsupported','incomplete')",(row['project'],row['session'],row['turn'],connection))]
                payloads=[json.loads(db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?',(ident,)).fetchone()[0]) for ident in ids]
                expected={external for payload in payloads if payload['event']['complete'] for external in payload['event']['expected_events']}
                arrived={r[0] for r in db.execute('''SELECT r.external_id FROM backend_source_revisions r
                    JOIN memories m ON m.id=r.source_id WHERE m.session=? AND m.turn=? AND m.active=1
                    AND r.connection=?''',(row['session'],row['turn'],connection))}
                if gap_ids or not expected<=arrived:
                    if not self.config.get('backend_worker',{}).get('allow_partial_capture',False):
                        db.execute("UPDATE curation_episode_jobs SET status='held',error='incomplete_capture' WHERE id=?",(row['id'],));continue
                    # Processing may finish for readable evidence, but the source
                    # capture remains partial. Never turn an absent event/pixel
                    # into evidence or remove its original gap record.
                    progress=json.loads(row['progress'] or '{}')
                    progress['capture_diagnostics']={'completion':'partial',
                        'gap_source_ids':sorted(gap_ids),'missing_expected_events':sorted(expected-arrived)}
                    db.execute('UPDATE curation_episode_jobs SET progress=? WHERE id=?',(json.dumps(progress),row['id']))
                    db.executemany('INSERT INTO backend_processing_dependencies VALUES(?,?,?) ON CONFLICT(episode_job,source_id) DO UPDATE SET policy_version=excluded.policy_version',
                        [(row['id'],ident,db.execute('SELECT policy_version FROM enterprise_sources WHERE id=?',(ident,)).fetchone()[0]) for ident in gap_ids])
                permission_hash=_hash([connection,policy['policy_version'],policy['reader_ids'],row['project']])
                observer=_hash([policy['tenant'],connection,conversation,row['project'],row['generation_id']])
                prior=db.execute('SELECT * FROM backend_observers WHERE id=?',(observer,)).fetchone()
                model=self.config['observer']['model']
                if prior and (prior['status']=='invalidated' or prior['permission_hash']!=permission_hash or prior['model']!=model or prior['prompt_version']!=VERSION or prior['execution_key']!=self.execution_key):
                    db.execute("UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,observer_epoch=observer_epoch+1,status='reconstruct',permission_hash=?,model=?,prompt_version=?,execution_key=? WHERE id=?",(permission_hash,model,VERSION,self.execution_key,observer))
                    db.execute('DELETE FROM backend_observer_dependencies WHERE observer_id=?',(observer,))
                db.execute('''INSERT INTO backend_observers
                    (id,tenant,connection,conversation,internal_project,generation,permission_hash,model,prompt_version,updated,execution_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',(observer,policy['tenant'],connection,conversation,row['project'],row['generation_id'],permission_hash,model,VERSION,time.time(),self.execution_key))
                job=_hash([row['id'],row['source_hash']])
                now=time.time()
                # Adopting a pre-worker partial job cannot certify its older
                # model inputs. Preserve the conservative legacy graph.
                progress=json.loads(row['progress'] or '{}')
                fresh=not any(progress.get(key) for key in ('stage_outputs','extraction','resolutions','failed_stage_outputs'))
                fresh=fresh and not db.execute('SELECT 1 FROM enterprise_model_inputs WHERE job_id=? LIMIT 1',(row['id'],)).fetchone()
                queued+=db.execute('INSERT INTO backend_jobs(id,episode_job,observer_id,source_hash,created,updated,dependency_snapshot_complete) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (job,row['id'],observer,row['source_hash'],now,now,int(fresh))).rowcount
        return queued

    def claim(self,*,lease_seconds=240,episode_job_ids=None):
        if not 1<=lease_seconds<=600:raise ValueError('invalid_worker_lease')
        if episode_job_ids is not None and (not isinstance(episode_job_ids,list) or
                not 1<=len(episode_job_ids)<=1000 or any(not isinstance(v,str) or not v or len(v)>128 for v in episode_job_ids)):
            raise ValueError('invalid_worker_episode_scope')
        with self.store.open() as state,state.db:
            db=state.db
            if getattr(db,'dialect',None)=='postgres':db.begin_write()
            else:db.execute('BEGIN IMMEDIATE')
            now=time.time()
            row=db.execute('''SELECT j.* FROM backend_jobs j JOIN backend_observers o ON o.id=j.observer_id
                WHERE (j.status='pending' OR j.status=? OR (j.status='running' AND j.lease_until<?))
                AND o.status!='invalidated'
                AND o.generation=?
                AND NOT EXISTS(SELECT 1 FROM backend_jobs busy WHERE busy.observer_id=j.observer_id
                    AND busy.status='running' AND busy.lease_until>=?)'''+
                ((' AND o.connection IN ('+','.join('?' for _ in self.allowed_connections)+')') if self.allowed_connections is not None else '')+
                ((' AND j.episode_job IN ('+','.join('?' for _ in episode_job_ids)+')') if episode_job_ids is not None else '')+
                ' ORDER BY j.created,j.id LIMIT 1' +
                (' FOR UPDATE OF o SKIP LOCKED' if getattr(db,'dialect',None)=='postgres' else ''),
                ('extraction_ready' if self.config.get('_pipeline_phase')=='consolidate' else 'pending',now,self.config['episode_curation']['generation_id'],now,*(self.allowed_connections or []),
                 *(episode_job_ids or []))).fetchone()
            if not row:return None
            fence=uuid.uuid4().hex;recovery=''
            if row['status']=='running':
                recovery='rebuild_uncertain_session'
                db.execute("UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,observer_epoch=observer_epoch+1,status='reconstruct' WHERE id=?",(row['observer_id'],))
                db.execute("UPDATE curation_episode_jobs SET status='pending',error=NULL,next_attempt=0 WHERE id=? AND status NOT IN ('done','no_learning')",(row['episode_job'],))
            db.execute("UPDATE backend_jobs SET status='running',fence=?,lease_until=?,attempts=attempts+1,recovery=?,updated=? WHERE id=?",(fence,now+lease_seconds,recovery,now,row['id']))
            return dict(db.execute('SELECT * FROM backend_jobs WHERE id=?',(row['id'],)).fetchone())

    def guard(self,db,job):
        if getattr(db,'dialect',None)=='postgres':
            policy_key=int.from_bytes(hashlib.sha256(self.store.tenant_id.encode()).digest()[:8],'big',signed=True)
            # Independent observers read current policy concurrently. Lifecycle
            # writers retain the exclusive delivery lock on this same key.
            db.execute('SELECT pg_advisory_xact_lock_shared(?)',(policy_key,))
        row=db.execute('SELECT * FROM backend_jobs WHERE id=?'+(' FOR UPDATE' if getattr(db,'dialect',None)=='postgres' else ''),(job['id'],)).fetchone()
        observer=db.execute('SELECT * FROM backend_observers WHERE id=?',(job['observer_id'],)).fetchone()
        if not row or row['status']!='running' or row['fence']!=job['fence'] or row['lease_until']<time.time():raise ValueError('stale_worker_fence')
        if not observer or observer['status']=='invalidated':raise ValueError('observer_context_invalidated')
        if observer['execution_key']!=self.execution_key:raise ValueError('observer_execution_changed')
        connection=db.execute('SELECT * FROM backend_connections WHERE id=?',(observer['connection'],)).fetchone()
        if (not connection or not connection['active'] or
            connection['permission_observed']+connection['freshness_seconds']<time.time()):raise ValueError('observer_permission_expired')
        if not self.store._connection_executor_authorized(db,connection):raise ValueError('observer_enrollment_or_delegation_revoked')
        owner=db.execute('SELECT active FROM enterprise_principals WHERE tenant=? AND id=?',(connection['tenant'],connection['owner'])).fetchone()
        membership=db.execute('SELECT active FROM enterprise_memberships WHERE tenant=? AND project=? AND principal=?',(connection['tenant'],connection['project'],connection['owner'])).fetchone()
        if not owner or not owner[0] or not membership or not membership[0]:raise ValueError('observer_owner_revoked')
        fingerprint=_hash([connection['id'],connection['policy_version'],connection['reader_ids'],observer['internal_project']])
        if observer['permission_hash']!=fingerprint:raise ValueError('observer_policy_changed')
        dependencies=db.execute('''SELECT d.source_id,d.policy_version,s.active,s.policy_version current_version
            FROM backend_processing_dependencies d LEFT JOIN enterprise_sources s ON s.id=d.source_id
            WHERE d.episode_job=?''',(job['episode_job'],)).fetchall()
        if any(not dep['active'] or dep['policy_version']!=dep['current_version'] for dep in dependencies):raise ValueError('observer_dependency_changed')
        segments=getattr(self.store,'conversation_segments',None)
        if segments is not None:
            segments.verify_sources(db,[dep['source_id'] for dep in dependencies])

    def mark_dispatch(self,job,attempt):
        with self.store.open() as state,state.db:
            self.guard(state.db,job)
            state.db.execute('UPDATE backend_jobs SET dispatch_attempt=?,pending_result=NULL,updated=? WHERE id=?',(attempt,time.time(),job['id']))

    def _context(self,state,job,*,deadline=None):
        from agenthub.observer_context import load_context
        row,current,previous,self.context_profile=load_context(state,job,self.config,deadline=deadline)
        return row,current,previous

    def run(self,*,max_jobs=1,max_calls=12,max_retries=3,max_seconds=120,max_input_tokens=100_000,
            refresh_queue=True,episode_job_ids=None):
        if not getattr(self.store,'curation_enabled',False):
            return {'completed':0,'calls':0,'curation_enabled':False}
        if (any(type(value) is not int for value in (max_jobs,max_calls,max_retries,max_seconds,max_input_tokens)) or
                max_jobs<1 or max_calls<1 or max_retries<0 or max_seconds<1 or max_input_tokens<100 or
                type(refresh_queue) is not bool): raise ValueError('invalid_worker_bounds')
        if refresh_queue:self.prepare()
        start=time.monotonic();completed=0;extracted=0;processed=0;self.calls=0;self.retries=0;self.input_tokens=0
        if self.dry_run:return {'dry_run':True,'completed':0,'calls':0,'status':self.status()}
        for _ in range(max_jobs):
            if self.stop or time.monotonic()-start>=max_seconds or self.calls>=max_calls:break
            job=self.claim(lease_seconds=min(600,max(240,int(max_seconds))),episode_job_ids=episode_job_ids);
            if not job:break
            processed+=1
            try:
                with self.store.open() as state:
                    row,current,previous=self._context(state,job,deadline=start+max_seconds)
                    self.guard(state.db,job)
                    observer=dict(state.db.execute('SELECT * FROM backend_observers WHERE id=?',(job['observer_id'],)).fetchone())
            except (ValueError,RuntimeError,ObjectMissing,ObjectCorrupt) as exc:
                # Preparation is inside the finite wave, and a slow/invalid
                # conversation cannot spin through all remaining job claims.
                with self.store.open() as state,state.db:
                    state.db.execute("UPDATE backend_jobs SET status='held',last_error=?,lease_until=0,updated=? WHERE id=? AND fence=?",(str(exc),time.time(),job['id'],job['fence']))
                    state.db.execute("UPDATE curation_episode_jobs SET status='held',error=?,updated=? WHERE id=? AND status='pending'",(str(exc),time.time(),job['episode_job']))
                continue
            session_id=observer['provider_session'];session_dir=self.store.home/'observers'/job['observer_id']/str(observer['observer_epoch'])
            resumable=getattr(self.runner,'resumable',self.config.get('backend_execution',{}).get('kind','codex')=='codex')
            if not resumable:session_id=None
            reconstruct=not session_id
            threshold=int(self.config.get('backend_worker',{}).get('compact_after_chars',256_000))
            native_execution=self.config.get('backend_execution',{}).get('kind')=='codex' or isinstance(self.runner,CodexExecution)
            missing_checkpoint=bool(native_execution and session_id and
                not _session_checkpoint_matches(session_dir,session_id))
            if observer['context_chars']>=threshold or missing_checkpoint:
                # A provider handle is only an accelerator. Reconstruct from
                # original evidence before reserving a call if usage is lost.
                session_id=None;reconstruct=True
                with self.store.open() as state,state.db:
                    self.guard(state.db,job)
                    state.db.execute('UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,observer_epoch=observer_epoch+1,context_chars=0 WHERE id=?',(job['observer_id'],))
                session_dir=session_dir.parent/str(observer['observer_epoch']+1)
            config=dict(self.config,_observer_reconstruct=reconstruct)
            def runner(home,cfg,instruction,payload,schema):
                nonlocal session_id
                if self.stop or self.calls>=max_calls or time.monotonic()-start>=max_seconds:raise ValueError('worker_dispatch_bound')
                call_window=min(180,max(10,self.config.get('observer',{}).get('timeout_seconds',180)))
                # Longer waves reserve a full configured call window. A short
                # explicitly bounded run retains its existing shorter timeout.
                time_reserve=call_window if max_seconds>call_window else 10
                if (self.live or native_execution) and max_seconds-(time.monotonic()-start)<time_reserve:
                    raise ValueError('worker_time_reserve')
                estimate=(len(json.dumps(payload,ensure_ascii=True))+len(instruction)+3)//4
                if self.input_tokens+estimate>max_input_tokens:raise ValueError('worker_input_bound')
                request_hash=_hash([instruction,payload,schema,self.execution_key])
                hashes=[request_hash]
                if 'observer_earlier_stage_context' in payload:
                    # Compatibility with confirmed pre-repair returns: the only
                    # omitted field contains newly reconstructed originals.
                    legacy={k:v for k,v in payload.items() if k!='observer_earlier_stage_context'}
                    hashes.append(_hash([instruction,legacy,schema,self.execution_key]))
                with self.store.open() as saved:
                    prior=None
                    for candidate_hash in hashes:
                        prior=saved.db.execute('SELECT * FROM backend_provider_returns WHERE job_id=? AND purpose=? AND request_hash=?',
                            (job['id'],cfg.get('_purpose',''),candidate_hash)).fetchone()
                        if prior:break
                    if prior:
                        self.guard(saved.db,job)
                        native=self.config.get('backend_execution',{}).get('kind')=='codex' or isinstance(self.runner,CodexExecution)
                        handle=prior['provider_handle']
                        if not native or _session_checkpoint_matches(session_dir,handle):
                            session_id=handle
                        else:
                            # Never combine an old provider handle with a new
                            # epoch's usage file. Rebuild original earlier stages
                            # on the next call, retaining a valid current handle.
                            config['_observer_reconstruct']=True
                        return json.loads(prior['result']),json.loads(prior['usage'])
                with self.store.open() as retry_state:
                    retry=_is_model_retry(retry_state.db,job,cfg.get('_purpose',''),
                        request_hash=request_hash,reviewed_retry=bool(payload.get('reviewed_retry')))
                if retry and self.retries>=max_retries:raise ValueError('worker_retry_bound')
                ident='backend-worker-'+uuid.uuid4().hex
                with self.store.open() as checked:self.guard(checked.db,job)
                if cfg.get('_purpose')=='episode_resolve':
                    with self.store.open() as checked,checked.db:
                        influences=checked.db.execute('''SELECT DISTINCT s.id,s.policy_version FROM enterprise_sources s
                            JOIN enterprise_dependencies d ON d.source_id=s.id
                            JOIN enterprise_model_inputs i ON i.related_document_id=d.document_id
                            WHERE i.job_id=? AND NOT EXISTS (SELECT 1 FROM backend_jobs b
                                WHERE b.episode_job=i.job_id AND b.dependency_snapshot_complete=1)
                            UNION SELECT s.id,s.policy_version FROM backend_model_input_snapshots m
                            CROSS JOIN LATERAL jsonb_array_elements_text(m.source_ids::jsonb) x(id)
                            JOIN enterprise_sources s ON s.id=x.id WHERE m.episode_job=?''',
                            (job['episode_job'],job['episode_job'])).fetchall()
                        for source in influences:
                            checked.db.execute('INSERT INTO backend_processing_dependencies VALUES(?,?,?) ON CONFLICT(episode_job,source_id) DO UPDATE SET policy_version=excluded.policy_version',
                                (job['episode_job'],source['id'],source['policy_version']))
                        self.guard(checked.db,job)
                meter=None
                if getattr(self.store,'dsn',None):
                    from agenthub.cloud_ops import Meter
                    meter=Meter(self.store)
                    meter.reserve(ident,job['id'],cfg.get('_purpose','curation'),retry=retry,lease_seconds=min(600,max(30,int(max_seconds))))
                phase='consolidation' if cfg.get('_purpose')=='episode_resolve' else 'live_curation'
                try:
                    if self.live and self.ledger_path:reserve(self.ledger_path,ident,phase,retry=retry)
                    self.mark_dispatch(job,ident)
                    if meter:meter.dispatched(ident)
                except BaseException:
                    if meter:meter.finish(ident,'cancelled')
                    raise
                self.calls+=1;self.retries+=int(retry);self.input_tokens+=estimate
                remaining=max_seconds-(time.monotonic()-start)
                cfg=dict(cfg,observer=dict(cfg.get('observer',{}),timeout_seconds=min(
                    cfg.get('observer',{}).get('timeout_seconds',180),max(1,int(remaining)))))
                started=time.monotonic();returned=False;usage={}
                with self.store.open() as checked,checked.db:
                    self.guard(checked.db,job)
                    checked.db.execute('UPDATE backend_jobs SET lease_until=? WHERE id=? AND fence=?',
                        (time.time()+min(600,max(30,int(remaining)+5)),job['id'],job['fence']))
                try:
                    if cfg.get('_purpose')=='durable_memory_curate':
                        actual=self.runner
                        raw,usage,new_session=actual(home,cfg,instruction,payload,schema,session_dir=session_dir,session_id=session_id)
                        session_id=new_session if resumable else None
                        config['_observer_reconstruct']=not bool(session_id)
                    else:
                        actual=self.runner
                        output=actual(home,cfg,instruction,payload,schema)
                        raw,usage=output[:2]
                    returned=True
                    if meter:meter.finish(ident,'returned',usage=usage,result=raw,latency=time.monotonic()-started)
                    if self.live and self.ledger_path:finish(self.ledger_path,ident,'complete',usage)
                    # Persist a confirmed return under current policy before installation.
                    # Recovery reuses this request identity without another billed provider call.
                    with self.store.open() as returned_state,returned_state.db:
                        self.guard(returned_state.db,job)
                        returned_state.db.execute('''INSERT INTO backend_provider_returns
                            (attempt_id,job_id,purpose,request_hash,result,usage,provider_handle,created)
                            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,purpose,request_hash) DO NOTHING''',
                            (ident,job['id'],cfg.get('_purpose',''),request_hash,json.dumps(raw),json.dumps(usage),session_id,time.time()))
                    with self.store.open() as checked,checked.db:
                        self.guard(checked.db,job)
                        checked.db.execute('UPDATE backend_observers SET pending_session=?,context_chars=context_chars+? WHERE id=?',(session_id,len(json.dumps(payload)) if cfg.get('_purpose')=='durable_memory_curate' else 0,job['observer_id']))
                        checked.db.execute('UPDATE backend_jobs SET pending_result=? WHERE id=?',(json.dumps({'result':raw,'usage':usage}),job['id']))
                        checked.db.execute('INSERT INTO backend_worker_receipts(job_id,attempt_id,purpose,status,usage,elapsed,created,is_live) VALUES(?,?,?,?,?,?,?,?)',
                            (job['id'],ident,cfg.get('_purpose'),'returned',json.dumps(usage),time.monotonic()-started,time.time(),int(self.live)))
                    return raw,usage
                except BaseException as exc:
                    if not returned:usage=getattr(exc,'usage',usage)
                    outcome=getattr(exc,'outcome','uncertain')
                    if outcome not in {'returned','failed','uncertain','cancelled'}:outcome='uncertain'
                    if meter and not returned:meter.finish(ident,outcome,usage=usage,latency=time.monotonic()-started)
                    if self.live and self.ledger_path and not returned:finish(self.ledger_path,ident,'complete' if outcome=='returned' else 'failed',usage)
                    with self.store.open() as failed,failed.db:
                        failed.db.execute('INSERT INTO backend_worker_receipts(job_id,attempt_id,purpose,status,usage,elapsed,created,is_live) VALUES(?,?,?,?,?,?,?,?)',
                            (job['id'],ident,cfg.get('_purpose'),'returned_but_not_committed' if returned else 'provider_'+outcome,json.dumps(usage),time.monotonic()-started,time.time(),int(self.live)))
                    raise
            def commit_guard(db,episode,phase):
                self.guard(db,job)
                if phase=='extracted':
                    from agenthub.observer_context import checkpoint_index
                    progress=json.loads(db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',(row['id'],)).fetchone()[0])
                    index=checkpoint_index(db,previous+current,row,config,observer)
                    checkpoint={'source_ids':[v['id'] for v in previous+current],
                        'watermark':row['source_hash'],'episode_job':row['id'],'context_is_evidence':False,
                        'context_index':index,'extraction_sha256':progress['extraction']['sha256']}
                    db.execute("UPDATE backend_observers SET provider_session=COALESCE(pending_session,provider_session),pending_session=NULL,status='ready',checkpoint=?,updated=? WHERE id=?",
                        (json.dumps(checkpoint),time.time(),job['observer_id']))
                    if config.get('_pipeline_phase')=='extract':
                        db.execute("UPDATE backend_jobs SET status='extraction_ready',pending_result=NULL,lease_until=0,updated=? WHERE id=? AND fence=?",(time.time(),job['id'],job['fence']))
                if phase=='after':
                    if hasattr(self.store,'mark_observed_preference_candidates'):
                        connection=db.execute('SELECT * FROM backend_connections WHERE id=?',(observer['connection'],)).fetchone()
                        processing_ctx=self.store.processing_identity(db,connection)
                        document_ids=[r[0] for r in db.execute('SELECT document_id FROM episode_candidates WHERE job_id=? AND document_id IS NOT NULL',(row['id'],))]
                        self.store.mark_observed_preference_candidates(db,processing_ctx,[s['id'] for s in previous+current],document_ids)
                    progress=json.loads(db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',(row['id'],)).fetchone()[0])
                    # Curated claims, their enterprise provenance, and any
                    # derivative index intent must commit together. A failed
                    # registration aborts the install and leaves the saved
                    # provider return available for explicit recovery.
                    document_ids=[r[0] for r in db.execute('''SELECT DISTINCT document_id
                        FROM episode_candidates WHERE job_id=? AND status='applied'
                        AND document_id IS NOT NULL''',(row['id'],))]
                    for document_id in document_ids:
                        self.store.refresh_documents(db=db,document_id=document_id)
                    db.execute("UPDATE backend_observers SET provider_session=COALESCE(pending_session,provider_session),pending_session=NULL,status='ready',checkpoint=?,updated=? WHERE id=?",
                        (json.dumps({'source_ids':[s['id'] for s in previous+current],
                            'watermark':row['source_hash'],'unresolved':progress.get('episode_summary_assertions',{}).get('open_work',[]),
                            'working_context':progress.get('episode_summary_assertions',{}).get('intent'),
                            'context_is_evidence':False,'episode_job':row['id'],
                            'context_index':json.loads(db.execute('SELECT checkpoint FROM backend_observers WHERE id=?',(job['observer_id'],)).fetchone()[0]).get('context_index')}),time.time(),job['observer_id']))
                    db.execute("UPDATE backend_jobs SET status='complete',pending_result=NULL,lease_until=0,updated=? WHERE id=? AND fence=?",(time.time(),job['id'],job['fence']))
            with self.store.open() as state:
                run_once(state,config,runner,project_scope=row['project'],job_id=row['id'],context_sources=previous,guard=commit_guard)
            with self.store.open() as state,state.db:
                outcome=state.db.execute('SELECT status,error FROM curation_episode_jobs WHERE id=?',(row['id'],)).fetchone()
                status=state.db.execute('SELECT status FROM backend_jobs WHERE id=?',(job['id'],)).fetchone()[0]
                if status=='complete':completed+=1
                elif status=='extraction_ready':extracted+=1
                elif (self.config.get('backend_worker',{}).get('resume_bounded_jobs',False)
                        and outcome['error'] in {'worker_dispatch_bound','worker_input_bound','worker_time_reserve'}):
                    # These checks occur before dispatch. All earlier stage
                    # outputs are durable; continue their confirmed provider
                    # conversation instead of creating a new observer epoch.
                    self.guard(state.db,job)
                    state.db.execute("""UPDATE backend_jobs SET status='pending',
                        recovery='bounded_checkpoint',last_error=NULL,lease_until=0,
                        dispatch_attempt=NULL,pending_result=NULL,updated=? WHERE id=? AND fence=?""",
                        (time.time(),job['id'],job['fence']))
                    state.db.execute("""UPDATE curation_episode_jobs SET status='pending',
                        error=NULL,next_attempt=0,updated=? WHERE id=?""",(time.time(),row['id']))
                    state.db.execute("""UPDATE backend_observers SET
                        provider_session=coalesce(pending_session,provider_session),
                        pending_session=NULL,status='ready',updated=? WHERE id=?""",
                        (time.time(),job['observer_id']))
                    # A finite wave cannot gain more time or input capacity by
                    # reclaiming this pending job. Leave its durable checkpoint
                    # for the next wave instead of spinning through no-op claims.
                    if outcome['error'] in {'worker_input_bound','worker_time_reserve'}:
                        break
                else:
                    # A returned but uninstalled result may have advanced the provider.
                    # Hold until explicit recovery, with no blind automatic retry.
                    state.db.execute("UPDATE backend_jobs SET status='held',last_error=?,lease_until=0 WHERE id=? AND fence=?",(outcome['error'] or 'incomplete_processing',job['id'],job['fence']))
                    # Keep the canonical episode in the same reviewable hold state.
                    # A pending episode behind a held worker would otherwise block
                    # generation activation without an explicit skip/recovery path.
                    state.db.execute("""UPDATE curation_episode_jobs SET status='held',
                        error=coalesce(error,?),updated=? WHERE id=? AND status='pending'""",
                        (outcome['error'] or 'incomplete_processing',time.time(),row['id']))
                    progress=json.loads(state.db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',(row['id'],)).fetchone()[0])
                    if not progress.get('extraction'):
                        state.db.execute("UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,observer_epoch=observer_epoch+1,status=CASE WHEN status='invalidated' THEN status ELSE 'reconstruct' END WHERE id=?",(job['observer_id'],))
            if hasattr(self.store,'curate_observed_preferences') and status=='complete':
                with self.store.open() as checked:
                    connection=checked.db.execute('SELECT * FROM backend_connections WHERE id=?',(observer['connection'],)).fetchone()
                    processing_ctx=self.store.processing_identity(checked.db,connection)
                    document_ids=[r[0] for r in checked.db.execute('SELECT document_id FROM episode_candidates WHERE job_id=? AND document_id IS NOT NULL',(row['id'],))]
                for source in previous+current:
                    self.store.curate_observed_preferences(processing_ctx,source['id'],document_ids)
            # commit_guard registered each applied claim and its derivative
            # index intent in the same transaction. A full repair sweep takes
            # the exclusive delivery lock and can block another observer's
            # shared policy guard. Run repair as separate maintenance, never
            # as a completion requirement for an already committed turn.
        return {'completed':completed,'extracted':extracted,'processed':processed,'calls':self.calls,'retries':self.retries,'estimated_input_tokens':self.input_tokens,'elapsed_seconds':time.monotonic()-start}

    def recover(self,job_id,*,reprocess=False,retain_validated=False,reviewed_reason=None,rederive=False,
                reviewed_stage_limit=None):
        if rederive and (reprocess or retain_validated or reviewed_reason or reviewed_stage_limit):raise ValueError('invalid_source_rederivation')
        if reviewed_reason is not None and (not reprocess or retain_validated or
                reviewed_reason not in {'missing_durable_fact','incorrect_attribution','incomplete_record'}):
            raise ValueError('invalid_reprocessing_review')
        with self.store.open() as state,state.db:
            row=state.db.execute('SELECT * FROM backend_jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['status'] not in ({'complete'} if reprocess else {'held','running'}):raise ValueError('recoverable_worker_job_required')
            if row['status']=='running' and row['lease_until']>=time.time():raise ValueError('worker_still_leased')
            episode=state.db.execute('SELECT * FROM curation_episode_jobs WHERE id=?',(row['episode_job'],)).fetchone()
            if not episode or episode['status']=='withdrawn':raise ValueError('withdrawn_worker_job')
            if reviewed_stage_limit is not None and (reprocess or not retain_validated or
                    episode['error']!='memory_stage_count_exceeds_limit' or
                    type(reviewed_stage_limit) is not int or reviewed_stage_limit<=int(
                        self.config.get('episode_curation',{}).get('max_stages',32))):
                raise ValueError('invalid_reviewed_stage_limit')
            dependencies=state.db.execute('SELECT s.active FROM backend_processing_dependencies d LEFT JOIN enterprise_sources s ON s.id=d.source_id WHERE d.episode_job=?',(row['episode_job'],)).fetchall()
            if not rederive and any(not dep['active'] for dep in dependencies):raise ValueError('inactive_worker_dependency')
            current_sources=sources_for_job(state.db,episode)
            observer_sql='SELECT * FROM backend_observers WHERE id=?'
            if getattr(state.db,'dialect',None)=='postgres':observer_sql+=' FOR UPDATE'
            observer=state.db.execute(observer_sql,(row['observer_id'],)).fetchone()
            # claim() locks this same observer row. Recovery either observes a
            # live claimant or finishes before a new claimant can use the handle.
            if state.db.execute("""SELECT 1 FROM backend_jobs WHERE observer_id=?
                    AND id!=? AND status='running' AND lease_until>? LIMIT 1""",
                    (row['observer_id'],job_id,time.time())).fetchone():
                raise ValueError('observer_still_leased')
            connection=state.db.execute('SELECT * FROM backend_connections WHERE id=?',(observer['connection'],)).fetchone() if observer else None
            if not connection or not connection['active']:raise ValueError('inactive_worker_connection')
            if hasattr(self.store,'processing_identity'):self.store.processing_identity(state.db,connection)
            if rederive:
                if episode['generation_id']!=self.config['episode_curation']['generation_id']:raise ValueError('rederivation_generation_mismatch')
                # Never unhide the prior result or reuse its comparison/context.
                # Retained raw turn sources must independently remain authorized.
                if not current_sources:raise ValueError('rederivation_originals_required')
                new_observer=_hash([row['observer_id'],'source_rederivation',uuid.uuid4().hex])
                state.db.execute('''INSERT INTO backend_observers
                    (id,tenant,connection,conversation,internal_project,generation,permission_hash,model,prompt_version,updated,execution_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(new_observer,observer['tenant'],observer['connection'],observer['conversation'],
                    observer['internal_project'],observer['generation'],observer['permission_hash'],observer['model'],observer['prompt_version'],time.time(),self.execution_key))
                state.db.execute('UPDATE backend_jobs SET observer_id=?,dependency_snapshot_complete=1,dispatch_attempt=NULL,pending_result=NULL WHERE id=?',(new_observer,job_id))
                state.db.execute('DELETE FROM backend_processing_dependencies WHERE episode_job=?',(row['episode_job'],))
                state.db.execute('DELETE FROM backend_model_input_snapshots WHERE episode_job=?',(row['episode_job'],))
                state.db.execute('DELETE FROM enterprise_model_inputs WHERE job_id=?',(row['episode_job'],))
                state.db.execute('DELETE FROM backend_provider_returns WHERE job_id=?',(job_id,))
            state.db.execute("UPDATE backend_jobs SET status='pending',recovery='explicit_rebuild',lease_until=0,last_error=NULL WHERE id=?",(job_id,))
            state.db.execute("UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,observer_epoch=observer_epoch+1,status='reconstruct' WHERE id=?",(row['observer_id'],))
            progress={}
            if rederive:
                progress={'source_rederivation':{'id':new_observer,'source_only':True}}
            elif retain_validated:
                old=state.db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',(row['episode_job'],)).fetchone()
                progress=json.loads(old[0])
                if reprocess:progress['resolutions']=[]
            elif reviewed_reason is not None:
                previous=json.loads(episode['progress'] or '{}')
                history=previous.get('extraction_history',[])
                if previous.get('extraction'):history=history+[previous['extraction']]
                progress={'extraction_history':history[-3:],
                    'capture_diagnostics':previous.get('capture_diagnostics',{}),
                    'reviewed_reprocessing':{'reason':reviewed_reason,
                        'cycle':int(previous.get('reviewed_reprocessing',{}).get('cycle',0))+1}}
            if reviewed_stage_limit is not None:
                from agenthub.processing.episode_pipeline import _hash as source_hash
                progress['reviewed_stage_limit']={'limit':reviewed_stage_limit,
                    'source_snapshot_hash':source_hash(current_sources)}
            state.db.execute("UPDATE curation_episode_jobs SET status='pending',progress=?,next_attempt=0,error=NULL WHERE id=?",(json.dumps(progress),row['episode_job']))
            if reprocess:
                state.db.execute("UPDATE episode_candidates SET status='superseded_processing' WHERE job_id=?",(row['episode_job'],))
        return {'job_id':job_id,'recovery':'source_rederivation' if rederive else 'explicit_rebuild'}

    def status(self):
        with self.store.open() as state:
            return {'jobs':{r[0]:r[1] for r in state.db.execute('SELECT status,count(*) FROM backend_jobs GROUP BY status')},
                'observers':[{'id':r['id'],'connection':r['connection'],'status':r['status'],'observer_epoch':r['observer_epoch'],
                    'source_epoch':r['source_epoch'],'provider_session_hash':_hash(r['provider_session']) if r['provider_session'] else None,
                    'context_chars':r['context_chars']} for r in state.db.execute('SELECT * FROM backend_observers')],
                'runner_dispatches':state.db.execute('SELECT count(*) FROM backend_worker_receipts WHERE attempt_id IS NOT NULL').fetchone()[0],
                'model_calls':state.db.execute('SELECT count(*) FROM backend_worker_receipts WHERE is_live=1').fetchone()[0],
                'pending_parts':dict(state.db.execute('SELECT count(*) count,coalesce(sum(length(data)),0) bytes,min(created) oldest FROM backend_parts').fetchone()),
                'capture_gaps':{r[0]:r[1] for r in state.db.execute('SELECT reason,count(*) FROM backend_capture_gaps GROUP BY reason')},
                'held_episodes':{r[0]:r[1] for r in state.db.execute("SELECT coalesce(error,'unknown'),count(*) FROM curation_episode_jobs WHERE status='held' GROUP BY error")}}
