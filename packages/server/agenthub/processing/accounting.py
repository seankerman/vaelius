"""Content-free attempt/job/session attribution and honest partial usage totals."""
from collections import Counter, defaultdict
import json
import time
import uuid
from agenthub.processing.storage import table_exists


TABLES = {'model_calls': 'model_call_details', 'calls': 'call_details'}
TOKEN_KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
              'output_tokens', 'reasoning_output_tokens')


def initialize(db, calls_table):
    if calls_table == "model_calls":
        db.require_schema()
        return
    details = TABLES[calls_table]
    db.execute(f'''CREATE TABLE IF NOT EXISTS {details}(call_id INTEGER PRIMARY KEY,
        attempt_id TEXT, job_kind TEXT, job_id TEXT, project TEXT, session TEXT,
        source_sessions TEXT, finished REAL, elapsed_seconds REAL)''')
    db.commit()


def job_context(kind, row, sources):
    row = dict(row)
    sessions = sorted({s['session'] for s in sources if s.get('session')})
    projects = {s['project'] for s in sources if s.get('project')}
    return {'job_kind': kind, 'job_id': row['id'],
            'project': row.get('project') or (next(iter(projects)) if len(projects) == 1 else None),
            'session': row.get('session') or (sessions[0] if len(sessions) == 1 else None),
            'source_sessions': sessions}


def context(value=None):
    """Only application-supplied identifiers enter accounting; never prompt text."""
    value = value or {}
    return {'attempt_id': value.get('attempt_id') or str(uuid.uuid4()),
            'job_kind': value.get('job_kind'), 'job_id': value.get('job_id'),
            'project': value.get('project'), 'session': value.get('session'),
            'source_sessions': value.get('source_sessions', [])}


def record_start(db, calls_table, ident, value):
    value = context(value)
    db.execute(f'''INSERT INTO {TABLES[calls_table]}
        (call_id,attempt_id,job_kind,job_id,project,session,source_sessions) VALUES(?,?,?,?,?,?,?)''',
        (ident,value['attempt_id'],value['job_kind'],value['job_id'],value['project'],value['session'],
         json.dumps(value['source_sessions'])))
    return value


def record_finish(db, calls_table, ident, elapsed=None):
    now = time.time()
    if elapsed is None:
        created = db.execute(f'SELECT created FROM {calls_table} WHERE id=?',(ident,)).fetchone()
        elapsed = max(0,now-created[0]) if created else None
    db.execute(f'UPDATE {TABLES[calls_table]} SET finished=?,elapsed_seconds=? WHERE call_id=?',
               (now,elapsed,ident))


def summarize_rows(rows):
    tokens=Counter();coverage=Counter();purposes=Counter();statuses=Counter();missing=0;elapsed=[]
    for row in rows:
        purposes[row['purpose']]+=1;statuses[row['status']]+=1
        try:usage=json.loads(row['usage'] or '{}')
        except (ValueError,TypeError):usage={}
        if not isinstance(usage,dict):usage={}
        valid=False
        for key in TOKEN_KEYS:
            if type(usage.get(key)) is int and usage[key]>=0:
                tokens[key]+=usage[key];coverage[key]+=1;valid=True
        missing+=not valid
        if row['elapsed_seconds'] is not None:elapsed.append(row['elapsed_seconds'])
    return {'calls':len(rows),'purposes':dict(purposes),'statuses':dict(statuses),
            'reported_tokens':dict(tokens),'token_reporting_calls':dict(coverage),
            'reported_tokens_mean_when_available':{k:v/coverage[k] for k,v in tokens.items()},
            'calls_without_usage':missing,'timed_calls':len(elapsed),
            'elapsed_seconds_sum':sum(elapsed) if elapsed else None,
            'elapsed_seconds_mean':sum(elapsed)/len(elapsed) if elapsed else None}


def report(db, calls_table='model_calls', since=None, project=None, sessions=None):
    """Read-only accounting, optionally limited to a project and exact sessions.

    Legacy rows remain unattributed; multi-session calls aren't split. When a
    project/session filter is supplied, unlinked calls are omitted rather than
    attributing or exposing work from outside that selection.
    """
    details=TABLES[calls_table]
    exists=(bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(details,)).fetchone())
            if calls_table=="calls" else table_exists(db,details))
    extra=('d.attempt_id,d.job_kind,d.job_id,d.project,d.session,d.source_sessions,d.elapsed_seconds'
           if exists else 'NULL attempt_id,NULL job_kind,NULL job_id,NULL project,NULL session,NULL source_sessions,NULL elapsed_seconds')
    sql=f'SELECT c.id,c.created,c.purpose,c.status,c.usage,{extra} FROM {calls_table} c'
    if exists:sql+=f' LEFT JOIN {details} d ON d.call_id=c.id'
    predicates=[];args=[]
    if since is not None:predicates.append('c.created>=?');args.append(since)
    if project is not None:
        predicates.append('d.project=?' if exists else 'FALSE')
        if exists:args.append(project)
    if sessions is not None:
        sessions=list(sessions)
        if not sessions:
            predicates.append('FALSE')
        elif exists:
            predicates.append('d.session IN ('+','.join('?' for _ in sessions)+')')
            args.extend(sessions)
        else:
            predicates.append('FALSE')
    if predicates:sql+=' WHERE '+' AND '.join(predicates)
    cur=db.execute(sql,args);columns=[d[0] for d in cur.description]
    rows=[dict(row) if hasattr(row,'keys') else dict(zip(columns,row)) for row in cur]
    sessions=defaultdict(list);jobs=defaultdict(list);stages=defaultdict(list);unallocated=Counter()
    for row in rows:
        stages[row['purpose']].append(row)
        if row['session'] is not None:sessions[(row['project'],row['session'])].append(row)
        else:
            reason='legacy' if row['attempt_id'] is None else (
                'multiple_source_sessions' if len(json.loads(row['source_sessions'] or '[]'))>1 else 'no_source_session')
            unallocated[reason]+=1
        if row['job_id'] is not None:jobs[(row['job_kind'],row['job_id'],row['project'],row['session'])].append(row)
    return {'since':since,'total':summarize_rows(rows),
            'instrumented_calls':sum(r['attempt_id'] is not None for r in rows),
            'legacy_unattributed_calls':sum(r['attempt_id'] is None for r in rows),
            'without_single_session':summarize_rows([r for r in rows if r['session'] is None]),
            'session_allocation_gaps':dict(unallocated),
            'stages':[dict(purpose=p,**summarize_rows(group)) for p,group in stages.items()],
            'sessions':[dict(project=p,session=s,**summarize_rows(group)) for (p,s),group in sessions.items()],
            'jobs':[dict(job_kind=k,job_id=j,project=p,session=s,**summarize_rows(group)) for (k,j,p,s),group in jobs.items()],
            'limits':'Only explicitly linked attempts are attributed. An attempt may fail before contacting a model; counts are not provider request counts. Legacy calls remain unknown; multi-session/source-free jobs are not allocated to a guessed session. Missing tokens or timing are unknown. Durations cover harness work, not queue waiting. Cached input is a subset of input and reasoning output may be a subset of output; do not add token categories together. Local and installation ledgers overlap and must not be summed. No money or subscription quota conversion.'}
