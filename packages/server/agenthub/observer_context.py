"""Bounded original context in the canonical observer checkpoint.

This is an acceleration artifact, never a searchable corpus. Current revisions,
policy and lifecycle are checked before each reuse; provider handles are separate.
"""
import hashlib
import json
import time
from agenthub.processing.episode_pipeline import sources_for_job
from agenthub.processing.media_projection import context_text

VERSION = 1


def fingerprint(db, sources):
    result = {}
    ids = [s['id'] for s in sources]
    for start in range(0, len(ids), 400):
        batch = ids[start:start+400]
        rows = db.execute('''SELECT m.id,m.active,m.project,m.session,m.turn,
            r.digest,r.revision,s.active source_active,s.policy_version,
            COALESCE(meta.source_role,'episode_evidence') source_role,
            COALESCE(meta.tool_name,'') tool_name
            FROM memories m JOIN backend_source_revisions r ON r.source_id=m.id
            JOIN enterprise_sources s ON s.id=m.id
            LEFT JOIN source_event_metadata meta ON meta.source_id=m.id
            WHERE m.id IN ('''+','.join('?' for _ in batch)+')', batch).fetchall()
        for row in rows:
            value = dict(row)
            if not value['active'] or not value['source_active']:
                raise ValueError('observer_context_source_changed')
            result[value['id']] = value
    if set(result) != set(ids):raise ValueError('observer_context_source_changed')
    return result


def checkpoint_index(db, sources, row, config, observer):
    # Original bodies are kept only for bounded complete turns. Oversized raw
    # media turns remain in the authority and get an explicit reconstruction gap.
    bound = min(512000, max(32000, int(config.get('backend_worker', {}).get('max_context_chars',128000))*2))
    groups = {}
    for source in sources:groups.setdefault(source['turn'], []).append(source)
    selected=[];used=0;omitted=0
    ordered=sorted(groups.values(),key=lambda g:max(v['created'] for v in g))
    # Keep an early original anchor alongside recent complete turns.
    prioritized=([ordered[0]] if ordered else [])+list(reversed(ordered[1:]))
    for group in prioritized:
        size=len(json.dumps(group, ensure_ascii=True))
        if used+size<=bound:selected.extend(group);used+=size
        else:omitted+=len(group)
    result={'version':VERSION,'sources':sorted(selected,key=lambda s:(s['created'],s['id'])),
        'fingerprints':fingerprint(db,selected),'through_created':row['created'],'through_id':row['id'],
        'permission_hash':observer['permission_hash'],'source_epoch':observer['source_epoch'],
        'media_policy':config.get('episode_curation',{}).get('media_policy'),
        'omitted_sources':omitted,'bounded_originals':True}
    result['sha256']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result


