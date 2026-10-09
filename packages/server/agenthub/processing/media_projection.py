"""Model-only media projection. Stored source bytes and offsets never change.

Encoded images/audio/attachments are opaque references, not visual evidence.
Only explicitly typed media is excluded; arbitrary encoded dataset text remains.
"""
import hashlib
import re


POLICY = 'references-v1'
_URI = re.compile(r'data:([A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+)(?:;[A-Za-z0-9=.+-]+)*;base64,[A-Za-z0-9+/=]+')
_DATA = re.compile(r'"data"\s*:\s*"([A-Za-z0-9+/=]+)"')
_MIME = re.compile(r'"(?:mimeType|mime_type|media_type)"\s*:\s*"([A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+)"')
_TYPE = re.compile(r'"type"\s*:\s*"(?:image|audio|video|base64|input_image|input_audio)"')
_FORMAT = re.compile(r'"format"\s*:\s*"(wav|mp3|pcm16|flac|ogg|webm)"')


def validate_policy(policy):
    if policy not in (None, POLICY):
        raise ValueError('invalid_media_policy')


def project_media(body):
    """Return exact readable ranges and bounded, non-citable media metadata."""
    ranges=[(match.start(),match.end(),match.group(1).lower()) for match in _URI.finditer(body)]
    for match in _DATA.finditer(body):
        start,end=match.span(1)
        if any(a<=start<z for a,z,_ in ranges):
            continue
        # Recognize siblings in this typed media object, not a nearby unrelated
        # object containing an ordinary dataset's base64 column.
        left=body.rfind('{',max(0,match.start()-4096),match.start())
        right=body.find('}',match.end(),min(len(body),match.end()+4096))
        if left<0 or right<0:
            continue
        metadata=body[left:match.start()]+body[match.end():right+1]
        mime=_MIME.search(metadata); audio=_FORMAT.search(metadata)
        if mime and (_TYPE.search(metadata) or mime.group(1).startswith(('image/','audio/','video/'))):
            kind=mime.group(1).lower()
        elif audio:
            kind='audio/'+audio.group(1)
        else:
            continue
        ranges.append((start,end,kind))
    ranges.sort()
    if len(ranges)>256:
        raise ValueError('media_reference_count_exceeds_limit')
    spans=[]; references=[]; offset=0
    for start,end,mime in ranges:
        if start<offset:
            continue
        if start>offset:
            spans.append((offset,start))
        references.append(dict(start=start,end=end,mime_type=mime,
            encoded_characters=end-start,sha256=hashlib.sha256(body[start:end].encode()).hexdigest(),
            content_is_evidence=False))
        offset=end
    if offset<len(body):
        spans.append((offset,len(body)))
    return spans,references


def context_text(body):
    """Readable ranking/sizing view only; never replaces the original body."""
    ranges,_=project_media(body)
    return ''.join(body[start:end] for start,end in ranges)
