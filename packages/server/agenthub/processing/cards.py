"""Compact authorized temporal discovery cards."""
def compact_card(reference, *, max_text=650):
    """Keep the useful answer and stable expansion handle; omit ranking diagnostics."""
    if not all(isinstance(reference.get(key),str) and reference[key]
               for key in ('id','revision','source_project','kind','text')):
        raise ValueError('invalid_memory_card_source')
    text=reference['text'].strip()
    if len(text)>max_text:
        boundary=text.rfind('. ',0,max_text)
        text=(text[:boundary+1] if boundary>=max_text//2 else text[:max_text]).rstrip()+'…'
    card={'id':reference['id'],'revision':reference['revision'],
          'source_project':reference['source_project'],'kind':reference['kind'],
          'text':text,'evidence_level':reference.get('evidence_level','model_derived_unverified'),
          'expand':['timeline','detail']}
    for key in ('occurred_date','valid_from','valid_to','time_status','actor'):
        if reference.get(key):card[key]=reference[key]
    return card
