"""Finite local operator maintenance, using the authoritative PostgreSQL service.

This module never dispatches a model. Source and connection mutations retain
their authenticated canonical authorization; operator possession is insufficient
to impersonate a source owner. Tenant retirement has a separate explicit apply.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from agenthub.backend_worker import Worker
from agenthub.cloud_ops import Meter
from agenthub.enterprise import Denied
from agenthub.postgres import connect, TenantRegistry


def private_input(path):
    path=Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_mode&0o077:
        raise ValueError('operator_input_permissions')
    if path.stat().st_size>1024*1024:raise ValueError('operator_input_bound')
    return json.loads(path.read_text())


def _write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:json.dump(value,output,indent=2,sort_keys=True)


class IndexFreshness:
    """Finite vector repair over namespaced rows in the existing durable outbox.

    Reconciliation derives work from accepted current document revisions. It is
    safe to repeat after a crash and never embeds during construction or scan.
    A source-write integration must invoke reconciliation for immediate readiness.
    """
    prefix='cloud-vector:v1:'
    kind='cloud_vector_v1'

    def __init__(self,store,*,model_key=None,max_attempts=3):
        if type(max_attempts) is not int or not 1<=max_attempts<=10:
            raise ValueError('index_attempt_bound')
        self.store=store
        self.model_key=model_key or getattr(store,'semantic_model_key',None)
        if not isinstance(self.model_key,str) or not self.model_key:
            raise ValueError('index_model_key_required')
        self.max_attempts=max_attempts

    def _id(self,document_id):
        digest=hashlib.sha256((self.store.tenant_id+'\0'+document_id).encode()).hexdigest()
        return self.prefix+digest

    def _active_generation(self,db):
        row=db.execute('''SELECT s.generation_id,s.model_key FROM cloud_vector_state s
            JOIN cloud_vector_generations g ON g.id=s.generation_id
            WHERE s.singleton=1 AND g.status='active' AND g.model_key=s.model_key''').fetchone()
        return row if row and row['model_key']==self.model_key else None

    def ensure_generation(self):
        """Initialize an empty incremental index; never replace an existing model."""
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            db=state.db
            if db.execute('SELECT 1 FROM cloud_vector_state WHERE singleton=1').fetchone():return
            from agenthub.processing.semantic import DIMENSION
            generation='vectors-'+uuid.uuid4().hex;now=time.time()
            db.execute('''INSERT INTO cloud_vector_generations(id,model_key,dimension,status,created,completed,document_count)
                VALUES(?,?,?,'active',?,?,0)''',(generation,self.model_key,DIMENSION,now,now))
            db.execute('INSERT INTO cloud_vector_state VALUES(1,?,?,?)',(generation,self.model_key,now))

    def _current(self,db,document_id):
        return db.execute('''SELECT d.active_revision_id AS revision_id,r.claim_json
            FROM knowledge_documents d JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
            JOIN enterprise_documents e ON e.id=d.document_id
            WHERE d.document_id=? AND d.lifecycle='active' AND e.active=1
            AND e.tenant=? AND e.revision=d.active_revision_id
            AND EXISTS(SELECT 1 FROM enterprise_dependencies x WHERE x.document_id=d.document_id)
            AND NOT EXISTS(SELECT 1 FROM enterprise_dependencies x
                LEFT JOIN enterprise_sources s ON s.id=x.source_id
                WHERE x.document_id=d.document_id AND (s.id IS NULL OR s.active=0
                    OR s.tenant!=e.tenant OR s.internal_project!=e.internal_project))''',
            (document_id,self.store.tenant_id)).fetchone()

    def _queue_revision_tx(self,db,document_id,revision_id,generation,now):
        payload=json.dumps({'kind':self.kind,'document_id':document_id,
            'revision_id':revision_id,'generation_id':generation},sort_keys=True)
        return db.execute('''INSERT INTO outbox(id,payload,status,attempts,last_error,response,next_attempt)
            VALUES(?,?,'pending',0,NULL,NULL,?) ON CONFLICT(id) DO UPDATE SET
            payload=excluded.payload,status='pending',attempts=0,last_error=NULL,
            response=NULL,next_attempt=excluded.next_attempt
            WHERE outbox.payload!=excluded.payload''',
            (self._id(document_id),payload,now)).rowcount

    def queue_document_tx(self,db,document_id,*,now=None):
        """Record one current revision in the caller's accepted-write transaction."""
        active=self._active_generation(db)
        current=self._current(db,document_id)
        if not active or not current:return 0
        generation=active['generation_id'];revision=current['revision_id']
        vector=db.execute('''SELECT revision_id FROM cloud_document_vectors
            WHERE generation_id=? AND document_id=?''',(generation,document_id)).fetchone()
        if vector and vector['revision_id']==revision:
            # A generation switch can leave an old job even though this exact
            # revision is already indexed. Retire that job without re-embedding.
            payload=json.dumps({'kind':self.kind,'document_id':document_id,
                'revision_id':revision,'generation_id':generation},sort_keys=True)
            db.execute("""UPDATE outbox SET payload=?,status='searchable',response=NULL,last_error=NULL
                WHERE id=? AND payload!=?""",(payload,self._id(document_id),payload))
            return 0
        return self._queue_revision_tx(db,document_id,revision,generation,
            time.time() if now is None else now)

    def reconcile(self,*,max_documents=1000,after_document_id='',now=None):
        if type(max_documents) is not int or not 1<=max_documents<=1000:
            raise ValueError('index_document_bound')
        if not isinstance(after_document_id,str) or len(after_document_id)>256:
            raise ValueError('index_reconcile_cursor')
        now=time.time() if now is None else now
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            db=state.db;active=self._active_generation(db)
            generation=active['generation_id'] if active else ''
            rows=db.execute('''SELECT d.document_id,d.active_revision_id AS revision_id
                FROM knowledge_documents d JOIN enterprise_documents e ON e.id=d.document_id
                LEFT JOIN cloud_document_vectors v ON v.document_id=d.document_id AND v.generation_id=?
                WHERE d.lifecycle='active' AND d.active_revision_id IS NOT NULL
                AND e.active=1 AND e.tenant=? AND e.revision=d.active_revision_id
                AND d.document_id>?
                AND (v.revision_id IS NULL OR v.revision_id!=d.active_revision_id)
                AND EXISTS(SELECT 1 FROM enterprise_dependencies x WHERE x.document_id=d.document_id)
                AND NOT EXISTS(SELECT 1 FROM enterprise_dependencies x
                    LEFT JOIN enterprise_sources s ON s.id=x.source_id
                    WHERE x.document_id=d.document_id AND (s.id IS NULL OR s.active=0
                        OR s.tenant!=e.tenant OR s.internal_project!=e.internal_project))
                ORDER BY d.document_id LIMIT ?''',(generation,self.store.tenant_id,after_document_id,max_documents+1)).fetchall()
            queued=0
            for row in rows[:max_documents]:
                queued+=self._queue_revision_tx(db,row['document_id'],row['revision_id'],generation,now)
            return {'tenant':self.store.tenant_id,'queued':queued,'scanned':min(len(rows),max_documents),
                'more':len(rows)>max_documents,
                'next_after_document_id':rows[max_documents-1]['document_id'] if len(rows)>max_documents else None,
                'generation':generation or None,'provider_calls':0}

    def claim(self,*,now=None,lease_seconds=30):
        if type(lease_seconds) is not int or not 1<=lease_seconds<=300:
            raise ValueError('index_lease_bound')
        now=time.time() if now is None else now
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            db=state.db
            # Retire expired poison claims without letting the earliest one
            # suppress a later tenant-local job in this finite worker turn.
            for _ in range(100):
                row=db.execute('''SELECT id,payload,attempts FROM outbox
                    WHERE id LIKE ? AND status IN ('pending','running') AND next_attempt<=?
                    ORDER BY next_attempt,id LIMIT 1''',(self.prefix+'%',now)).fetchone()
                if not row:return None
                if row['attempts']>=self.max_attempts:
                    db.execute("UPDATE outbox SET status='failed',last_error='lease_attempts_exhausted',response=NULL WHERE id=?",(row['id'],))
                    continue
                payload=json.loads(row['payload'])
                if (not isinstance(payload,dict) or payload.get('kind')!=self.kind or
                        not isinstance(payload.get('document_id'),str) or
                        not isinstance(payload.get('revision_id'),str) or
                        not isinstance(payload.get('generation_id'),str) or
                        self._id(payload['document_id'])!=row['id']):
                    raise ValueError('index_outbox_namespace_collision')
                fence=uuid.uuid4().hex
                db.execute("UPDATE outbox SET status='running',attempts=attempts+1,response=?,next_attempt=? WHERE id=?",
                    (fence,now+lease_seconds,row['id']))
                return {'id':row['id'],'payload':payload,'fence':fence,
                    'attempts':row['attempts']+1}
            return None

    def _transition(self,job,status,*,now,error=None):
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            row=state.db.execute('SELECT status,response,payload FROM outbox WHERE id=?',(job['id'],)).fetchone()
            if not row or row['status']!='running' or row['response']!=job['fence']:
                return 'stale'
            if row['payload']!=json.dumps(job['payload'],sort_keys=True):
                return 'stale'
            if status=='pending':
                next_attempt=now+min(2**max(0,job['attempts']-1),60) if error else now
            else:next_attempt=now
            state.db.execute('''UPDATE outbox SET status=?,response=NULL,next_attempt=?,last_error=?
                WHERE id=?''',(status,next_attempt,error,job['id']))
            return status

    def run_once(self,embedder,*,now=None,lease_seconds=30):
        now=time.time() if now is None else now
        if embedder is None:raise ValueError('local_embedding_model_required')
        job=self.claim(now=now,lease_seconds=lease_seconds)
        if job is None:return {'tenant':self.store.tenant_id,'status':'idle','provider_calls':0}
        document=job['payload']['document_id'];revision=job['payload']['revision_id']
        with self.store.open() as state:
            active=self._active_generation(state.db)
            current=self._current(state.db,document)
        if not current or current['revision_id']!=revision:
            status=self._transition(job,'no_learning',now=now,error='source_or_revision_changed')
            return {'tenant':self.store.tenant_id,'status':status,'provider_calls':0}
        if not active or active['generation_id']!=job['payload']['generation_id']:
            status=self._transition(job,'pending',now=now)
            if status=='pending' and active:
                with self.store.delivery_lock(),self.store.open() as state,state.db:
                    self.queue_document_tx(state.db,document,now=now)
            return {'tenant':self.store.tenant_id,'status':status,'provider_calls':0}
        claim=json.loads(current['claim_json'])
        body='\n'.join(str(claim.get(key,'')) for key in ('title','lesson','applicability'))
        try:
            values=embedder.embed_documents([body])
            if len(values)!=1:raise ValueError('embedding_batch_shape')
            from agenthub.cloud_retrieval import _vector
            vector=_vector(values[0])
        except Exception as error:
            terminal=job['attempts']>=self.max_attempts
            status=self._transition(job,'failed' if terminal else 'pending',now=now,
                error=type(error).__name__)
            return {'tenant':self.store.tenant_id,'status':status,'provider_calls':0}
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            db=state.db;row=db.execute('SELECT status,response,payload FROM outbox WHERE id=?',(job['id'],)).fetchone()
            if not row or row['status']!='running' or row['response']!=job['fence'] or row['payload']!=json.dumps(job['payload'],sort_keys=True):
                return {'tenant':self.store.tenant_id,'status':'stale','provider_calls':0}
            active=self._active_generation(db);current=self._current(db,document)
            if not current or current['revision_id']!=revision:
                db.execute("UPDATE outbox SET status='no_learning',response=NULL,last_error='source_or_revision_changed' WHERE id=?",(job['id'],))
                return {'tenant':self.store.tenant_id,'status':'no_learning','provider_calls':0}
            if not active or active['generation_id']!=job['payload']['generation_id']:
                db.execute("UPDATE outbox SET status='pending',response=NULL,next_attempt=? WHERE id=?",(now,job['id']))
                if active:self.queue_document_tx(db,document,now=now)
                return {'tenant':self.store.tenant_id,'status':'pending','provider_calls':0}
            db.execute('''INSERT INTO cloud_document_vectors(generation_id,document_id,revision_id,body_sha256,embedding)
                VALUES(?,?,?,?,?::vector) ON CONFLICT(generation_id,document_id) DO UPDATE SET
                revision_id=excluded.revision_id,body_sha256=excluded.body_sha256,embedding=excluded.embedding''',
                (active['generation_id'],document,revision,hashlib.sha256(body.encode()).hexdigest(),vector))
            db.execute("UPDATE outbox SET status='searchable',response=NULL,last_error=NULL,next_attempt=? WHERE id=?",(now,job['id']))
        return {'tenant':self.store.tenant_id,'status':'searchable','provider_calls':0}

    def retry(self,document_id,*,now=None):
        if not isinstance(document_id,str) or not 1<=len(document_id)<=256:
            raise ValueError('index_document_id_required')
        now=time.time() if now is None else now
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            row=state.db.execute('SELECT status FROM outbox WHERE id=?',(self._id(document_id),)).fetchone()
            if not row or row['status'] not in {'failed','no_learning'}:
                raise ValueError('retryable_index_job_required')
            state.db.execute("UPDATE outbox SET status='pending',attempts=0,last_error=NULL,response=NULL,next_attempt=? WHERE id=?",(now,self._id(document_id)))
        return {'tenant':self.store.tenant_id,'status':'pending','provider_calls':0}

    def status(self,*,max_documents=1000,after_id=''):
        if (type(max_documents) is not int or not 1<=max_documents<=1000 or
                not isinstance(after_id,str) or len(after_id)>256):
            raise ValueError('index_status_bound')
        with self.store.open() as state:
            db=state.db;active=self._active_generation(db)
            rows=db.execute('''SELECT id,payload,status FROM outbox WHERE id LIKE ?
                AND id>? ORDER BY id LIMIT ?''',(self.prefix+'%',after_id,max_documents+1)).fetchall()
            by_document={}
            for row in rows[:max_documents]:
                payload=json.loads(row['payload'])
                if payload.get('kind')!=self.kind:raise ValueError('index_outbox_namespace_collision')
                document=payload['document_id'];status=row['status']
                if status=='searchable':
                    current=self._current(db,document)
                    if not current:status='no_learning'
                    elif not active:status='pending'
                    else:
                        vector=db.execute('''SELECT revision_id FROM cloud_document_vectors
                            WHERE generation_id=? AND document_id=?''',
                            (active['generation_id'],document)).fetchone()
                        if not vector or vector['revision_id']!=current['revision_id']:
                            status='pending'
                by_document[document]=status
        counts={status:sum(value==status for value in by_document.values())
            for status in ('pending','running','failed','no_learning','searchable')}
        return {'tenant':self.store.tenant_id,'counts':counts,'by_document':by_document,
            'more':len(rows)>max_documents,
            'next_after_id':rows[max_documents-1]['id'] if len(rows)>max_documents else None,
            'provider_calls':0}


