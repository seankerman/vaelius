"""Versioned, redacted source intake in the PostgreSQL authority."""

from agenthub.processing.storage import table_exists, begin_write
import base64
import hashlib
import json
import time

from agentclient.enterprise_capture import redact
from agentclient.general_contract import canonical, digest, validate_event, validate_part, EVENT_BYTES


def initialize(db):
    db.require_schema()


class GeneralSourcesMixin:
    def _connection_executor_authorized(self,db,connection):
        rows=db.execute('''SELECT c.*,p.active principal_active FROM enterprise_credentials c
            JOIN enterprise_principals p ON p.tenant=c.tenant AND p.id=c.principal
            WHERE c.tenant=? AND c.enrollment=? AND c.active=1''',(connection['tenant'],connection['enrollment'])).fetchall()
        for credential in rows:
            if not credential['principal_active'] or 'ingest' not in json.loads(credential['actions']):continue
            if (credential['acting_for'] or credential['principal'])!=connection['owner']:continue
            if credential['acting_for']:
                grant=db.execute('SELECT * FROM enterprise_delegations WHERE tenant=? AND principal=? AND acting_for=? AND active=1',
                    (credential['tenant'],credential['principal'],credential['acting_for'])).fetchone()
                if not grant or 'ingest' not in json.loads(grant['actions']) or connection['project'] not in json.loads(grant['projects']):continue
            return True
        return False

    def enroll_connection(self, ctx, ident, namespace, project, source_types, *,
                          visibility='private', reader_ids=None, freshness_seconds=86400,
                          capabilities=None):
        from agenthub.enterprise import Denied, Conflict
        self._need(ctx,'ingest')
        from agentclient.general_contract import text, SOURCE_TYPES
        for value in (ident,namespace,project): text(value)
        if (not source_types or set(source_types)-SOURCE_TYPES or visibility not in {'private','team','organization'}
                or not 1 <= freshness_seconds <= 86400*30): raise ValueError('invalid_connection')
        readers=sorted(set(reader_ids or []))
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db
            if not self._project_member(db,ctx,project): raise Denied()
            for reader in readers:
                if not db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=? AND active=1', (ctx['tenant'],reader)).fetchone():
                    raise ValueError('unknown_connection_reader')
            existing=db.execute('SELECT * FROM backend_connections WHERE id=?',(ident,)).fetchone()
            if existing: raise Conflict()
            db.execute('''INSERT INTO backend_connections
                (id,tenant,enrollment,owner,namespace,project,source_types,visibility,reader_ids,
                 permission_observed,freshness_seconds,capabilities) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ident,ctx['tenant'],ctx['enrollment'],ctx['actor'],namespace,project,
                 json.dumps(sorted(source_types)),visibility,json.dumps(readers),time.time(),freshness_seconds,
                 json.dumps(capabilities or {})))
            self._audit(db,ctx,'connection_enroll','accepted',ident,1)
        return {'connection':ident,'policy_version':1,'visibility':visibility}

    def _connection(self, db, ctx, ident, *, ingest=True, allow_stale=False):
        from agenthub.enterprise import Denied
        row=db.execute('SELECT * FROM backend_connections WHERE id=?',(ident,)).fetchone()
        if (not row or row['tenant']!=ctx['tenant'] or not row['active'] or
                (not allow_stale and row['permission_observed']+row['freshness_seconds']<time.time())): raise Denied()
        if ingest and (row['enrollment']!=ctx['enrollment'] or row['owner']!=ctx['actor'] or
                       not self._project_member(db,ctx,row['project'])): raise Denied()
        return row

    def _general_source_allowed(self, db, ctx, source):
        link=db.execute('SELECT connection FROM backend_source_revisions WHERE source_id=?',(source['id'],)).fetchone()
        if not link: return True
        try: connection=self._connection(db,ctx,link[0],ingest=False)
        except Exception: return False
        readers=json.loads(connection['reader_ids'])
        return not readers or ctx['actor'] in readers or ctx['actor']==connection['owner']

    def connection_policy(self, ctx, ident, *, visibility=None, reader_ids=None, active=True):
        from agenthub.enterprise import Denied
        self._need(ctx,'policy')
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db;row=self._connection(db,ctx,ident,allow_stale=True)
            visibility=visibility or row['visibility'];readers=sorted(set(reader_ids if reader_ids is not None else json.loads(row['reader_ids'])))
            if visibility not in {'private','team','organization'}: raise ValueError('invalid_visibility')
            rank={'private':0,'team':1,'organization':2}
            if rank[visibility]>rank[row['visibility']]: raise Denied()
            prior=json.loads(row['reader_ids'])
            if prior and (not readers or not set(readers)<=set(prior)): raise Denied()
            for reader in readers:
                if not db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=? AND active=1',(ctx['tenant'],reader)).fetchone(): raise Denied()
            changed=visibility!=row['visibility'] or readers!=prior or bool(active)!=bool(row['active'])
            db.execute('''UPDATE backend_connections SET visibility=?,reader_ids=?,active=?,
                policy_version=policy_version+?,permission_observed=? WHERE id=?''',
                (visibility,json.dumps(readers),int(active),int(changed),time.time(),ident))
            if changed:
                from agenthub.enterprise import _scope
                sources=db.execute('SELECT source_id FROM backend_source_revisions WHERE connection=?',(ident,)).fetchall()
                for source in sources: self._invalidate_source(db,source[0], 'connection_policy_changed')
                db.execute('''UPDATE enterprise_sources SET visibility=?,policy_version=policy_version+1
                    WHERE id IN (SELECT source_id FROM backend_source_revisions WHERE connection=?)''',(visibility,ident))
                scope=_scope(ctx['tenant'],row['project'],visibility,ctx['actor'])
                db.execute('UPDATE enterprise_sources SET internal_project=? WHERE id IN (SELECT source_id FROM backend_source_revisions WHERE connection=?)',(scope,ident))
                db.execute('UPDATE memories SET project=? WHERE id IN (SELECT source_id FROM backend_source_revisions WHERE connection=?)',(scope,ident))
                from agenthub.source_index import queue
                for source in sources:queue(db,source[0],restart=True)
            self._audit(db,ctx,'connection_policy','narrowed' if changed else 'refreshed',ident,row['policy_version']+int(changed))
        return {'connection':ident,'policy_version':row['policy_version']+int(changed),'invalidated':changed}

    def _invalidate_source(self, db, source_id, reason):
        # Existing lifecycle already blocks revisions/temporal and journals denial.
        dependencies=[r[0] for r in db.execute('SELECT document_id FROM enterprise_dependencies WHERE source_id=?',(source_id,))]
        for doc in dependencies:
            db.execute('UPDATE enterprise_documents SET active=0,blocked_reason=? WHERE id=?',(reason,doc))
            db.execute('DELETE FROM knowledge_fts WHERE document_id=?',(doc,))
            db.execute('DELETE FROM knowledge_embeddings WHERE document_id=?',(doc,))
        self._invalidate_processing_source(db,source_id,reason)
        # Native correction/withdrawal should not leave old evidence searchable.

    def ingest_part(self, ctx, value):
        from agenthub.enterprise import Conflict
        self._need(ctx,'ingest');validate_part(value)
        key=(value['connection'],value['external_id'],value['revision'])
        decoded=base64.b64decode(value['data'])
        with self.open() as state,state.db:
            db=state.db;self._connection(db,ctx,value['connection'])
            begin_write(db)
            receipt=db.execute('SELECT * FROM backend_ingest_receipts WHERE connection=? AND external_id=? AND revision=?',key).fetchone()
            if receipt:
                if receipt['digest']!=value['digest']: raise Conflict()
                known=db.execute('SELECT sha256 FROM backend_part_hashes WHERE connection=? AND external_id=? AND revision=? AND part_index=?',(*key,value['part_index'])).fetchone()
                if not known or known[0]!=hashlib.sha256(decoded).hexdigest() or receipt['part_count']!=value['part_count']:raise Conflict()
                segments=getattr(self,'conversation_segments',None)
                if segments is not None:segments.verify_source(db,receipt['source_id'])
                return {'disposition':'duplicate','digest':receipt['digest'],'received_parts':receipt['part_count'],'complete':True,'source_id':receipt['source_id']}
            prior=db.execute('SELECT * FROM backend_parts WHERE connection=? AND external_id=? AND revision=? AND part_index=?',(*key,value['part_index'])).fetchone()
            if prior and (prior['digest']!=value['digest'] or prior['part_count']!=value['part_count'] or prior['data']!=decoded): raise Conflict()
            peers=db.execute('SELECT min(digest) digest,min(part_count) part_count,coalesce(sum(length(data)),0) size FROM backend_parts WHERE connection=? AND external_id=? AND revision=?',key).fetchone()
            if peers and peers['digest'] and (peers['digest']!=value['digest'] or peers['part_count']!=value['part_count']): raise Conflict()
            if (peers['size'] or 0)+(0 if prior else len(decoded))>EVENT_BYTES: raise ValueError('reassembly_too_large')
            total=db.execute('SELECT coalesce(sum(length(data)),0) FROM backend_parts WHERE connection=?',(value['connection'],)).fetchone()[0]
            if total+len(decoded)>64*1024*1024: raise ValueError('connection_parts_backpressure')
            db.execute("INSERT INTO backend_parts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",(*key,value['digest'],value['part_index'],value['part_count'],decoded,time.time()))
            rows=db.execute('SELECT * FROM backend_parts WHERE connection=? AND external_id=? AND revision=? ORDER BY part_index',key).fetchall()
            if len(rows)!=value['part_count']:
                return {'disposition':'incomplete','digest':value['digest'],'received_parts':len(rows),'complete':False,'source_id':''}
            raw=b''.join(r['data'] for r in rows)
            if hashlib.sha256(raw).hexdigest()!=value['digest']: raise Conflict()
        try:
            payload=json.loads(raw);validate_event(payload)
            if (payload['connection'],payload['external_id'],payload['revision'])!=key: raise Conflict()
            safe,_=redact(payload)
            if safe!=payload:raise ValueError('unredacted_enterprise_source')
        except ValueError:
            # Incomplete fragments are transport staging, never source/model
            # input. Drop rejected reassemblies, including earlier fragments.
            with self.open() as state,state.db:
                self._connection(state.db,ctx,value['connection'])
                state.db.execute('DELETE FROM backend_parts WHERE connection=? AND external_id=? AND revision=?',key)
            raise ValueError('invalid_or_unredacted_enterprise_source') from None
        result=self.ingest_general(ctx,payload)
        with self.open() as state,state.db:
            state.db.executemany("INSERT INTO backend_part_hashes VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                ((*key,r['part_index'],hashlib.sha256(r['data']).hexdigest()) for r in rows))
            state.db.execute("INSERT INTO backend_ingest_receipts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",(*key,value['digest'],result['disposition'],result['source_id'],value['part_count'],time.time()))
            state.db.execute('DELETE FROM backend_parts WHERE connection=? AND external_id=? AND revision=?',key)
        return {'disposition':result['disposition'],'digest':value['digest'],'received_parts':value['part_count'],'complete':True,'source_id':result['source_id']}

    def ingest_general(self, ctx, value):
        from agenthub.enterprise import Conflict, Denied, _scope, _digest
        self._need(ctx,'ingest');validate_event(value)
        safe,_=redact(value)
        if safe!=value: raise ValueError('unredacted_enterprise_source')
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db;connection=self._connection(db,ctx,value['connection'])
            if value['project']!=connection['project'] or value['source_type'] not in json.loads(connection['source_types']): raise Denied()
            key=(value['connection'],value['external_id'],value['revision']);hashed=digest(value)
            prior=db.execute('SELECT * FROM backend_source_revisions WHERE connection=? AND external_id=? AND revision=?',key).fetchone()
            if prior:
                if prior['digest']!=hashed: raise Conflict()
                segments=getattr(self,'conversation_segments',None)
                if segments is not None:segments.verify_source(db,prior['source_id'])
                return {'source_id':prior['source_id'],'disposition':'duplicate'}
            head=db.execute('SELECT * FROM backend_source_heads WHERE connection=? AND external_id=?',key[:2]).fetchone()
            if head and int(value['revision'])<=int(head['revision']): raise Conflict()
            source_id=_digest(json.dumps([ctx['tenant'],connection['namespace'],*key]))
            project=_scope(ctx['tenant'],value['project'],connection['visibility'],ctx['actor'])
            session=_digest(json.dumps([ctx['tenant'],value['connection'],value['conversation']]))
            turn=_digest(json.dumps([session,value['turn']])) if value['turn'] else ''
            blocks=value['blocks'];event=value['event'];now=time.time()
            body='\n'.join(b['value'] if isinstance(b['value'],str) else json.dumps(b['value'],ensure_ascii=False,sort_keys=True) for b in blocks)
            kind=('UserPromptSubmit' if event['role']=='user' else 'Stop' if event['complete']
                  else 'AssistantMessage' if event['role']=='assistant' else 'PostToolUse')
            if event['kind']=='PostCompact':kind='SourceBoundary'
            if value['disposition'] in {'excluded','unsupported','incomplete'}: kind='gap'
            role='context_transfer' if value['origin']=='context_transfer' else 'episode_evidence'
            # Exact imported content is a copy with original lineage, never a
            # second independent observation. Match only currently readable
            # native originals in this tenant; do not leak denied source IDs.
            copies=[]
            def strings(item):
                if isinstance(item,str): yield item
                elif isinstance(item,dict):
                    for child in item.values(): yield from strings(child)
                elif isinstance(item,list):
                    for child in item: yield from strings(child)
            quoted={s for s in strings(blocks) if len(s)>=40}
            if value['source_type']=='agent' and quoted:
                for native in db.execute('''SELECT DISTINCT s.*,m.body FROM backend_native_artifacts n
                    JOIN enterprise_sources s ON s.id=n.source_id JOIN memories m ON m.id=s.id
                    WHERE s.tenant=? AND s.active=1
                    AND n.artifact_kind IN ('native_document','native_record')''',(ctx['tenant'],)):
                    if native['body'] in quoted and self._visible_source(db,ctx,native): copies.append(native['id'])
                if copies: role='context_transfer'
            db.execute('INSERT INTO backend_source_revisions VALUES(?,?,?,?,?,?,?,?,?)',(source_id,*key,hashed,canonical(value).decode(),now,value['disposition'],connection['policy_version']))
            db.execute('''INSERT INTO enterprise_sources
                (id,tenant,owner,external_project,internal_project,external_id,enrollment,payload_hash,
                 visibility,raw_visibility,occurred_at,created,occurred_precision,occurred_timezone)
                VALUES(?,?,?,?,?,?,?,?,?,'private',?,?,?,?)''',
                (source_id,ctx['tenant'],ctx['actor'],value['project'],project,
                 value['connection']+':'+value['external_id']+':'+value['revision'],ctx['enrollment'],hashed,
                 connection['visibility'],str(value['occurred_at']),now,
                 'instant' if value['occurred_at']!='unknown' else 'unknown',None))
            segments=getattr(self,'conversation_segments',None)
            if segments is not None:segments.retain(db,source_id,value)
            db.execute('INSERT INTO memories(id,session,project,body,kind,created,exit_code,turn) VALUES(?,?,?,?,?,?,?,?)',
                (source_id,session,project,body,kind,now,event['exit_code'],turn))
            db.execute('''INSERT INTO source_event_metadata(source_id,tool_name,source_role,capture_id,event_fields,response_shape,created)
                VALUES(?,?,?,?,?,?,?)''',(source_id,event['tool_name'],role,value['external_id'],json.dumps(sorted(event)),json.dumps({'typed':True}),now))
            db.executemany('INSERT INTO backend_source_links VALUES(?,?,?)',((source_id,ident,'copied_native_context') for ident in copies))
            if head:
                self._invalidate_source(db,head['source_id'],'source_revision_replaced')
                db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?',(head['source_id'],))
                db.execute('UPDATE memories SET active=0 WHERE id=?',(head['source_id'],))
            db.execute('''INSERT INTO backend_source_heads VALUES(?,?,?,?) ON CONFLICT(connection,external_id)
                DO UPDATE SET revision=excluded.revision,source_id=excluded.source_id''',(*key,source_id))
            if kind=='gap': db.execute('INSERT INTO backend_capture_gaps VALUES(?,?,?,?)',(source_id,value['connection'],value['disposition'],now))
            late=db.execute("SELECT id FROM curation_episode_jobs WHERE project=? AND session=? AND turn=? AND status IN ('done','no_learning')",
                (project,session,turn)).fetchall()
            for job in late:
                db.execute("UPDATE curation_episode_jobs SET status='pending',progress='{}',next_attempt=0,error='late_source_revision' WHERE id=?",(job[0],))
                for derived in db.execute('SELECT document_id FROM episode_candidates WHERE job_id=? AND document_id IS NOT NULL',(job[0],)):
                    db.execute("UPDATE enterprise_documents SET active=0,blocked_reason='late_source_revision' WHERE id=?",(derived[0],))
                    db.execute('DELETE FROM knowledge_fts WHERE document_id=?',(derived[0],))
                if table_exists(db,'backend_observers'):
                    db.execute("UPDATE backend_observers SET provider_session=NULL,pending_session=NULL,observer_epoch=observer_epoch+1,status='reconstruct' WHERE connection=? AND conversation=?",
                        (value['connection'],value['conversation']))
            if value['event']['kind']=='PostCompact' and table_exists(db,'backend_observers'):
                db.execute('UPDATE backend_observers SET source_epoch=source_epoch+1 WHERE connection=? AND conversation=?',
                    (value['connection'],value['conversation']))
            self._audit(db,ctx,'ingest_v2',value['disposition'],source_id,connection['policy_version'])
            if value['source_type'] in {'agent','conversation'} and value['disposition'] in {'accepted','redacted'}:
                from agenthub.source_index import queue
                queue(db,source_id)
            return {'source_id':source_id,'disposition':value['disposition']}

    def general_source(self, ctx, source_id):
        from agenthub.enterprise import Denied
        self._need(ctx,'source_read')
        segments=getattr(self,'conversation_segments',None)
        if segments is not None:
            with self.open() as state:
                row=state.db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=?',(source_id,)).fetchone()
                if row and json.loads(row['payload']).get('source_type') in {'agent','conversation'}:
                    return segments.fetch(ctx,source_id)
        with self.open() as state:
            source=state.db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
            if not self._visible_source(state.db,ctx,source,raw=True): raise Denied()
            row=state.db.execute('SELECT revision,payload FROM backend_source_revisions WHERE source_id=?',(source_id,)).fetchone()
            if not row: raise Denied()
            return {'source_id':source_id,'revision':row['revision'],'payload':json.loads(row['payload'])}
