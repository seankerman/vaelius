"""Resumable deterministic source passages in the canonical document index.

Originals and observer outputs are retained. Jobs only derive searchable spans;
they never classify, summarize, merge, publish, or invoke a language model.
"""
import hashlib
import json
import time

from agentclient.enterprise_capture import redact
from agenthub.conversation_context import stamp, discovery_cards
from agenthub.local_connectors import native_sections
from agenthub.processing.media_projection import project_media

PREFIX='source-index:v1:'


def queue(db,source_id,*,restart=False):
    return db.execute('''INSERT INTO outbox(id,payload) VALUES(?,?)
        ON CONFLICT(id) '''+('''DO UPDATE SET payload=excluded.payload,status='pending',
        last_error=NULL,next_attempt=0''' if restart else 'DO NOTHING'),
        (PREFIX+source_id,json.dumps({'source_id':source_id,'offset':0}))).rowcount


class SourceIndex:
    def __init__(self,store):self.store=store

    def status(self):
        with self.store.open() as state:
            rows=state.db.execute('''SELECT status,last_error,count(*) n FROM outbox
                WHERE id LIKE ? GROUP BY status,last_error ORDER BY status,last_error''',(PREFIX+'%',)).fetchall()
            passages=state.db.execute("SELECT count(*) FROM enterprise_documents WHERE representation='raw' AND active=1").fetchone()[0]
            return {'jobs':[dict(row) for row in rows],'active_passages':passages,'model_calls':0}

    def reconcile(self,*,after='',limit=1000):
        if not isinstance(after,str) or not 1<=limit<=1000:raise ValueError('source_scan_bound')
        with self.store.open() as state,state.db:
            rows=state.db.execute('''SELECT s.id FROM enterprise_sources s
                JOIN backend_source_revisions r ON r.source_id=s.id
                JOIN backend_connections c ON c.id=r.connection
                WHERE s.tenant=? AND s.active=1 AND s.id>?
                AND r.disposition IN ('accepted','redacted')
                AND jsonb_exists_any(c.source_types::jsonb,ARRAY['agent','conversation'])
                ORDER BY s.id LIMIT ?''',(self.store.tenant_id,after,limit+1)).fetchall()
            queued=sum(queue(state.db,r['id']) for r in rows[:limit])
            return {'queued':queued,'more':len(rows)>limit,
                'next_after':rows[limit-1]['id'] if len(rows)>limit else None,'model_calls':0}

    def run(self,*,max_sources=20,max_seconds=60,max_passages=64):
        if not 1<=max_sources<=10000 or not 1<=max_seconds<=3600 or not 1<=max_passages<=1000:
            raise ValueError('source_worker_bound')
        started=time.monotonic();completed=held=passages=0
        for _ in range(max_sources):
            if time.monotonic()-started>=max_seconds:break
            # Transaction lock is the claim/fence. A crash rolls back this batch;
            # other workers skip it, and committed offsets resume the next batch.
            with self.store.delivery_read_lock(),self.store.open() as state,state.db:
                db=state.db
                job=db.execute('''SELECT id,payload FROM outbox WHERE id LIKE ?
                    AND status='pending' ORDER BY next_attempt,id LIMIT 1 FOR UPDATE SKIP LOCKED''',
                    (PREFIX+'%',)).fetchone()
                if not job:break
                payload=json.loads(job['payload']);source_id=payload['source_id']
                row=db.execute('''SELECT s.*,m.body,m.session,m.turn,r.payload,r.disposition,
                    c.active connection_active,c.permission_observed,c.freshness_seconds
                    FROM enterprise_sources s JOIN memories m ON m.id=s.id
                    JOIN backend_source_revisions r ON r.source_id=s.id
                    JOIN backend_connections c ON c.id=r.connection WHERE s.id=?''',(source_id,)).fetchone()
                if (not row or not row['active'] or not row['connection_active'] or
                        row['permission_observed']+row['freshness_seconds']<time.time()):
                    db.execute("UPDATE outbox SET status='failed',last_error='source_unavailable' WHERE id=?",(job['id'],))
                    held+=1;continue
                event=json.loads(row['payload'])
                if (row['disposition'] not in ('accepted','redacted') or event.get('source_type') not in ('agent','conversation')
                        or redact(event)[0]!=event):
                    db.execute("UPDATE outbox SET status='failed',last_error='source_not_eligible_or_unredacted' WHERE id=?",(job['id'],))
                    held+=1;continue
                from agenthub.source_objects import ObjectMissing, ObjectCorrupt
                try:
                    adapter=getattr(self.store,'conversation_segments',None)
                    if adapter:adapter.verify_source(db,source_id)
                    ranges,media=project_media(row['body'])
                except (ValueError,ObjectMissing,ObjectCorrupt) as error:
                    db.execute("UPDATE outbox SET status='failed',last_error=? WHERE id=?",
                        (type(error).__name__,job['id']))
                    held+=1;continue
                spans=[]
                for left,right in ranges:
                    spans.extend((left+a,left+b) for a,b,_ in native_sections(row['body'][left:right],width=1600))
                offset=payload['offset']
                for left,right in spans[offset:offset+max_passages]:
                    self._passage(db,row,event,left,right,bool(media));passages+=1
                offset+=min(max_passages,len(spans)-offset)
                done=offset>=len(spans)
                db.execute('UPDATE outbox SET payload=?,status=?,next_attempt=?,last_error=NULL WHERE id=?',
                    (json.dumps({'source_id':source_id,'offset':offset}),('searchable' if spans else 'no_text') if done else 'pending',time.time(),job['id']))
                completed+=int(done)
        return {'completed_sources':completed,'passages':passages,'held_sources':held,'model_calls':0}

    def _passage(self,db,row,event,left,right,media):
        from agenthub.processing.knowledge import ingest_observation
        text=row['body'][left:right]
        ident=hashlib.sha256(f"raw-v1:{row['id']}:{row['policy_version']}:{left}:{right}".encode()).hexdigest()
        role=event['event']['role'];speaker=event.get('actor') or role
        metadata={'source_id':row['id'],'role':role,'speaker':speaker,
            'occurred_at':row['occurred_at'],'conversation':event['conversation'],'turn':event['turn'],
            'start':left,'end':right,'media_omitted':media,'source_revision':event['revision']}
        title=f"{row['external_project']} · {speaker} · {row['occurred_at']}"
        claim={'title':title[:140],'lesson':text,'knowledge_type':'source_passage',
            'evidence_status':'discovery_only','source_context':metadata,
            'evidence':[{'source_id':row['id'],'segment_id':f'raw:{left}:{right}'}]}
        db.execute('''INSERT INTO memories(id,session,project,body,kind,created,turn)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING''',
            (ident,row['session'],row['internal_project'],text,'NativeRepresentation',time.time(),row['turn']))
        result=ingest_observation(db,ident,row['internal_project'],row['session'],claim,force_new=True)
        doc=result['document_id']
        db.execute('INSERT INTO backend_native_artifacts VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
            (doc,row['id'],'raw_source',event.get('source_url','')))
        db.execute('INSERT INTO backend_native_spans VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',
            (doc,row['id'],left,right,''))
        db.execute('''INSERT INTO observation_sources(memory_id,source_id)
            SELECT ?,origin_source_id FROM backend_source_links WHERE source_id=?
            ON CONFLICT DO NOTHING''',(ident,row['id']))
        self.store.refresh_documents(db=db,document_id=doc)
        try:occurred=stamp(row['occurred_at'])
        except (ValueError,TypeError,OverflowError):occurred=None
        db.execute('''UPDATE enterprise_documents SET representation='raw',raw_owner=?,source_time=?,
            source_captured=? WHERE id=?''',(row['owner'],occurred,row['created'],doc))