def run_fair_index_workers(indexes,embedder,*,max_jobs=100,now=None):
    """One finite round-robin pass per tenant before returning to a busy tenant."""
    if (not isinstance(indexes,(list,tuple)) or not indexes or len(indexes)>100 or
            len({index.store.tenant_id for index in indexes})!=len(indexes) or
            type(max_jobs) is not int or not 1<=max_jobs<=1000):
        raise ValueError('index_fairness_bound')
    results=[]
    while len(results)<max_jobs:
        progressed=False
        for index in indexes:
            if len(results)>=max_jobs:break
            result=index.run_once(embedder,now=now)
            if result['status']!='idle':
                results.append(result);progressed=True
        if not progressed:break
    return {'results':results,'jobs':len(results),'provider_calls':0}


class Maintenance:
    """Bounded commands for one explicitly selected tenant; no inference calls."""
    def __init__(self,store,config):
        self.store=store
        # Worker recovery/status are the canonical state machine. live=False
        # does not dispatch and does not touch the installation/campaign ledger.
        self.worker=Worker(store,config,live=False)

    def hold(self,job_ids):
        if (not isinstance(job_ids,list) or not 1<=len(job_ids)<=100 or
                len(set(job_ids))!=len(job_ids) or any(not isinstance(j,str) or not j or len(j)>128 for j in job_ids)):
            raise ValueError('explicit_bounded_jobs_required')
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            rows=[]
            for job_id in sorted(job_ids):
                row=state.db.execute('SELECT * FROM backend_jobs WHERE id=? FOR UPDATE',(job_id,)).fetchone()
                if not row or row['status'] not in {'pending','running','held'}:
                    raise ValueError('holdable_worker_job_required')
                rows.append(row)
            for row in rows:
                state.db.execute("UPDATE backend_jobs SET status='held',fence=?,lease_until=0,last_error='operator_hold',updated=? WHERE id=?",
                    (uuid.uuid4().hex,time.time(),row['id']))
                self.store._audit(state.db,{'tenant':self.store.tenant_id,'actor':'operator'},'worker_hold','held',row['id'])
        return {'job_ids':sorted(job_ids),'held':len(rows),'provider_calls':0}

    def retry(self,job_id,*,retain_validated=False):
        with self.store.delivery_lock():
            return self.worker.recover(job_id,retain_validated=retain_validated)|{'provider_calls':0}

    def admission(self,value):
        if set(value)-{'max_parallel','max_attempts','enabled'}:raise ValueError('invalid_admission_fields')
        if any(type(value.get(k,default)) is not int for k,default in [('max_parallel',4),('max_attempts',100000)]) or type(value.get('enabled',True)) is not bool:
            raise ValueError('invalid_admission_types')
        Meter(self.store).configure(**value)
        return {'tenant':self.store.tenant_id,'admission':value,'provider_calls':0}

    def reconcile(self,value):
        if set(value)-{'attempt_id','usage','result_digest','latency','price'}:raise ValueError('invalid_reconciliation_fields')
        digest=value['result_digest']
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('confirmed_result_digest_required')
        return Meter(self.store).reconcile(value['attempt_id'],usage=value['usage'],
            result={'sha256':digest},latency=value.get('latency'),price=value.get('price'))|{'provider_calls':0}

    def lifecycle(self,ctx,value):
        result=self.store.lifecycle(ctx,value)
        # No reason, source contents or correction text enters the audit receipt.
        return {k:v for k,v in result.items() if k in {'target_id','operation','source_version',
            'dependent_documents_blocked','replacement_source_id'}}|{'provider_calls':0}

    def connection(self,ctx,action,ident,value=None):
        value=value or {}
        if action=='enroll':
            result=self.store.enroll_connection(ctx,ident,value['namespace'],value['project'],value['source_types'],
                visibility=value.get('visibility','private'),reader_ids=value.get('reader_ids'),
                freshness_seconds=value.get('freshness_seconds',86400),capabilities=value.get('capabilities'))
        elif action in {'refresh','disable','policy'}:
            if set(value)-{'visibility','reader_ids'}:raise ValueError('invalid_connection_policy_fields')
            result=self.store.connection_policy(ctx,ident,**value,active=action!='disable')
        elif action=='status':
            self.store._need(ctx,'ingest')
            with self.store.open() as state:
                # Unlike _connection(), status remains useful after disable.
                row=state.db.execute('SELECT * FROM backend_connections WHERE id=?',(ident,)).fetchone()
                if not row or row['tenant']!=ctx['tenant'] or row['enrollment']!=ctx['enrollment'] or row['owner']!=ctx['actor']:
                    raise Denied()
                result={key:row[key] for key in ('id','policy_version','permission_observed','freshness_seconds','active')}
                result['stale']=row['permission_observed']+row['freshness_seconds']<time.time()
        else:raise ValueError('invalid_connection_action')
        return result|{'provider_calls':0}

    def export(self,objects,destination,*,max_rows=5000,max_bytes=64*1024*1024,max_objects=100):
        from agenthub.cloud_recovery import backup
        result=backup(self.store,objects,destination,max_rows=max_rows,max_bytes=max_bytes,max_objects=max_objects)
        return {'tenant':self.store.tenant_id,'snapshot':str(Path(destination).resolve()),
            'fingerprint':result['fingerprint'],'tables':len(result['tables']),
            'objects':len(result['objects']),'kind':result['kind'],'provider_calls':0}