def load_context(state, job, config, *, deadline=None):
    db=state.db;start=time.monotonic()
    def bounded():
        if deadline is not None and time.monotonic()>=deadline:raise ValueError('worker_preparation_bound')
    bounded()
    row=db.execute('SELECT * FROM curation_episode_jobs WHERE id=?',(job['episode_job'],)).fetchone()
    current=sources_for_job(db,row);loaded=time.monotonic()
    observer=dict(db.execute('SELECT * FROM backend_observers WHERE id=?',(job['observer_id'],)).fetchone())
    cache=json.loads(observer['checkpoint'] or '{}').get('context_index');previous=[];kind='cold'
    if (isinstance(cache,dict) and cache.get('version')==VERSION and
            cache.get('permission_hash')==observer['permission_hash'] and
            cache.get('source_epoch')==observer['source_epoch'] and
            cache.get('media_policy')==config.get('episode_curation',{}).get('media_policy') and
            (cache['through_created'],cache['through_id'])<(row['created'],row['id'])):
        digest=hashlib.sha256(json.dumps({k:v for k,v in cache.items() if k!='sha256'},sort_keys=True).encode()).hexdigest()
        if digest!=cache.get('sha256') or fingerprint(db,cache['sources'])!=cache['fingerprints']:
            raise ValueError('observer_context_source_changed')
        previous=cache['sources'];kind='warm'
    # Do not select progress bodies for each prior job. The source IDs, ordering
    # and completed/extraction boundary are sufficient to load a bounded delta.
    query="""SELECT id,project,session,turn,source_ids,created FROM curation_episode_jobs
        WHERE project=? AND session=? AND generation_id=?
        AND (status IN ('done','no_learning') OR stage='extraction_ready' OR (stage LIKE 'resolve%%' AND progress LIKE '%%"extraction":%%'))
        AND (created<? OR (created=? AND id<?)) AND id!=?"""
    args=[row['project'],row['session'],row['generation_id'],row['created'],row['created'],row['id'],row['id']]
    if kind=='warm':
        query+=' AND (created>? OR (created=? AND id>?))'
        args += [cache['through_created'],cache['through_created'],cache['through_id']]
    prior_jobs=db.execute(query+' ORDER BY created,id',args).fetchall()
    gaps=cache.get('omitted_sources',0) if kind=='warm' else 0
    for prior in prior_jobs:
        bounded()
        try:previous.extend(sources_for_job(db,prior))
        except ValueError:gaps+=len(json.loads(prior['source_ids']))
    # Replayed stages need their exact original context, even if the continuing
    # bounded window changed. Recover only their input turns, not the whole chat.
    progress=json.loads(row['progress'] or '{}')
    source_only=bool(progress.get('source_rederivation',{}).get('source_only'))
    if source_only:previous=[]
    required=set(progress.get('extraction',{}).get('input_manifest',{}))
    for output in progress.get('stage_outputs',[]):
        required.update(output.get('input_manifest',{}))
        required.update(v['original_id'] for v in output.get('reference_manifest',{}).get('entries',{}).values() if v['kind']=='e')
    missing=required-{v['id'] for v in current+previous}
    if missing:
        turns=[]
        for offset in range(0,len(missing),400):
            ids=sorted(missing)[offset:offset+400]
            turns.extend(r[0] for r in db.execute('SELECT DISTINCT turn FROM memories WHERE project=? AND session=? AND id IN ('+','.join('?' for _ in ids)+')',(row['project'],row['session'],*ids)))
        for turn in set(turns):
            bounded()
            prior=db.execute('SELECT id,project,session,turn,source_ids,created FROM curation_episode_jobs WHERE project=? AND session=? AND turn=? AND generation_id=?',(row['project'],row['session'],turn,row['generation_id'])).fetchone()
            if prior:previous.extend(sources_for_job(db,prior))
    previous=list({s['id']:s for s in previous if s['id'] not in {v['id'] for v in current}}.values())
    source_loaded_at=time.monotonic();loaded_bytes=sum(len(v['body']) for v in current+previous)
    from agentclient.cleaning import terms
    from agenthub.processing.durable_memory import packets_for
    from agenthub.processing.continuous_observer import _whole_turn
    settings=config.get('episode_curation',{});media=settings.get('media_policy')
    def readable(s):return context_text(s['body']) if media else s['body']
    query_terms=set(terms(' '.join(readable(s) for s in current)));groups={}
    for source in previous:groups.setdefault(source['turn'],[]).append(source)
    budget=max(1000,int(config.get('backend_worker',{}).get('max_context_chars',128000))//3)
    selected=[];used=0
    source_bound=int(config.get('backend_worker',{}).get('max_context_sources',256))
    if not 1<=source_bound<=10000:raise ValueError('observer_source_bound')
    ranked=sorted(groups.values(),key=lambda group:(bool(required & {s['id'] for s in group}),
        len(query_terms & set(terms(' '.join(readable(s) for s in group)))),max(s['created'] for s in group)),reverse=True)
    projection_at=time.monotonic()
    for group in ranked:
        bounded()
        if media:
            packets,_=packets_for(group,media_policy=media,
                max_events=int(settings.get('max_events_per_stage',120)),
                max_chars=int(settings.get('max_chars_per_stage',100000)),
                max_stages=int(settings.get('max_stages',32)),
                split_oversized_events=settings.get('split_oversized_events',False))
            size=len(json.dumps(_whole_turn(packets),ensure_ascii=True))
        else:size=len(json.dumps(group,ensure_ascii=True))
        if (used+size<=budget and len(selected)+len(group)+len(current)<=source_bound) or required & {s['id'] for s in group}:
            selected.extend(group);used+=size
        else:gaps+=len(group)
    previous=sorted(selected,key=lambda s:(s['created'],s['id']))
    selection_at=time.monotonic()
    bounded()
    # Only sources that actually enter the model/validation packet are influences.
    with db:
        prior_count=db.execute('SELECT count(*) FROM backend_observer_dependencies WHERE observer_id=?',(job['observer_id'],)).fetchone()[0]
        compact=int(config.get('backend_worker',{}).get('compact_after_chars',256000))
        if prior_count+len(current)>source_bound or observer['context_chars']>=compact or config.get('backend_execution',{}).get('kind')=='operator_api':
            db.execute("UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,context_chars=0,observer_epoch=observer_epoch+1,status='reconstruct' WHERE id=?",(job['observer_id'],))
            db.execute('DELETE FROM backend_observer_dependencies WHERE observer_id=?',(job['observer_id'],))
        elif observer['provider_session'] or observer['pending_session']:
            # Native continuation can still see earlier turns absent from this
            # bounded reconstruction packet. They remain actual input dependencies.
            db.execute('''INSERT INTO backend_processing_dependencies(episode_job,source_id,policy_version)
                SELECT ?,source_id,policy_version FROM backend_observer_dependencies WHERE observer_id=?
                ON CONFLICT(episode_job,source_id) DO NOTHING''',(row['id'],job['observer_id']))
        db.executemany('INSERT INTO backend_processing_dependencies VALUES(?,?,?) ON CONFLICT(episode_job,source_id) DO UPDATE SET policy_version=excluded.policy_version',[(row['id'],s['id'],s['policy_version']) for s in previous+current])
        db.executemany('INSERT INTO backend_observer_dependencies VALUES(?,?,?) ON CONFLICT(observer_id,source_id) DO UPDATE SET policy_version=excluded.policy_version',[(job['observer_id'],s['id'],s['policy_version']) for s in previous+current])
        if gaps:db.execute('INSERT INTO backend_worker_receipts(job_id,purpose,status,usage,created) VALUES(?,?,?,?,?)',(job['id'],'context_reconstruction','bounded_original_context_gap',json.dumps({'omitted_sources':gaps}),time.time()))
    profile={'cache':kind,'current_load_seconds':loaded-start,'original_context_load_seconds':source_loaded_at-loaded,
        'ranking_projection_seconds':projection_at-source_loaded_at,'packet_selection_seconds':selection_at-projection_at,
        'dependency_seconds':time.monotonic()-selection_at,'loaded_bytes':loaded_bytes,'context_seconds':time.monotonic()-loaded,
        'elapsed_seconds':time.monotonic()-start,'delta_jobs':len(prior_jobs),'selected_sources':len(current+previous),
        'selected_bytes':sum(len(s['body']) for s in current+previous),'omitted_sources':gaps,'selected_packet_chars':used}
    return row,current,previous,profile
