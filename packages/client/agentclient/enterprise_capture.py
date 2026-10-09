"""Faithful, deterministic enterprise capture; never imports or calls a model."""
import uuid

from agentclient.cleaning import SECRET_KEYS, REDACTED_SECRET, clean_private
from agentclient.general_contract import VERSION, validate_event

SELF_TOOLS = {'agentnetwork_memory', 'mcp__agentnetwork__agentnetwork_memory'}


def redact(value, field='$', manifest=None):
    """Filter before hashing/splitting. Manifest contains locations, never secrets."""
    manifest = [] if manifest is None else manifest
    if isinstance(value, str):
        safe, reasons = clean_private(value)
        if reasons:
            manifest.append({'field': field, 'reason': 'credential_pattern'})
        return safe, manifest
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe_key, reasons = clean_private(str(key))
            if reasons:
                manifest.append({'field': field, 'reason': 'credential_key'})
            if safe_key in result:
                raise ValueError('redacted_key_collision')
            if SECRET_KEYS.fullmatch(str(key)) and item not in (None, '', REDACTED_SECRET):
                result[safe_key] = REDACTED_SECRET
                manifest.append({'field': field + '.' + safe_key, 'reason': 'credential_field'})
            else:
                result[safe_key] = redact(item, field + '.' + safe_key, manifest)[0]
        return result, manifest
    if isinstance(value, list):
        return [redact(item, f'{field}[{i}]', manifest)[0] for i, item in enumerate(value)], manifest
    return value, manifest


def normalize_capture(event, project, connection, *, source_type='agent', origin='host_hook'):
    kind = event.get('hook_event_name', '')
    tool = str(event.get('tool_name', ''))
    blocks = [];role = 'unknown';channel = '';complete = False;disposition = 'accepted'
    if event.get('origin') == 'backend_observer' or tool.replace('-', '_').lower() in SELF_TOOLS:
        disposition = 'excluded'
    elif kind == 'UserPromptSubmit':
        role = 'user';blocks = [{'type': 'text', 'value': str(event.get('prompt', ''))}]
    elif kind in {'AssistantMessage', 'Stop'}:
        role = 'assistant';channel = event.get('channel', 'final' if kind == 'Stop' else 'commentary')
        blocks = [{'type': 'text', 'value': str(event.get('last_assistant_message', event.get('message', '')))}]
        complete = kind == 'Stop'
    elif kind == 'PostToolUse':
        role = 'tool'
        blocks = [{'type': 'tool_arguments', 'value': event.get('tool_input', {})},
                  {'type': 'tool_result', 'value': event.get('tool_response', {})}]
    elif kind == 'Attachment':
        blocks = [{'type': 'attachment', 'value': event['attachment']}]
    elif kind == 'PostCompact':
        # Intake records source lifecycle; trusted receiver boundary is a different route.
        role = 'source'
        blocks = [{'type':'record_fields','value':{'trigger':event.get('trigger','unknown')}}]
    elif kind == 'gap':
        disposition = 'incomplete'
    else: disposition = 'unsupported'
    response = event.get('tool_response', {})
    code = event.get('exit_code', response.get('exit_code') if isinstance(response, dict) else None)
    if type(code) is not int: code = None
    safe_blocks, redactions = redact(blocks, '$.blocks')
    if redactions and disposition == 'accepted': disposition = 'redacted'
    # No equal-text dedup: caller's native event ID, or a unique hook receipt.
    ident = str(event.get('event_id') or event.get('capture_id') or event.get('tool_use_id') or uuid.uuid4().hex)
    value = {'version': VERSION, 'connection': connection, 'external_id': ident,
        'revision': str(event.get('revision', '1')), 'source_type': source_type,
        'project': project, 'conversation': str(event.get('session_id', '')),
        'turn': str(event.get('turn_id', '')), 'actor': str(event.get('speaker', '')),
        'source_url': str(event.get('source_url', '')), 'occurred_at': event.get('timestamp', 'unknown'),
        'origin': 'context_transfer' if any(x in tool.lower() for x in ('read_thread', 'wait_threads')) else origin,
        'disposition': disposition, 'redactions': redactions,
        'event': {'kind': kind, 'role': role, 'channel': channel,
            'order': event.get('source_order'), 'tool_name': tool,
            'call_id': str(event.get('tool_use_id', '')), 'parent_id': str(event.get('parent_id', '')),
            'complete': complete, 'expected_events': event.get('expected_events', []), 'exit_code': code},
        'blocks': safe_blocks}
    # Preserve cwd explicitly rather than dereferencing files.
    if kind == 'PostToolUse' and event.get('cwd'):
        value['blocks'].append({'type': 'record_fields', 'value': {'cwd': str(event['cwd'])}})
    value, extra = redact(value)
    value['redactions'] += extra
    if extra and value['disposition'] == 'accepted': value['disposition'] = 'redacted'
    return validate_event(value)


def capabilities():
    return {'adapter': 'codex-desktop-item-completed-and-hooks-v2',
        'capture_owner': 'explicit hooks or desktop, never both for one conversation',
        'verified_shapes': ['UserMessage', 'AgentMessage', 'CommandExecution', 'McpToolCall', 'FileChange',
            'ContextCompaction', 'Plan', 'WebSearch', 'Extension', 'DynamicToolCall',
            'SubAgentActivity', 'CollabAgentToolCall', 'task_complete'],
        'reference_only': ['attachments'], 'excluded': ['reasoning', 'system', 'developer', 'backend_observer', 'retrieved_memory'],
        'unknown_types': 'explicit unsupported disposition', 'local_model_calls': 0}