def _operator(profile,tenant,expected_database=None):
    from psycopg.conninfo import conninfo_to_dict
    profile=Path(profile).expanduser().resolve()
    value=private_input(profile/'operator.json')
    item=value['tenants'].get(tenant)
    if not item:raise ValueError('operator_tenant_missing')
    if expected_database is not None and item['database']!=expected_database:
        raise ValueError('expected_tenant_database_mismatch')
    admin=conninfo_to_dict(item['admin_dsn']);app=conninfo_to_dict(item['dsn'])
    control=conninfo_to_dict(value['control_admin_dsn'])
    if any(i.get('host') not in {'127.0.0.1','localhost','::1'} for i in (admin,app,control)):
        raise ValueError('maintenance_local_postgres_only')
    if (admin.get('dbname')!=item['database'] or app.get('dbname')!=item['database'] or
            app.get('user')!=item['role'] or admin.get('user')==item['role'] or
            item['database']==control.get('dbname')):
        raise ValueError('operator_tenant_database_binding_conflict')
    with connect(value['control_admin_dsn']) as db:
        route=db.execute('SELECT dsn FROM cloud_tenants WHERE id=%s',(tenant,)).fetchone()
        if not route or route['dsn']!=item['dsn']:raise ValueError('operator_tenant_route_conflict')
    return profile,value,item


