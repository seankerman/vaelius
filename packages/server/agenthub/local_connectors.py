"""Explicit local adapters for documents, conversation exports and structured records.

These simulate source/ACL/revision semantics, not vendor OAuth integrations.
The operator selects paths and maximum readers. Content never grants readers.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import time

from agentclient.enterprise_capture import normalize_capture, redact
from agentclient.general_contract import canonical, digest


def initialize(db):
    db.require_schema()


def native_sections(text, *, width=850, markdown=False):
    """Exact character spans in the redacted original; preserve heading sections."""
    headings=list(re.finditer(r'^#{1,6}[ \t]+([^\n]+)',text,re.M)) if markdown else []
    boundaries=sorted({0,len(text),*(m.start() for m in headings)})
    names={m.start():m.group(1).strip() for m in headings}
    heading=''
    for first,last in zip(boundaries,boundaries[1:]):
        heading=names.get(first,heading)
        start=first
        while start<last:
            end=min(last,start+width)
            if end<last:
                cut=max(text.rfind('\n',start+width//2,end),text.rfind(' ',start+width//2,end))
                if cut>=0:end=cut+1
            if text[start:end].strip():yield start,end,heading
            start=end


class Connector:
    def __init__(self,store):
        self.store=store
        with store.open() as state:initialize(state.db)

    def enroll(self,ctx,ident,adapter,path,project,*,visibility='private',reader_ids=None,policy_file=None):
        if adapter not in {'directory','conversations','records'}:raise ValueError('unsupported_local_adapter')
        source=Path(path).expanduser().resolve(strict=True)
        if adapter=='directory' and not source.is_dir() or adapter!='directory' and not source.is_file():raise ValueError('adapter_path_type')
        connection='connector-'+ident
        source_types=['document' if adapter=='directory' else 'conversation' if adapter=='conversations' else 'record']
        self.store.enroll_connection(ctx,connection,'local-'+ident,project,source_types,
            visibility=visibility,reader_ids=reader_ids,
            capabilities={'snapshot':'complete local file tree/export','incremental':'revision upsert',
                'permissions':'operator mapped connection-wide; no nested inherited groups',
                'unsupported':'binary parsers, upstream OAuth/group authority'})
        policy=Path(policy_file).expanduser().resolve(strict=True) if policy_file else None
        with self.store.open() as state,state.db:
            state.db.execute('INSERT INTO backend_connectors(id,connection,adapter,path,policy_file,max_visibility,max_readers,updated) VALUES(?,?,?,?,?,?,?,?)',
                (ident,connection,adapter,str(source),str(policy) if policy else None,visibility,json.dumps(sorted(reader_ids or [])),time.time()))
        return {'connector':ident,'connection':connection,'adapter':adapter,'path':str(source)}

    def _snapshot(self,row):
        path=Path(row['path']);items=[];complete=True
        if row['adapter']=='directory':
            if not path.is_dir():raise ValueError('connector_snapshot_unavailable')
            def onerror(error):raise error
            for folder,dirs,files in os.walk(path,followlinks=False,onerror=onerror):
                for entry in dirs+files:
                    candidate=Path(folder)/entry
                    if candidate.is_symlink():raise ValueError('connector_symlink_excluded')
                for filename in sorted(files):
                    target=Path(folder)/filename
                    if target.suffix.lower() not in {'.md','.txt'}:continue
                    if not target.resolve().is_relative_to(path):raise ValueError('connector_path_escape')
                    before=target.stat()
                    if before.st_size>4*1024*1024:raise ValueError('connector_document_too_large')
                    text=target.read_text(encoding='utf8')
                    after=target.stat()
                    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('connector_file_changed_during_read')
                    external=str(target.relative_to(path))
                    title=next((line.lstrip('# ').strip() for line in text.splitlines() if line.strip()),external)
                    items.append({'id':external,'title':title,'source_type':'document',
                        'blocks':[{'type':'text','value':text}], 'occurred_at':datetime.fromtimestamp(after.st_mtime,timezone.utc).isoformat(),
                        'source_url':'local:'+external})
        else:
            if not path.is_file() or path.stat().st_size>16*1024*1024:raise ValueError('connector_export_unavailable_or_large')
            data=json.loads(path.read_text());complete=data.get('complete',True)
            if type(complete) is not bool:raise ValueError('invalid_snapshot_completeness')
            if row['adapter']=='records':
                for record in data['records']:
                    if record.get('deleted'):continue
                    if not isinstance(record.get('fields'),dict):raise ValueError('invalid_record_fields')
                    items.append({'id':record['id'],'title':record['id'],'source_type':'record',
                        'blocks':[{'type':'record_fields','value':record}],
                        'occurred_at':record.get('updated_at','unknown'),'source_url':record.get('url','')})
            else:
                for thread in data['threads']:
                    if thread.get('deleted'):continue
                    for order,message in enumerate(thread['messages']):
                        if message.get('deleted'):continue
                        if message.get('role','user') not in {'user','assistant'}:raise ValueError('unsupported_conversation_role')
                        items.append({'id':thread['id']+':'+message['id'],'title':thread['id'],
                            'source_type':'conversation','conversation':thread['id'],'turn':thread['id'],
                            'role':message.get('role','user'),'actor':message.get('actor',''),
                            'order':order,
                            'blocks':[{'type':'text','value':message['text']},
                                {'type':'record_fields','value':{'participants':thread.get('participants',[]),'message_revision':message.get('revision','unknown')}}],
                            'occurred_at':message.get('timestamp','unknown'),'source_url':message.get('url','')})
                    items.append({'id':thread['id']+':end','title':thread['id'],'source_type':'conversation',
                        'conversation':thread['id'],'turn':thread['id'],'role':'source','complete':True,
                        'order':len(thread['messages']),
                        'blocks':[], 'occurred_at':'unknown','source_url':'',
                        'snapshot_revision':thread.get('revision','unknown')})
        if len(items)>10000:raise ValueError('connector_snapshot_item_bound')
        if len({item['id'] for item in items})!=len(items):raise ValueError('connector_duplicate_upstream_id')
        return sorted(items,key=lambda item:item['id']),complete

    def _native(self,source_id,item):
        from agenthub.processing.knowledge import accept_local_candidate
        if item['source_type']=='conversation':return 0
        with self.store.open() as state,state.db:
            db=state.db;source=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
            memory=db.execute('SELECT * FROM memories WHERE id=?',(source_id,)).fetchone()
            text='\n'.join(block['value'] if isinstance(block['value'],str) else json.dumps(block['value'],ensure_ascii=False,sort_keys=True) for block in item['blocks'])
            count=0
            # Deterministic exact spans, never an LLM summary or factual endorsement.
            for start,end,heading in native_sections(text,markdown=item['source_type']=='document'):
                chunk=text[start:end]
                ident=hashlib.sha256(canonical([source_id,source['policy_version'],start,chunk])).hexdigest()
                db.execute('INSERT INTO memories(id,session,project,body,kind,created,turn) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                    (ident,memory['session'],source['internal_project'],chunk,'NativeRepresentation',time.time(),''))
                db.execute('INSERT INTO observation_sources VALUES(?,?) ON CONFLICT DO NOTHING',(ident,source_id))
                result=accept_local_candidate(db,ident,source['internal_project'],memory['session'],
                    ((item['title'] or item['id']) + (' / '+heading if heading and heading!=item['title'] else ''))[:140],chunk,[source_id])
                db.execute('''INSERT INTO backend_native_artifacts VALUES(?,?,?,?) ON CONFLICT(document_id)
                    DO UPDATE SET source_id=excluded.source_id,artifact_kind=excluded.artifact_kind,source_url=excluded.source_url''',
                    (result['document_id'],source_id,'native_'+item['source_type'],item['source_url']))
                db.execute('''INSERT INTO backend_native_spans VALUES(?,?,?,?,?) ON CONFLICT(document_id)
                    DO UPDATE SET source_id=excluded.source_id,start=excluded.start,"end"=excluded."end",heading=excluded.heading''',
                    (result['document_id'],source_id,start,end,heading))
                self.store.refresh_documents(db=db,document_id=result['document_id'])
                db.execute("UPDATE enterprise_documents SET representation='source' WHERE id=?",(result['document_id'],))
                count+=1
        self.store.refresh_documents();return count

    def sync(self,ctx,ident,*,dry_run=False,max_items=1000):
        if not 1<=max_items<=10000:raise ValueError('connector_item_bound')
        with self.store.open() as state:
            row=state.db.execute('SELECT * FROM backend_connectors WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('unknown_connector')
            connection=self.store._connection(state.db,ctx,row['connection'],allow_stale=True)
            known={r['external_id']:dict(r) for r in state.db.execute('SELECT * FROM backend_connector_items WHERE connector=?',(ident,))}
        # Read/validate the complete snapshot before committing any tombstones.
        items,complete=self._snapshot(row)
        policy={'visibility':connection['visibility'],'reader_ids':json.loads(connection['reader_ids'])}
        if row['policy_file']:
            policy=json.loads(Path(row['policy_file']).read_text())
            if set(policy)!={'visibility','reader_ids'}:raise ValueError('invalid_operator_policy')
            rank={'private':0,'team':1,'organization':2}
            if policy['visibility'] not in rank or rank[policy['visibility']]>rank[row['max_visibility']]:raise ValueError('policy_exceeds_enrollment')
            maximum=set(json.loads(row['max_readers']))
            if maximum and (not policy['reader_ids'] or not set(policy['reader_ids'])<=maximum):raise ValueError('readers_exceed_enrollment')
        changed=policy['visibility']!=connection['visibility'] or sorted(policy['reader_ids'])!=json.loads(connection['reader_ids'])
        cleaned=[]; manifests={}
        for item in items:
            safe,manifest=redact(item);cleaned.append(safe);manifests[safe['id']]=manifest
        hashes={item['id']:digest(item) for item in cleaned}
        updates=[item for item in cleaned if item['id'] not in known or known[item['id']]['content_hash']!=hashes[item['id']] or not known[item['id']]['active']]
        deletes=[item for key,item in known.items() if key not in hashes and item['active']] if complete else []
        if len(updates)+len(deletes)>max_items:raise ValueError('connector_batch_bound_no_checkpoint')
        report={'connector':ident,'upserts':len(updates),'deletions':len(deletes),'policy_changed':changed,
            'complete_snapshot':complete,'dry_run':dry_run,'model_calls':0}
        if dry_run:return report
        self.store.connection_policy(ctx,row['connection'],**policy)
        if changed:
            # Reprepare under new policy without manufacturing a content revision.
            for item in cleaned:
                if item['id'] in known and known[item['id']]['active'] and item not in updates:
                    self._native(known[item['id']]['source_id'],item)
        for item in updates:
            revision=str(known.get(item['id'],{}).get('revision',0)+1)
            role=item.get('role','source');kind='UserPromptSubmit' if role=='user' else 'Stop' if item.get('complete') else 'AssistantMessage' if role=='assistant' else 'NativeSource'
            value=normalize_capture({'hook_event_name':kind,'event_id':item['id'],'session_id':item.get('conversation',''),
                'turn_id':item.get('turn',''),'revision':revision,'timestamp':item['occurred_at'],
                'source_order':item.get('order'),
                'speaker':item.get('actor',''),'source_url':item['source_url']},connection['project'],row['connection'],source_type=item['source_type'],origin='local_connector')
            value['blocks'],value['redactions']=redact(item['blocks'],'$.blocks')
            value['redactions']+=manifests[item['id']]
            value['disposition']='redacted' if value['redactions'] else 'accepted'
            value['event']['role']=role;value['event']['complete']=bool(item.get('complete'))
            result=self.store.ingest_general(ctx,value)
            self._native(result['source_id'],item)
            with self.store.open() as state,state.db:
                state.db.execute('''INSERT INTO backend_connector_items VALUES(?,?,?,?,?,1)
                    ON CONFLICT(connector,external_id) DO UPDATE SET content_hash=excluded.content_hash,
                    revision=excluded.revision,source_id=excluded.source_id,active=1''',
                    (ident,item['id'],hashes[item['id']],int(revision),result['source_id']))
        for item in deletes:
            with self.store.open() as state:
                current=state.db.execute('SELECT source_version FROM enterprise_sources WHERE id=?',(item['source_id'],)).fetchone()[0]
            self.store.lifecycle(ctx,{'version':'enterprise-local-1','target_id':item['source_id'],
                'expected_revision':str(current),'idempotency_key':'connector-delete-'+item['source_id'],
                'reason':'complete source snapshot deletion','operation':'delete'})
            with self.store.open() as state,state.db:state.db.execute('UPDATE backend_connector_items SET active=0 WHERE connector=? AND external_id=?',(ident,item['external_id']))
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE backend_connectors SET cursor=cursor+1,status=?,updated=? WHERE id=?",('synced' if complete else 'incomplete_snapshot',time.time(),ident))
        return report

    def status(self,ctx,ident):
        with self.store.open() as state:
            row=state.db.execute('SELECT * FROM backend_connectors WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('unknown_connector')
            connection=self.store._connection(state.db,ctx,row['connection'],allow_stale=True)
            return {'connector':ident,'adapter':row['adapter'],'cursor':row['cursor'],'status':row['status'],
                'policy_version':connection['policy_version'],'permission_fresh':connection['permission_observed']+connection['freshness_seconds']>=time.time(),
                'items':state.db.execute('SELECT count(*) FROM backend_connector_items WHERE connector=? AND active=1',(ident,)).fetchone()[0]}
