"""Content-free enterprise usage, admission, readiness and bounded local probes."""
import hashlib
import json
import math
import statistics
import time
from agenthub.enterprise import Conflict

class AdmissionExhausted(RuntimeError):pass

class Meter:
    def __init__(self,store):self.store=store
    def configure(self,*,max_parallel=4,max_attempts=100000,enabled=True):
        if type(max_parallel) is not int or max_parallel<1 or type(max_attempts) is not int or max_attempts<1:raise ValueError('invalid_admission')
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO cloud_admission VALUES(?,?,?,?) ON CONFLICT(tenant)
                DO UPDATE SET max_parallel=excluded.max_parallel,max_attempts=excluded.max_attempts,enabled=excluded.enabled''',
                (self.store.tenant_id,max_parallel,max_attempts,int(enabled)))
    def reserve(self,attempt_id,job_id,purpose,*,retry=False,lease_seconds=180):
        if not attempt_id or not job_id or not purpose or not 1<=lease_seconds<=600:raise ValueError('invalid_attempt')
        with self.store.open() as state,state.db:
            row=state.db.execute('SELECT * FROM cloud_admission WHERE tenant=? FOR UPDATE',(self.store.tenant_id,)).fetchone()
            if not row or not row['enabled']:raise AdmissionExhausted('tenant_processing_disabled')
            if state.db.execute('SELECT 1 FROM cloud_usage_attempts WHERE id=?',(attempt_id,)).fetchone():raise Conflict()
            total=state.db.execute('SELECT count(*) FROM cloud_usage_attempts WHERE tenant=?',(self.store.tenant_id,)).fetchone()[0]
            busy=state.db.execute("SELECT count(*) FROM cloud_usage_attempts WHERE tenant=? AND status IN ('reserved','dispatched') AND lease_until>?",(self.store.tenant_id,time.time())).fetchone()[0]
            if busy>=row['max_parallel']:raise AdmissionExhausted('tenant_processing_admission')
            state.db.execute('''INSERT INTO cloud_usage_attempts(id,tenant,job_id,purpose,status,is_retry,reserved,lease_until)
                VALUES(?,?,?,?,\'reserved\',?,?,?)''',(attempt_id,self.store.tenant_id,job_id,purpose,int(retry),time.time(),time.time()+lease_seconds))
        return attempt_id
    def dispatched(self,attempt_id):
        with self.store.open() as state,state.db:
            if state.db.execute("UPDATE cloud_usage_attempts SET status='dispatched' WHERE id=? AND tenant=? AND status='reserved' AND lease_until>?",(attempt_id,self.store.tenant_id,time.time())).rowcount!=1:raise Conflict()
    def finish(self,attempt_id,status,*,usage=None,result=None,latency=None,price=None):
        if status not in {'returned','failed','uncertain','cancelled'}:raise ValueError('invalid_usage_status')
        usage=usage or {}
        for key,value in usage.items():
            if key not in {'input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','reasoning_output_tokens'} or type(value) is not int or value<0:raise ValueError('invalid_usage')
        digest=hashlib.sha256(json.dumps([status,usage,result],sort_keys=True).encode()).hexdigest()
        with self.store.open() as state,state.db:
            row=state.db.execute('SELECT * FROM cloud_usage_attempts WHERE id=? AND tenant=? FOR UPDATE',(attempt_id,self.store.tenant_id)).fetchone()
            if not row:raise ValueError('usage_attempt_missing')
            if row['finished'] is not None:
                if row['result_digest']!=digest:raise Conflict()
                return {'attempt_id':attempt_id,'duplicate':True}
            estimate,version=_estimated_cost(usage,price)
            state.db.execute('''UPDATE cloud_usage_attempts SET status=?,finished=?,usage=?,result_digest=?,latency=?,
                price_version=?,estimated_cost=?,lease_until=0 WHERE id=?''',
                (status,time.time(),json.dumps(usage),digest,latency,version,estimate,attempt_id))
        return {'attempt_id':attempt_id,'duplicate':False,'estimated_cost':estimate,'billed_cost':None}
    def reconcile(self,attempt_id,status='returned',*,usage,result,latency=None,price=None):
        # A confirmed late return updates one attempt, never reserves another.
        if status!='returned':raise ValueError('confirmed_return_required')
        digest=hashlib.sha256(json.dumps(['returned',usage,result],sort_keys=True).encode()).hexdigest()
        with self.store.open() as state,state.db:
            row=state.db.execute('SELECT * FROM cloud_usage_attempts WHERE id=? AND tenant=? FOR UPDATE',(attempt_id,self.store.tenant_id)).fetchone()
            if not row:raise ValueError('usage_attempt_missing')
            if row['status']=='returned':
                if row['result_digest']!=digest:raise Conflict()
                return {'attempt_id':attempt_id,'duplicate':True}
            if row['status']!='uncertain':raise Conflict()
            for key,value in usage.items():
                if key not in {'input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','reasoning_output_tokens'} or type(value) is not int or value<0:raise ValueError('invalid_usage')
            state.db.execute('INSERT INTO cloud_usage_reconciliations VALUES(?,?,?,?,?)',
                (attempt_id,row['status'],'returned',digest,time.time()))
            estimate,version=_estimated_cost(usage,price)
            state.db.execute("UPDATE cloud_usage_attempts SET status='returned',finished=?,usage=?,result_digest=?,latency=?,estimated_cost=?,price_version=?,lease_until=0 WHERE id=?",
                (time.time(),json.dumps(usage),digest,latency,estimate,version,attempt_id))
        return {'attempt_id':attempt_id,'duplicate':False,'reconciled':True}
    def metric(self,ident,kind,amount,*,details=None):
        if type(amount) is not int or amount<0 or not kind:raise ValueError('invalid_metric')
        details=details or {}
        if set(details)-{'source_revision','generation','status','stage','elapsed_ms'}:raise ValueError('metric_private_field')
        with self.store.open() as state,state.db:
            prior=state.db.execute('SELECT tenant,kind,amount,details FROM cloud_metrics WHERE id=?',(ident,)).fetchone()
            if prior and (prior['tenant']!=self.store.tenant_id or prior['kind']!=kind or prior['amount']!=amount or json.loads(prior['details'])!=details):raise Conflict()
            state.db.execute('INSERT INTO cloud_metrics VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (ident,self.store.tenant_id,kind,amount,time.time(),json.dumps(details)))
            actual=state.db.execute('SELECT tenant,kind,amount,details FROM cloud_metrics WHERE id=?',(ident,)).fetchone()
            if actual['tenant']!=self.store.tenant_id or actual['kind']!=kind or actual['amount']!=amount or json.loads(actual['details'])!=details:raise Conflict()
    def status(self):
        with self.store.open() as state:
            attempts=state.db.execute('SELECT status,is_retry,usage,latency,estimated_cost FROM cloud_usage_attempts WHERE tenant=?',(self.store.tenant_id,)).fetchall()
            counts={};tokens={};unknown=0;latencies=[]
            for row in attempts:
                counts[row['status']]=counts.get(row['status'],0)+1
                usage=json.loads(row['usage'])
                if not usage:unknown+=1
                for key,value in usage.items():tokens[key]=tokens.get(key,0)+value
                if row['latency'] is not None:latencies.append(row['latency'])
            metrics={r[0]:int(r[1]) for r in state.db.execute('SELECT kind,sum(amount) FROM cloud_metrics WHERE tenant=? GROUP BY kind',(self.store.tenant_id,))}
            return {'tenant':self.store.tenant_id,'attempts':len(attempts),'statuses':counts,
                'retry_attempts':sum(r['is_retry'] for r in attempts),'reported_tokens':tokens,
                'without_reported_usage':unknown,'metrics':metrics,'latency':percentiles(latencies),
                'estimated_cost':sum(r['estimated_cost'] or 0 for r in attempts),'billed_cost':None}

def percentiles(values):
    if not values:return {'count':0,'p50':None,'p95':None}
    ordered=sorted(values)
    return {'count':len(ordered),'p50':statistics.median(ordered),'p95':ordered[min(len(ordered)-1,int(len(ordered)*.95))]}

def require_reconciled(store):
    with store.open() as state:
        row=state.db.execute('SELECT ready FROM cloud_recovery_state WHERE singleton=1').fetchone()
        if row and not row[0]:raise ValueError('restore_requires_reconciliation')

def hold_restore(store,reason,manifest):
    with store.open() as state,state.db:
        state.db.execute('''INSERT INTO cloud_recovery_state VALUES(1,0,?,?,?) ON CONFLICT(singleton)
            DO UPDATE SET ready=0,reason=excluded.reason,manifest=excluded.manifest,updated=excluded.updated''',
            (reason,json.dumps(manifest),time.time()))

def _estimated_cost(usage,price):
    estimate=None;version=None
    if price:
        version=price['version'];cached=usage.get('cached_input_tokens',0);writes=usage.get('cache_write_input_tokens',0)
        if not isinstance(version,str) or not version or len(version)>80:raise ValueError('invalid_price_version')
        rates=[price[key] for key in ('input','cached_input','output')]
        write_rate=price.get('cache_write_input',price['input']);rates.append(write_rate)
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in rates):raise ValueError('invalid_price_rate')
        estimate=(max(0,usage.get('input_tokens',0)-cached-writes)*price['input']+cached*price['cached_input']+writes*write_rate+usage.get('output_tokens',0)*price['output'])/1_000_000
    return estimate,version