def retire(profile,tenant,*,expected_database,objects,apply=False,max_objects=100,max_seconds=60):
    """Retire one local tenant, disabling routes before current data/object purge.

    The saved private receipt survives dropping the tenant database. This cannot
    erase retained snapshots or S3 object versions and explicitly reports that.
    Apply is resumable while the database exists; failure leaves routing disabled.
    """
    from psycopg import sql
    from agenthub.source_objects import validate_key
    if (not expected_database or type(max_objects) is not int or not 1<=max_objects<=1000 or
            type(max_seconds) not in (int,float) or not 1<=max_seconds<=600):
        raise ValueError('explicit_retirement_bounds_required')
    deadline=time.monotonic()+max_seconds
    profile,operator,item=_operator(profile,tenant,expected_database)
    keys=set()
    with connect(item['admin_dsn']) as db:
        for table in ('backend_object_uploads','cloud_migrated_source_objects','cloud_segment_uploads'):
            rows=db.execute(sql.SQL('SELECT object_key FROM {} LIMIT %s').format(sql.Identifier(table)),(max_objects+1,)).fetchall()
            keys.update(validate_key(row['object_key']) for row in rows)
        if len(keys)>max_objects:raise ValueError('retirement_object_bound')
        sources=db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0]
    receipt={'id':uuid.uuid4().hex,'command':'tenant-retire','tenant':tenant,'database':expected_database,
        'apply':bool(apply),'source_rows':sources,'current_object_keys':len(keys),
        'retained_backups':'preserved; separately governed','object_versions':'not purged; separately governed',
        'provider_calls':0,'timestamp':time.time()}
    if not apply:return receipt|{'status':'preview','routing_changed':False,'database_dropped':False}
    registry=TenantRegistry(operator['control_admin_dsn'],profile/'operator-state')
    registry.set_active(tenant,False)
    # Disable other control identities/routes as well; cached application stores
    # check the authoritative tenant route at authentication and readiness.
    with connect(operator['control_admin_dsn']) as db:
        db.execute('UPDATE cloud_credential_routes SET active=0 WHERE tenant=%s',(tenant,))
        db.execute('UPDATE cloud_external_bindings SET active=0 WHERE tenant=%s',(tenant,))
    receipt['routing_changed']=True
    path=profile/'maintenance-receipts'/(receipt['id']+'.json')
    _write(path,receipt|{'status':'routing_disabled','database_dropped':False})
    try:
        # Fence current processing and deny delivery before physically deleting.
        from agenthub.postgres import PostgresEnterpriseStore
        store=PostgresEnterpriseStore(profile/'operator-state'/tenant,item['admin_dsn'],tenant)
        with store.delivery_lock(),store.open() as state,state.db:
            state.db.execute('UPDATE cloud_admission SET enabled=0 WHERE tenant=?',(tenant,))
            state.db.execute('UPDATE enterprise_credentials SET active=0 WHERE tenant=?',(tenant,))
            state.db.execute('UPDATE enterprise_refresh_tokens SET active=0')
            state.db.execute("UPDATE backend_jobs SET status='held',fence=?,lease_until=0,last_error='tenant_retired'",(uuid.uuid4().hex,))
            state.db.execute("UPDATE backend_observers SET status='invalidated',provider_session=NULL,pending_session=NULL")
        for key in sorted(keys):
            if time.monotonic()>=deadline:raise TimeoutError('tenant_retirement_deadline')
            objects.delete(key)
        if time.monotonic()>=deadline:raise TimeoutError('tenant_retirement_deadline')
        with connect(operator['control_admin_dsn'],autocommit=True) as db:
            # Explicit DB identifier is validated against the private operator
            # route above. FORCE terminates only this selected tenant's sessions.
            db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(expected_database)))
        receipt.update(status='retired',database_dropped=True,current_object_delete_calls=len(keys))
    except Exception as error:
        receipt.update(status='failed_routing_disabled',database_dropped=False,error=type(error).__name__)
        raise
    finally:
        # Content-free final receipt, never DSNs, tokens, source data or keys.
        temporary=path.with_suffix('.final.json');_write(temporary,receipt)
    return receipt


