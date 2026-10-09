"""Immutable source spans. Models select IDs; code supplies verbatim evidence."""
import hashlib
import re


def segments(source):
    text=source['body'];result=[]
    # Preserve exact whitespace and offsets; split long runs at a nearby boundary.
    start=0
    while start<len(text):
        end=min(len(text),start+480)
        if end<len(text):
            breaks=[m.end() for m in re.finditer(r'\n|(?<=[.!?])\s+|\s+',text[start:end])]
            if breaks and breaks[-1]>=120:end=start+breaks[-1]
        quote=text[start:end]
        if quote.strip():
            ident=hashlib.sha256((source['id']+':'+str(start)+':'+str(end)).encode()).hexdigest()[:16]
            result.append({'segment_id':ident,'source_id':source['id'],'start':start,'end':end,'quote':quote})
        start=end
    return result
