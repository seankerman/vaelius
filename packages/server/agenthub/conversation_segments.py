"""Verified individual bounded conversation events in the backend object adapter.

PostgreSQL remains the metadata/knowledge authority. An immutable canonical typed
event/version is one simple segment; completed-turn observer grouping spans several
such segments. Canonical payloads are already deterministically redacted. This is
not raw transcript indexing, archive packing or a second client knowledge corpus.
"""
from __future__ import annotations

import hashlib
import io
import json
import time

from agentclient.general_contract import EVENT_BYTES,canonical,validate_event
from agenthub.enterprise import Denied
from agenthub.source_objects import ObjectCorrupt, ObjectMissing, bounded_spool, object_key, verify

REPRESENTATION='canonical_redacted_event_v1'


class ConversationSegments:
    def __init__(self,store,objects):
        self.store=store;self.objects=objects

    def _reservation(self,source_id,value,raw):
        """Commit the upload reservation outside the eventual source transaction.

        No foreign key to an uncommitted source is needed here. If object upload
        succeeds but canonical commit fails, an explicit orphan remains resumable.
        The stable identity/content key is reused by an identical transport retry.
        """
        sha=hashlib.sha256(raw).hexdigest()
        key=object_key(self.store.tenant_id,source_id,value['revision'],sha)
        ident=hashlib.sha256(canonical([self.store.tenant_id,value['connection'],
            value['external_id'],value['revision']])).hexdigest()
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO cloud_segment_uploads
                (id,tenant,connection,external_id,revision,source_id,object_key,sha256,byte_length,status,updated)
                VALUES(?,?,?,?,?,?,?,?,?,'uploading',?) ON CONFLICT DO NOTHING''',
                (ident,self.store.tenant_id,value['connection'],value['external_id'],value['revision'],
                 source_id,key,sha,len(raw),time.time()))
            row=dict(state.db.execute('SELECT * FROM cloud_segment_uploads WHERE id=? FOR UPDATE',(ident,)).fetchone())
            if (row['source_id'],row['object_key'],row['sha256'],row['byte_length'])!=(source_id,key,sha,len(raw)):
                raise ObjectCorrupt('segment_upload_identity_conflict')
            if row['status']=='removed':
                state.db.execute("UPDATE cloud_segment_uploads SET status='uploading',updated=? WHERE id=?",(time.time(),ident))
                row['status']='uploading'
        return row

    def retain(self,db,source_id,value,*,repair_missing=False):
        from agentclient.enterprise_capture import redact
        validate_event(value)
        if value['source_type'] not in {'agent','conversation'}:return None
        if redact(value)[0]!=value:raise ValueError('unredacted_segment_source')
        source=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
        if not source or source['tenant']!=self.store.tenant_id or not source['active']:raise Denied()
        raw=canonical(value)
        sha=hashlib.sha256(raw).hexdigest()
        revision=db.execute('SELECT digest FROM backend_source_revisions WHERE source_id=?',(source_id,)).fetchone()
        if not revision or revision['digest']!=sha or source['payload_hash']!=sha:
            raise ObjectCorrupt('segment_projection_checksum_mismatch')
        row=self._reservation(source_id,value,raw)
        try:
            # Accepted sources never silently repair a missing retained original.
            # Uncommitted uploads can resume the exact caller-provided bytes.
            if row['status'] in {'uploaded','accepted'}:
                try:verify(self.objects.head(row['object_key']),len(raw),row['sha256'])
                except ObjectMissing:
                    if row['status']=='accepted' and not repair_missing:raise
                    self.objects.put(row['object_key'],io.BytesIO(raw),expected_sha256=row['sha256'])
                    verify(self.objects.head(row['object_key']),len(raw),row['sha256'])
            else:
                self.objects.put(row['object_key'],io.BytesIO(raw),expected_sha256=row['sha256'])
                verify(self.objects.head(row['object_key']),len(raw),row['sha256'])
            with self.store.open() as uploaded,uploaded.db:
                uploaded.db.execute("UPDATE cloud_segment_uploads SET status='uploaded',updated=? WHERE id=? AND status!='accepted'",
                    (time.time(),row['id']))
        except Exception:
            with self.store.open() as failed,failed.db:
                failed.db.execute("UPDATE cloud_segment_uploads SET status='failed',updated=? WHERE id=? AND status!='accepted'",
                    (time.time(),row['id']))
            raise
        db.execute('''INSERT INTO cloud_conversation_segments
            (source_id,tenant,connection,external_id,revision,object_key,sha256,byte_length,representation,status,created)
            VALUES(?,?,?,?,?,?,?,?,?,'active',?) ON CONFLICT DO NOTHING''',
            (source_id,self.store.tenant_id,value['connection'],value['external_id'],value['revision'],
             row['object_key'],row['sha256'],len(raw),REPRESENTATION,time.time()))
        self.verify_source(db,source_id)
        # Accepted retained input counters commit with the canonical source and
        # locator. Upload failure/transaction abort must not meter acceptance.
        # These are logical redacted bytes, never multipart/base64 wire overhead.
        details={'stage':'capture','status':'accepted'}
        for kind,amount in (('input_bytes',len(raw)),('source_revisions',1)):
            metric_id='segment:'+source_id+':'+kind
            db.execute('''INSERT INTO cloud_metrics(id,tenant,kind,amount,created,details)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
                (metric_id,self.store.tenant_id,kind,amount,time.time(),json.dumps(details,sort_keys=True)))
            metric=db.execute('SELECT tenant,kind,amount,details FROM cloud_metrics WHERE id=?',(metric_id,)).fetchone()
            if (metric['tenant'],metric['kind'],metric['amount'],json.loads(metric['details']))!=(
                    self.store.tenant_id,kind,amount,details):
                raise ObjectCorrupt('segment_metric_identity_conflict')
        # This final state changes atomically with canonical source acceptance.
        db.execute("UPDATE cloud_segment_uploads SET status='accepted',updated=? WHERE id=?",(time.time(),row['id']))
        return {'source_id':source_id,'sha256':row['sha256'],'bytes':len(raw),'representation':REPRESENTATION}

    def _read(self,row):
        if row['tenant']!=self.store.tenant_id or row['status']!='active':raise Denied()
        with self.objects.open(row['object_key']) as source,bounded_spool(source,limit=EVENT_BYTES,
                expected_sha256=row['sha256']) as (spool,length,sha):
            if length!=row['byte_length']:raise ObjectCorrupt('segment_length_mismatch')
            raw=spool.read(EVENT_BYTES+1)
        value=json.loads(raw);validate_event(value)
        if canonical(value)!=raw or value['source_type'] not in {'agent','conversation'}:
            raise ObjectCorrupt('segment_representation_mismatch')
        if (value['connection'],value['external_id'],value['revision'])!=(row['connection'],row['external_id'],row['revision']):
            raise ObjectCorrupt('segment_identity_mismatch')
        return value

    def verify_source(self,db,source_id):
        revision=db.execute('SELECT * FROM backend_source_revisions WHERE source_id=?',(source_id,)).fetchone()
        if not revision:return None # Declared v1/native adapters retain their own representation.
        cached=json.loads(revision['payload'])
        if cached.get('source_type') not in {'agent','conversation'}:return None
        row=db.execute('SELECT * FROM cloud_conversation_segments WHERE source_id=?',(source_id,)).fetchone()
        if not row:raise ValueError('segment_original_required')
        value=self._read(row)
        if value!=cached:raise ObjectCorrupt('segment_canonical_projection_mismatch')
        return value

    def verify_sources(self,db,source_ids):
        # The caller owns current policy/processing identity checks. Never scan
        # the corpus to make an undeclared background backfill happen here.
        if not isinstance(source_ids,(list,tuple,set)) or len(source_ids)>10000:
            raise ValueError('explicit_segment_verification_bound')
        for source_id in dict.fromkeys(source_ids):self.verify_source(db,source_id)

    def verify_context_sources(self,db,source_ids):
        """Verify bounded retained originals without one SQL query per message."""
        if len(source_ids)>8:raise ValueError('context_verification_bound')
        rows=db.execute('''SELECT r.source_id,r.payload,s.* FROM backend_source_revisions r
            LEFT JOIN cloud_conversation_segments s ON s.source_id=r.source_id
            WHERE r.source_id=ANY(?::text[])''',(list(source_ids),)).fetchall()
        for row in rows:
            cached=json.loads(row['payload'])
            if cached.get('source_type') not in {'agent','conversation'}:continue
            if not row['object_key']:raise ValueError('segment_original_required')
            if self._read(row)!=cached:raise ObjectCorrupt('segment_canonical_projection_mismatch')

    def fetch(self,ctx,source_id):
        self.store._need(ctx,'source_read')
        with self.store.delivery_lock(),self.store.open() as state:
            source=state.db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
            if not self.store._visible_source(state.db,ctx,source,raw=True):raise Denied()
            value=self.verify_source(state.db,source_id)
            if value is None:raise Denied()
            # The lock serializes ordinary lifecycle changes. Recheck after read
            # for long adapter I/O and current global registry/identity changes.
            self.store._need(ctx,'source_read')
            source=state.db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
            if not self.store._visible_source(state.db,ctx,source,raw=True):raise Denied()
            return {'source_id':source_id,'revision':value['revision'],'payload':value}

    def retain_existing(self,ctx,source_ids,*,repair_missing=False):
        """Explicit bounded owner-authorized repair of pre-object typed sources."""
        self.store._need(ctx,'ingest');self.store._need(ctx,'source_read')
        if type(repair_missing) is not bool:raise ValueError('explicit_segment_repair_boolean_required')
        if (not isinstance(source_ids,list) or not 1<=len(source_ids)<=100 or
                len(set(source_ids))!=len(source_ids) or any(not isinstance(x,str) or not x for x in source_ids)):
            raise ValueError('explicit_bounded_segment_sources_required')
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            selected=[]
            for source_id in source_ids:
                source=state.db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
                if (not self.store._visible_source(state.db,ctx,source,raw=True) or source['owner']!=ctx['actor']):raise Denied()
                row=state.db.execute('SELECT * FROM backend_source_revisions WHERE source_id=?',(source_id,)).fetchone()
                if not row:raise ValueError('typed_conversation_source_required')
                value=json.loads(row['payload']);validate_event(value)
                if value['source_type'] not in {'agent','conversation'}:raise ValueError('typed_conversation_source_required')
                self.store._connection(state.db,ctx,value['connection'])
                selected.append((source_id,value))
            for source_id,value in selected:self.retain(state.db,source_id,value,repair_missing=repair_missing)
        return {'retained':len(selected),'provider_calls':0,'source_ids':source_ids}

    def purge(self,db,source_id):
        row=db.execute('SELECT * FROM cloud_conversation_segments WHERE source_id=? FOR UPDATE',(source_id,)).fetchone()
        if not row:return
        if row['tenant']!=self.store.tenant_id:raise Denied()
        self.objects.delete(row['object_key'])
        db.execute("UPDATE cloud_conversation_segments SET status='removed' WHERE source_id=?",(source_id,))
        db.execute("UPDATE cloud_segment_uploads SET status='removed',updated=? WHERE object_key=?",(time.time(),row['object_key']))

    def cleanup(self,upload_id,*,grace_seconds=3600):
        if type(grace_seconds) not in (int,float) or not 1<=grace_seconds<=604800:
            raise ValueError('segment_cleanup_grace_required')
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            row=state.db.execute('SELECT * FROM cloud_segment_uploads WHERE id=? FOR UPDATE',(upload_id,)).fetchone()
            if not row or row['tenant']!=self.store.tenant_id:raise Denied()
            reference=state.db.execute('SELECT 1 FROM cloud_conversation_segments WHERE object_key=?',(row['object_key'],)).fetchone()
            if reference or row['status']=='accepted':raise ValueError('segment_upload_referenced')
            if row['updated']+grace_seconds>time.time():raise ValueError('segment_upload_recent')
            if row['status'] not in {'uploaded','failed','removed'}:raise ValueError('segment_upload_in_flight')
            self.objects.delete(row['object_key'])
            state.db.execute("UPDATE cloud_segment_uploads SET status='removed',updated=? WHERE id=?",(time.time(),upload_id))
            return {'id':upload_id,'status':'removed','provider_calls':0}