def execute(profile,tenant,command,*,value=None,token_file=None,output=None,job_id=None,
            connection_id=None,expected_database=None,apply=False):
    profile,operator,item=_operator(profile,tenant,expected_database if command=='tenant-retire' else None)
    from agenthub.cloud_local import runtime, worker_config
    from agenthub.object_config import objects_from_settings
    settings,registry=runtime(profile)
    if command=='tenant-retire':
        return retire(profile,tenant,expected_database=expected_database,objects=objects_from_settings(settings),apply=apply)
    store=registry.resolve(tenant)
    value=value or {}
    if command.startswith('index-'):
        index=IndexFreshness(store)
        if command=='index-reconcile':
            result=index.reconcile(**value)
        elif command=='index-status':
            if set(value)-{'max_documents','after_id'}:raise ValueError('invalid_index_status_fields')
            result=index.status(**value)
        elif command=='index-retry':
            if set(value)!={'document_id'}:raise ValueError('index_document_id_required')
            result=index.retry(value['document_id'])
        elif command=='index-run':
            if set(value)-{'max_jobs','max_seconds','lease_seconds'}:raise ValueError('invalid_index_run_fields')
            max_jobs=value.get('max_jobs',100);max_seconds=value.get('max_seconds',60)
            lease_seconds=value.get('lease_seconds',30)
            if (type(max_jobs) is not int or not 1<=max_jobs<=1000 or
                    type(max_seconds) not in (int,float) or not 1<=max_seconds<=300):
                raise ValueError('index_worker_bound')
            embedder=store.semantic_embedder
            if embedder is None:raise ValueError('local_embedding_model_required')
            deadline=time.monotonic()+max_seconds;results=[]
            while len(results)<max_jobs and time.monotonic()<deadline:
                item=index.run_once(embedder,lease_seconds=lease_seconds)
                if item['status']=='idle':break
                results.append(item)
            result={'tenant':tenant,'jobs':len(results),'statuses':{status:sum(
                item['status']==status for item in results) for status in
                ('searchable','pending','failed','no_learning','stale')},
                'elapsed_seconds':max_seconds-max(0,deadline-time.monotonic()),'provider_calls':0}
        else:raise ValueError('invalid_maintenance_command')
    else:
        maintenance=Maintenance(store,worker_config(profile))
        if command=='hold':result=maintenance.hold(value['job_ids'])
        elif command=='retry':result=maintenance.retry(job_id,retain_validated=value.get('retain_validated',False))
        elif command=='admission':result=maintenance.admission(value)
        elif command=='reconcile-usage':result=maintenance.reconcile(value)
        elif command=='usage':result=Meter(store).status()
        elif command=='queue':result=maintenance.worker.status()
        elif command=='export':
            if not output:raise ValueError('new_export_destination_required')
            result=maintenance.export(objects_from_settings(settings),output,**value)
        elif command in {'source','connection'}:
            if not token_file:raise ValueError('authenticated_owner_token_required')
            token_path=Path(token_file).expanduser().resolve()
            if token_path.stat().st_mode&0o077:raise ValueError('token_permissions')
            ctx=store.authenticate(token_path.read_text().strip())
            if command=='source':result=maintenance.lifecycle(ctx,value)
            else:result=maintenance.connection(ctx,value['action'],connection_id,value.get('policy',{}))
        else:raise ValueError('invalid_maintenance_command')
    from agenthub.backend_ops import build_identity
    receipt={'id':uuid.uuid4().hex,'command':command,'tenant':tenant,'timestamp':time.time(),
        'build':build_identity(),'provider_calls':0,'status':'passed','result':result}
    _write(profile/'maintenance-receipts'/(receipt['id']+'.json'),receipt)
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['hold','retry','admission','reconcile-usage','usage','queue',
        'source','connection','export','tenant-retire','index-reconcile','index-status',
        'index-run','index-retry'])
    parser.add_argument('--profile',required=True);parser.add_argument('--tenant',required=True)
    parser.add_argument('--input');parser.add_argument('--token-file');parser.add_argument('--output')
    parser.add_argument('--job-id');parser.add_argument('--connection-id')
    parser.add_argument('--expected-database');parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv);os.umask(0o077)
    result=execute(args.profile,args.tenant,args.command,value=private_input(args.input) if args.input else {},
        token_file=args.token_file,output=args.output,job_id=args.job_id,connection_id=args.connection_id,
        expected_database=args.expected_database,apply=args.apply)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
