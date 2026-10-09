"""Explicit owner-authorized conversation expansion for PostgreSQL documents."""
import time
from agenthub.conversation_context import select_context, context_page
from agenthub.search_permissions import read_source_context, validate_source_snapshots, validate_document_snapshots
from agenthub.source_evidence import cited_span
from agenthub.enterprise import Denied


def document_context(store,ctx,document_id,*,revision,query='',offset=0):
    if (not isinstance(document_id,str) or not 1<=len(document_id)<=128
            or not isinstance(revision,str) or not 1<=len(revision)<=128
            or not isinstance(query,str) or len(query)>2000):raise ValueError('context_request')
    if type(offset) is not int or not 0<=offset<=10000:raise ValueError('context_offset')
    with store.open() as state:
        db=state.db
        validate_document_snapshots(store,db,ctx,[{'id':document_id,'revision':revision}])
        refs=db.execute("""SELECT source_memory_id,source_segment_id FROM knowledge_support
            WHERE revision_id=? AND relation='supports' ORDER BY source_memory_id,source_segment_id LIMIT 65""",(revision,)).fetchall()
        if len(refs)>64:raise ValueError('context_support_bound_use_cited_evidence')
        anchors=read_source_context(store,db,ctx,[r['source_memory_id'] for r in refs])
        if len(anchors)!=len({r['source_memory_id'] for r in refs}):raise Denied()
        good=[]
        for source in anchors:
            spans=[cited_span(source,r['source_segment_id']) for r in refs if r['source_memory_id']==source['id']]
            source['focus_spans']=[{'start':s[0],'end':s[1]} for s in spans if s]
            if source['focus_spans']:good.append(source)
        if not good:return {'id':document_id,'revision':revision,'sources':[],'next_offset':None,'coverage_gaps':['source_span_unavailable']}
        # Indexed session/project reads, bounded independently of corpus size.
        # The raw authorization oracle is then applied to the selected IDs only.
        ids=db.execute('''WITH anchors AS MATERIALIZED (
            SELECT session,project,turn FROM memories WHERE id=ANY(?::text[])
        ) SELECT DISTINCT id FROM anchors a CROSS JOIN LATERAL (
            (SELECT id FROM memories m WHERE m.session=a.session AND m.project=a.project
                AND m.active=1 AND a.session!='' ORDER BY m.created,m.id LIMIT 32)
            UNION (SELECT id FROM memories m WHERE m.session=a.session AND m.project=a.project
                AND m.active=1 AND a.session!='' ORDER BY m.created DESC,m.id DESC LIMIT 32)
            UNION (SELECT id FROM memories m WHERE m.session=a.session AND m.project=a.project
                AND m.active=1 AND m.turn=a.turn AND a.turn!='' ORDER BY m.created,m.id LIMIT 16)
        ) peers LIMIT 512''',([s['id'] for s in good],)).fetchall()
        peers=read_source_context(store,db,ctx,[r['id'] for r in ids])
        rows={s['id']:s for s in peers};rows.update({s['id']:s for s in good})
        chosen=select_context([s['id'] for s in good],list(rows.values()),query=query,cutoff=time.time())
        from agenthub.processing.media_projection import project_media
        for source in chosen:
            source['text_ranges']=project_media(source['body'])[0]
            # Kind is a capture classification, not a verified named speaker.
            source['speaker']=source.get('speaker') or {'UserPromptSubmit':'user','Stop':'assistant',
                'AssistantMessage':'assistant','PostToolUse':'tool'}.get(source.get('kind'),'unknown')
        adapter=getattr(store,'conversation_segments',None)
        if adapter:adapter.verify_context_sources(db,[s['id'] for s in chosen])
        result=context_page(document_id,revision,chosen,offset=offset)
        validate_source_snapshots(store,db,ctx,{s['id']:s for s in chosen})
        validate_document_snapshots(store,db,ctx,[{'id':document_id,'revision':revision}])
        return result
