"""Revision-specific agent self-reports; never an assertion of verified success."""
import hashlib
import json
import re
import time
from agenthub.enterprise import Denied, Conflict


def record_feedback(store,ctx,value):
    fields={'id','revision','request_key','outcome'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('feedback_fields')
    for key in ('id','revision','request_key'):
        if not isinstance(value[key],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value[key]):raise ValueError('feedback_identifier')
    if value['outcome'] not in {'helpful','unhelpful','incorrect','not_used'}:raise ValueError('feedback_outcome')
    store._need(ctx,'read')
    with store.delivery_lock(),store.open() as state,state.db:
        db=state.db
        # Canonical indexed permission checks also revalidate the current identity.
        doc=store._document_allowed(db,ctx,value['id'])
        if not doc:raise Denied()
        if doc['active_revision_id']!=value['revision']:raise Conflict()
        ident=hashlib.sha256(json.dumps([ctx['tenant'],ctx['principal'],ctx['enrollment'],value['request_key']]).encode()).hexdigest()
        db.execute('''INSERT INTO memory_feedback_reports
            (id,tenant,principal,actor,enrollment,document_id,revision_id,request_key,outcome,created)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING''',
            (ident,ctx['tenant'],ctx['principal'],ctx['actor'],ctx['enrollment'],value['id'],value['revision'],value['request_key'],value['outcome'],time.time()))
        row=db.execute('SELECT document_id,revision_id,outcome,actor FROM memory_feedback_reports WHERE id=?',(ident,)).fetchone()
        if (row['document_id'],row['revision_id'],row['outcome'],row['actor'])!=(value['id'],value['revision'],value['outcome'],ctx['actor']):raise Conflict()
        return {'status':'recorded','id':ident,'classification':'agent_self_report','independently_verified':False}
