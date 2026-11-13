"""Content-free usage metrics, readiness and bounded local probes."""
import json
import statistics
import time
from agenthub.enterprise import Conflict

class Meter:
    def __init__(self,store):self.store=store
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
            metrics={r[0]:int(r[1]) for r in state.db.execute('SELECT kind,sum(amount) FROM cloud_metrics WHERE tenant=? GROUP BY kind',(self.store.tenant_id,))}
            return {'tenant':self.store.tenant_id,'metrics':metrics}

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
