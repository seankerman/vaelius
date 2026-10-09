"""Enterprise v2 source/transport contract. v1 remains a declared separate adapter.

No tenant, reader list, visibility grant or provider choice comes from a source.
Connection enrollment is a trusted server operation, independently of content.
"""
import base64
import hashlib
import json
import math
import re

VERSION = 'enterprise-local-2'
REQUEST_BYTES = 262_144
PART_BYTES = 131_072
EVENT_BYTES = 8_388_608
MAX_PARTS = 128
SOURCE_TYPES = {'agent', 'conversation', 'document', 'record'}
DISPOSITIONS = {'accepted', 'redacted', 'excluded', 'unsupported', 'incomplete'}
BLOCK_TYPES = {'text', 'tool_arguments', 'tool_result', 'diff', 'record_fields', 'attachment'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def object_fields(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= set(value)
            or set(value) - set(required) - set(optional)):
        raise ValueError('invalid_general_fields')


def text(value, limit=256, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError('invalid_general_text')
    return value


def validate_event(value):
    object_fields(value, {'version', 'connection', 'external_id', 'revision', 'source_type',
        'project', 'conversation', 'turn', 'event', 'blocks', 'occurred_at',
        'actor', 'source_url', 'disposition', 'redactions', 'origin'})
    if value['version'] != VERSION or value['source_type'] not in SOURCE_TYPES:
        raise ValueError('unsupported_general_version_or_type')
    for key in ('connection', 'external_id', 'project', 'conversation', 'turn', 'actor'):
        text(value[key], empty=key in {'conversation', 'turn', 'actor'})
    text(value['revision'], 16)
    if not re.fullmatch(r'[1-9][0-9]{0,14}', value['revision']):
        raise ValueError('invalid_general_revision')
    text(value['source_url'], 2048, empty=True)
    text(value['origin'], 128)
    if value['disposition'] not in DISPOSITIONS:
        raise ValueError('invalid_capture_disposition')
    if (not isinstance(value['occurred_at'], (str, int, float)) or
            isinstance(value['occurred_at'], bool) or
            isinstance(value['occurred_at'], float) and not math.isfinite(value['occurred_at'])):
        raise ValueError('invalid_general_time')
    if isinstance(value['occurred_at'], str): text(value['occurred_at'], 128)
    event = value['event']
    object_fields(event, {'kind', 'role', 'channel', 'order', 'tool_name', 'call_id',
                         'parent_id', 'complete', 'expected_events', 'exit_code'})
    for key in ('kind', 'role', 'channel', 'tool_name', 'call_id', 'parent_id'):
        text(event[key], empty=True)
    if event['role'] not in {'user', 'assistant', 'tool', 'source', 'unknown'}:
        raise ValueError('invalid_event_role')
    if event['order'] is not None and (type(event['order']) is not int or event['order'] < 0):
        raise ValueError('invalid_event_order')
    if type(event['complete']) is not bool:
        raise ValueError('invalid_event_completeness')
    if not isinstance(event['expected_events'], list) or len(event['expected_events']) > 1000:
        raise ValueError('invalid_expected_events')
    for ident in event['expected_events']: text(ident)
    if event['exit_code'] is not None and (type(event['exit_code']) is not int or abs(event['exit_code']) > 65535):
        raise ValueError('invalid_exit_code')
    if not isinstance(value['blocks'], list) or len(value['blocks']) > 128:
        raise ValueError('invalid_content_blocks')
    for block in value['blocks']:
        object_fields(block, {'type', 'value'})
        if block['type'] not in BLOCK_TYPES:
            raise ValueError('unsupported_content_block')
        if block['type'] in {'text', 'diff'} and not isinstance(block['value'], str):
            raise ValueError('invalid_text_block')
        if block['type'] == 'attachment':
            object_fields(block['value'], {'reference', 'media_type', 'name'})
            for entry in block['value'].values(): text(entry, 2048, empty=True)
    if not isinstance(value['redactions'], list) or len(value['redactions']) > 1000:
        raise ValueError('invalid_redactions')
    for redaction in value['redactions']:
        object_fields(redaction, {'field', 'reason'})
        text(redaction['field'], 1024);text(redaction['reason'], 80)
    if len(canonical(value)) > EVENT_BYTES:
        raise ValueError('logical_event_too_large')
    return value


def validate_part(value):
    object_fields(value, {'version', 'connection', 'external_id', 'revision', 'digest',
                         'part_index', 'part_count', 'data'})
    if value['version'] != VERSION:
        raise ValueError('unsupported_general_version')
    for key in ('connection', 'external_id', 'revision'): text(value[key])
    if not re.fullmatch(r'[0-9a-f]{64}', value['digest']):
        raise ValueError('invalid_event_digest')
    if (type(value['part_count']) is not int or not 1 <= value['part_count'] <= MAX_PARTS
            or type(value['part_index']) is not int
            or not 0 <= value['part_index'] < value['part_count']):
        raise ValueError('invalid_part_order')
    if len(canonical(value)) > PART_BYTES:
        raise ValueError('part_too_large')
    try: base64.b64decode(value['data'], validate=True)
    except (TypeError, ValueError): raise ValueError('invalid_part_encoding') from None
    return value


def split_event(value):
    validate_event(value)
    raw = canonical(value)
    chunk_size = 90_000
    count = math.ceil(len(raw) / chunk_size)
    parts = [{'version': VERSION, 'connection': value['connection'],
        'external_id': value['external_id'], 'revision': value['revision'],
        'digest': hashlib.sha256(raw).hexdigest(), 'part_index': i, 'part_count': count,
        'data': base64.b64encode(raw[i * chunk_size:(i + 1) * chunk_size]).decode('ascii')}
        for i in range(count)]
    for part in parts: validate_part(part)
    return parts


def validate_response(path, value):
    route = path.removeprefix('/enterprise/v2/').split('/')[0]
    if route == 'parts':
        object_fields(value, {'disposition', 'digest', 'received_parts', 'complete', 'source_id'})
        if (value['disposition'] not in {'accepted', 'duplicate', 'incomplete', 'redacted', 'excluded', 'unsupported'}
                or type(value['complete']) is not bool
                or type(value['received_parts']) is not int or value['received_parts'] < 0):
            raise ValueError('invalid_part_receipt')
        text(value['digest'], 64);text(value['source_id'], empty=True)
    elif route == 'sources':
        object_fields(value, {'source_id', 'revision', 'payload'})
        text(value['source_id']);text(value['revision']);validate_event(value['payload'])
    elif route == 'release':
        object_fields(value,{'document_id','disposition'},{'revision'})
        text(value['document_id'])
        if value['disposition'] not in {'accepted','duplicate'}:raise ValueError('invalid_release_response')
    else: raise ValueError('invalid_general_response_route')
    return value
