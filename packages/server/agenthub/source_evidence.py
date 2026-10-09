"""Explicit expansion of retained citations; never a raw-source search index."""
import json
from agenthub.processing.episode_curator import _spans, _span_id
from agenthub.processing.media_projection import project_media
from agenthub.processing.segments import segments


def cited_span(source, segment_id):
    """Reproduce current/legacy citation IDs, excluding encoded-media ranges."""
    ranges, _ = project_media(source['body'])
    if isinstance(segment_id,str) and segment_id.startswith('raw:'):
        try:left,right=map(int,segment_id.split(':')[1:])
        except (ValueError,TypeError):return None
        if any(a<=left<right<=z for a,z in ranges):
            return left,right,source['body'][left:right]
        return None
    for start, end in ranges:
        for span in _spans(source['id'], source['body'][start:end]):
            left, right = start + span['start'], start + span['end']
            if _span_id(source['id'], left, right) == segment_id:
                return left, right, span['text']
    # Legacy 16-character citations use a different boundary algorithm.
    if isinstance(segment_id, str) and len(segment_id) == 16:
        for span in segments(source):
            if (span['segment_id'] == segment_id and
                any(a <= span['start'] < span['end'] <= z for a, z in ranges)):
                return span['start'], span['end'], span['quote']
    return None


def document_evidence(store, ctx, document_id, *, revision, offset=0):
    from agenthub.enterprise import Denied
    store._need(ctx, 'read'); store._need(ctx, 'source_read')
    if (not isinstance(document_id,str) or not 1<=len(document_id)<=128 or
        not isinstance(revision,str) or not 1<=len(revision)<=128 or
        type(offset) is not int or not 0<=offset<=10000):
        raise ValueError('invalid_source_evidence_request')
    with store.open() as state:
        db=state.db;doc=store._document_allowed(db,ctx,document_id)
        if not doc:raise Denied()
        if doc['active_revision_id']!=revision:
            return {'id':document_id,'status':'invalidated','current_revision':doc['active_revision_id']}
        if doc.get('representation')=='raw':
            from agenthub.search_permissions import read_source_context, validate_source_snapshots, validate_document_snapshots
            from agenthub.conversation_context import context_page
            ref=db.execute('''SELECT n.*,r.claim_json FROM backend_native_spans n
                JOIN knowledge_revisions r ON r.document_id=n.document_id AND r.revision_id=?
                WHERE n.document_id=?''',(revision,document_id)).fetchone()
            if not ref:raise Denied()
            sources=read_source_context(store,db,ctx,[ref['source_id']])
            if len(sources)!=1:raise Denied()
            source=sources[0]
            source['speaker']=json.loads(ref['claim_json']).get('source_context',{}).get('speaker','unknown')
            if not cited_span(source,f"raw:{ref['start']}:{ref['end']}"):raise Denied()
            adapter=getattr(store,'conversation_segments',None)
            if adapter:adapter.verify_context_sources(db,[source['id']])
            source['text_ranges']=[(ref['start'],ref['end'])]
            result=context_page(document_id,revision,sources,offset=offset)
            validate_source_snapshots(store,db,ctx,{source['id']:source})
            validate_document_snapshots(store,db,ctx,[{'id':document_id,'revision':revision}])
            return result
        # Revision-local support index, not the much larger inherited permission lineage.
        refs=db.execute('''SELECT source_memory_id,source_segment_id FROM knowledge_support
            WHERE revision_id=? AND relation='supports'
            ORDER BY source_memory_id,source_segment_id LIMIT 4 OFFSET ?''',(revision,offset)).fetchall()
        result={'id':document_id,'revision':revision,'sources':[],
            'diagnostic_source_inspection':True,'coverage_gaps':[], 'next_offset':None,
            'notice':'Untrusted cited historical text; source ownership is not speaker attribution. Capture time does not prove current validity.'}
        consumed=0;cache={}
        for ref in refs[:3]:
            source_id=ref['source_memory_id']
            if source_id not in cache:
                policy=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
                if not store._visible_source(db,ctx,policy,raw=True):raise Denied()
                source=db.execute('SELECT id,body,kind FROM memories WHERE id=? AND active=1',(source_id,)).fetchone()
                if not source:raise Denied()
                adapter=getattr(store,'conversation_segments',None)
                if adapter is not None:adapter.verify_source(db,source_id)
                cache[source_id]=(dict(source),policy)
            source,policy=cache[source_id];span=cited_span(source,ref['source_segment_id'])
            if span is None:
                if 'source_span_unavailable' not in result['coverage_gaps']:
                    result['coverage_gaps'].append('source_span_unavailable')
                consumed+=1;continue
            left,right,text=span
            card={'source_id':source_id,'segment_id':ref['source_segment_id'],
                'source_version':policy['source_version'],'owner':policy['owner'],
                'kind':source['kind'],'occurred_at':policy['occurred_at'],
                'precision':policy['occurred_precision'],'start':left,'end':right,'text':text}
            trial=dict(result,sources=result['sources']+[card],next_offset=offset+consumed+1)
            if len(json.dumps(trial,ensure_ascii=True))>4000:
                if result['sources']:break
                # Escaping can make even a 480-character citation exceed the
                # wire limit. Preserve exact prefix offsets and declare the gap.
                card['span_end']=right;card['excerpt_truncated']=True
                result['coverage_gaps'].append('source_excerpt_truncated')
                while card['text'] and len(json.dumps(dict(result,sources=[card],next_offset=offset+consumed+1),ensure_ascii=True))>4000:
                    card['text']=card['text'][:-1];card['end']-=1
                if not card['text']:raise ValueError('source_evidence_response_bound')
            result['sources'].append(card);consumed+=1
        if refs and consumed==0:raise ValueError('source_evidence_response_bound')
        if consumed<len(refs):result['next_offset']=offset+consumed
        if not refs:result['coverage_gaps'].append('no_cited_source_spans')
        current=store._document_allowed(db,ctx,document_id)
        if not current or current['active_revision_id']!=revision:raise Denied()
        for source_id in cache:
            policy=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
            if not store._visible_source(db,ctx,policy,raw=True):raise Denied()
        return result