def search(store,ctx,value,filters,*,candidate_pool=False):
    """Source discovery through the existing authorized hybrid candidate query."""
    rows=store.candidates(ctx,value['query'],project=value.get('project'),
        filters=dict(filters,source_as_of=value.get('as_of'),time_mode=value.get('time_mode')),
        limit=store.retrieval_candidate_limit,vector=store.hybrid_enabled,lexical=store.retrieval_lexical_enabled)
    cards=[]
    for row in rows:
        claim=json.loads(row['claim_json']);metadata=claim.get('source_context',{})
        cards.append({'id':row['document_id'],'revision':row['revision_id'],'title':claim.get('title',''),
            'text':claim.get('lesson',''),'event_time':metadata.get('occurred_at'),
            'parent_ids':[metadata['source_id']] if metadata.get('source_id') else None})
    if value.get('mode')=='automatic' and value.get('session'):
        from agenthub.processing.context_delivery import current_epoch
        with store.open() as state:
            receiver=store._receiver(ctx,value['session']);epoch=str(current_epoch(state.db,receiver)['epoch'])
            seen={(r['document_id'],r['revision']) for r in state.db.execute('''SELECT document_id,revision
                FROM enterprise_receipts WHERE session=? AND epoch=? AND status='offered'
                AND document_id=ANY(?::text[])''',(receiver,epoch,[c['id'] for c in cards]))}
            cards=[c for c in cards if (c['id'],c['revision']) not in seen]
    if candidate_pool:
        return {'results':[{'id':c['id'],'revision':c['revision'],'title':c['title'],
            'lesson':c['text'],'evidence_status':'discovery_only'} for c in cards],
            'answerable':False,'_candidate_pool':True}
    output=discovery_cards(cards,limit=value.get('limit',5),max_chars=1500 if value.get('mode')=='automatic' else 3800)
    output['results']=output.pop('records')
    output['support']='partial' if output['results'] else 'none'
    return store.validate_search_delivery(ctx,value,output)
