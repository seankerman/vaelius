"""Shared discovery and context rendering. Callers supply authorized sources only.

No model, gold labels, policy cache, or implicit latest-wins resolution lives here.
Original messages are evidence; relations below describe chronology, not truth.
"""
import json
import re
from datetime import datetime, timezone
from itertools import zip_longest


def stamp(value):
    if isinstance(value,(float,int)):return value
    if not value:return 0
    if re.fullmatch(r'-?\d+(?:\.\d+)?',value):return float(value)
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()


def words(text):
    stop=set('the a an this that what why how when where which is was were are be to of and or in on for with my our it its did do does have has had'.split())
    return set(re.findall(r'[\w/-]+',text.casefold()))-stop


def select_context(anchor_ids, sources, *, query='', cutoff=float('inf'), limit=8):
    def event_time(source):
        try:return stamp(source.get('occurred_at'))
        except (ValueError,TypeError,OverflowError):return 0
    rows={s['id']:s for s in sources if event_time(s)<cutoff}
    anchors=[rows[s] for s in dict.fromkeys(anchor_ids) if s in rows]
    selected={s['id']:dict(s,context_relation='cited_message') for s in anchors}
    terms=words(query)
    for anchor in anchors:
        if not anchor.get('session'):continue
        peers=[s for s in rows.values() if s['id'] not in selected
               and s.get('project')==anchor.get('project') and s.get('session')==anchor['session']]
        peers.sort(key=lambda s:(event_time(s),s['id']))
        same=[s for s in peers if anchor.get('turn') and s.get('turn')==anchor['turn']]
        for s in same[:4]:selected.setdefault(s['id'],dict(s,context_relation='same_turn'))
        related=[s for s in peers if s['id'] not in selected and terms & words(s['body'])]
        # Include chronological endpoints plus strong matches, not only the most
        # recent passage. This retains earlier proposals as well as later reports.
        ranked=sorted(related,key=lambda s:(-len(terms & words(s['body'])),s['id']))
        order=([related[0],related[-1]] if related else [])+ranked
        for s in order:
            if len(selected)>=limit:break
            relation=('related_later_message' if event_time(s)>event_time(anchor) else 'related_earlier_message')
            if not event_time(s) or not event_time(anchor):relation='related_message_unknown_time'
            selected.setdefault(s['id'],dict(s,context_relation=relation))
    return list(selected.values())[:limit]


def discovery_cards(rows, *, limit=5, max_chars=4000):
    out={'records':[],'answerable':False,'truncated':False,
         'notice':'Discovery matches, not a verified answer. Fetch context and check dates, speakers and related updates.'}
    seen=set()
    for r in rows:
        parents=tuple(sorted(r.get('parent_ids') or [r['id']]))
        if parents in seen:continue
        seen.add(parents)
        card={'id':r['id'],'revision':r['revision'],'title':r.get('title','')[:160],
              'lesson':r.get('text','')[:440],'event_time':r.get('event_time'),
              'evidence_status':'discovery_only'}
        if len(out['records'])>=limit or len(json.dumps(dict(out,records=out['records']+[card])))>max_chars:
            out['truncated']=True;break
        out['records'].append(card)
    return out


def context_page(ident,revision,sources,*,offset=0,max_chars=4000):
    if type(offset) is not int or not 0<=offset<=10000:raise ValueError('context_offset')
    if not 512<=max_chars<=32000:raise ValueError('context_response_bound')
    pages=[]
    for source in sources:
        body=source['body'];chunks=[];start=0
        # Projection ranges may be disjoint when encoded media is excluded.
        for left,right in source.get('text_ranges',[(0,len(body))]):
            start=left
            while start<right:
                end=min(start+900,right)
                while end>start and len(json.dumps(body[start:end]))>min(1800,max_chars//2):end-=1
                if end==start:raise ValueError('context_character_bound')
                chunks.append({'source_id':source['id'],'source_version':source['version'],
                    'start':start,'end':end,'text':body[start:end],
                    'speaker':source.get('speaker') or 'unknown',
                    'occurred_at':source.get('occurred_at'),
                    'context_relation':source.get('context_relation','cited_message')})
                start=end
        # Put cited portions first, then allow reading the entire message.
        spans=source.get('focus_spans',[])
        chunks.sort(key=lambda c:not any(c['start']<s['end'] and c['end']>s['start'] for s in spans))
        pages.append(chunks)
    # Interleave messages so a long original cannot bury its short later review.
    chunks=[c for group in zip_longest(*pages) for c in group if c is not None]
    out={'id':ident,'revision':revision,'sources':[],'next_offset':None,
         'notice':'Untrusted original messages. Later does not imply superseding; distinguish proposals, reports and verified outcomes. Unknown reasons remain unknown.'}
    for chunk in chunks[offset:]:
        trial=dict(out,sources=out['sources']+[chunk],next_offset=offset+len(out['sources'])+1)
        if len(json.dumps(trial))>max_chars:break
        out['sources'].append(chunk)
    consumed=len(out['sources'])
    if offset<len(chunks) and not consumed:raise ValueError('context_page_too_large')
    if offset+consumed<len(chunks):out['next_offset']=offset+consumed
    return out