def main(argv=None):
    """Explicit local operator repair; never discover sources or run a model."""
    import argparse
    from pathlib import Path
    from agenthub.cloud_maintenance import private_input, _operator
    from agenthub.cloud_local import runtime, receipt
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['retain-existing','cleanup'])
    parser.add_argument('--profile',required=True);parser.add_argument('--tenant',required=True)
    parser.add_argument('--input',required=True);parser.add_argument('--token-file')
    args=parser.parse_args(argv)
    profile,_,_=_operator(args.profile,args.tenant)
    value=private_input(args.input);_,registry=runtime(profile);store=registry.resolve(args.tenant)
    segments=getattr(store,'conversation_segments',None)
    if segments is None:raise ValueError('conversation_object_adapter_required')
    if args.command=='retain-existing':
        if set(value)-{'source_ids','repair_missing'}:raise ValueError('invalid_segment_repair_fields')
        if not args.token_file:parser.error('--token-file current owner credential required')
        token=Path(args.token_file).expanduser().resolve()
        if not token.is_file() or token.stat().st_mode&0o077:raise ValueError('owner_token_permissions')
        ctx=store.authenticate(token.read_text().strip())
        result=segments.retain_existing(ctx,value['source_ids'],repair_missing=value.get('repair_missing',False))
        result={key:result[key] for key in ('retained','provider_calls')}
    else:
        if set(value)-{'upload_id','grace_seconds'}:raise ValueError('invalid_segment_cleanup_fields')
        result=segments.cleanup(value['upload_id'],grace_seconds=value.get('grace_seconds',3600))
    print(json.dumps(receipt(profile,'conversation-segment-'+args.command,result),sort_keys=True))


if __name__=='__main__':main()
