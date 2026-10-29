"""Enterprise v2 source/transport contract. v1 remains a declared separate adapter.

No tenant, reader list, visibility grant or provider choice comes from a source.
Optional `sharing` (redacted passages) and `originals` (unredacted originals) fields
carry a repository scope file's request; the service caps them by the person's own
setting, project and tenant policy and connection visibility, so they never grant access.
An optional `origin_label` carries what the event touched (`agentclient.origin`):
its `ceiling` can only lower the stored levels, never raise them.
Optional `workspace` (an alias of `project`; both must agree) and `remote` (the
repository's normalized remote, `normalize_remote`) name where the capture comes
from; the service rejects a remote that is not linked to the named workspace.
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
# Origin labels (agentclient.origin): the sharing ladder, most restrictive first;
# what an event touched; why it (or its session before it) was restricted; and
# how many other projects one label may name. Categories only, never content.
ORIGIN_LEVELS = ('private', 'team', 'organization')
ORIGIN_KINDS = ('project', 'web', 'allowed_path', 'allowed_mcp', 'local_tooling', 'outside_repo', 'other_project',
                'mcp', 'shell', 'private_network', 'recalled_memory', 'unknown_tool', 'unknown_call')
ORIGIN_REASONS = ('outside_repo', 'shell_outside_repo', 'personal_content', 'credential_file', 'other_project',
                  'mcp_server', 'private_network', 'recalled_memory', 'unknown_tool', 'unknown_call')
ORIGIN_MAX_PROJECTS = 16
ORIGIN_PROJECT = re.compile(r'[A-Za-z0-9_-]{1,120}')
# A normalized repository remote: host/path, lower case, without scheme, user,
# port or a trailing `.git` or `/`. Paths may have more than two segments.
REMOTE = re.compile(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?:/[a-z0-9._~+-]{1,128}){1,16}')
REMOTE_BYTES = 512
_SCP_REMOTE = re.compile(r'(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)')
_URL_REMOTE = re.compile(r'(?:https?|ssh|git|git\+ssh|ssh\+git)://(?:[^@/\s]+@)?([^:/\s]+)(?::[0-9]*)?/(.+)', re.I)


def normalize_remote(url):
    """`host/path` for a Git remote URL, or None when it names no hosted repository.

    `https://example.com/Example-Org/Maple.git`, `ssh://git@example.com:22/example-org/maple`
    and `git@example.com:example-org/maple.git` all become `example.com/example-org/maple`.
    Local paths and `file://` remotes name no shared repository and give None.
    """
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any(c.isspace() for c in url.strip()):
        return None
    url = url.strip()
    match = _URL_REMOTE.fullmatch(url)
    if match is None and '://' not in url and not url.startswith(('/', '.', '~')):
        match = _SCP_REMOTE.fullmatch(url)
        if match is not None and len(match.group(1)) == 1:
            return None  # a Windows drive letter (C:/...), not an scp-style host
    if match is None:
        return None
    host, path = match.group(1).lower(), match.group(2).lower().strip('/')
    while path.endswith('.git') or path.endswith('/'):
        path = path[:-4] if path.endswith('.git') else path[:-1]
    path = '/'.join(segment for segment in path.split('/') if segment)
    value = host + '/' + path
    if len(value) > REMOTE_BYTES or not REMOTE.fullmatch(value) or '..' in value.split('/'):
        return None
    return value


def validate_remote(value):
    if not isinstance(value, str) or len(value) > REMOTE_BYTES or not REMOTE.fullmatch(value) \
            or normalize_remote('https://' + value) != value:
        raise ValueError('invalid_remote')
    return value


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
        'actor', 'source_url', 'disposition', 'redactions', 'origin'},
        ('sharing', 'originals', 'origin_label', 'workspace', 'remote'))
    if value['version'] != VERSION or value['source_type'] not in SOURCE_TYPES:
        raise ValueError('unsupported_general_version_or_type')
    # A scope-file request, never a grant: the service caps it by every ceiling.
    # Only a level above private is sent; omission means private, which older
    # services expect. A service before migration 045 accepts only 'team' sharing.
    for key in ('sharing', 'originals'):
        if key in value and value[key] not in ('team', 'organization'):
            raise ValueError('invalid_sharing_request')
    if 'origin_label' in value:
        validate_origin_label(value['origin_label'])
    if 'workspace' in value and value['workspace'] != value['project']:
        raise ValueError('workspace_project_mismatch')
    if 'remote' in value:
        validate_remote(value['remote'])
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


def validate_origin_label(label):
    """{'ceiling', 'kinds', 'reason', 'projects', 'inherited'}.

    `projects` names other projects whose content the event carries: the service
    shares it no wider than every one of them allows, and for team sharing only
    with members of all of them. A restricted label carries its reason."""
    object_fields(label, {'ceiling', 'kinds', 'reason', 'projects', 'inherited'})
    if label['ceiling'] not in ORIGIN_LEVELS:
        raise ValueError('invalid_origin_ceiling')
    if (not isinstance(label['kinds'], list) or len(label['kinds']) > len(ORIGIN_KINDS)
            or len(set(label['kinds'])) != len(label['kinds'])
            or any(kind not in ORIGIN_KINDS for kind in label['kinds'])):
        raise ValueError('invalid_origin_kinds')
    projects = label['projects']
    if (not isinstance(projects, list) or len(projects) > ORIGIN_MAX_PROJECTS or len(set(projects)) != len(projects)
            or any(not isinstance(p, str) or not ORIGIN_PROJECT.fullmatch(p) for p in projects)):
        raise ValueError('invalid_origin_projects')
    if type(label['inherited']) is not bool:
        raise ValueError('invalid_origin_inherited')
    restricted = label['ceiling'] != ORIGIN_LEVELS[-1] or bool(projects)
    if (label['reason'] not in ORIGIN_REASONS) if restricted else label['reason'] != '':
        raise ValueError('invalid_origin_reason')
    return label


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
        # `notice` is a status line for the person (for example an automatic repository link).
        object_fields(value, {'disposition', 'digest', 'received_parts', 'complete', 'source_id'}, ('notice',))
        if 'notice' in value: text(value['notice'], 300)
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
