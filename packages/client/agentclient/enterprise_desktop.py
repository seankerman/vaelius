"""Forward-only structured Codex capture using verified local rollout shapes."""
import hashlib
import json
import os
from pathlib import Path

from agentclient.enterprise_capture import normalize_capture


def convert(record, session, project, connection, *, turn='', order=None):
    payload=record.get('payload', {})
    if not isinstance(payload, dict): return None
    typ=record.get('type');event={'session_id':session, 'turn_id':payload.get('turn_id',turn),
        'timestamp':record.get('timestamp','unknown'), 'source_order':order}
    if typ == 'compacted':
        event.update(hook_event_name='PostCompact',event_id='compact:'+str(order))
    elif typ == 'response_item':
        kind=payload.get('type')
        # Visible messages are owned by item_completed. Hidden messages never captured.
        if kind in {'message','reasoning'}: return None
        if kind in {'function_call', 'custom_tool_call', 'function_call_output','custom_tool_call_output'}:
            ident=str(payload.get('id') or payload.get('call_id') or order)
            is_output=kind.endswith('output')
            event.update(hook_event_name='PostToolUse',event_id='protocol:'+ident+':'+kind,
                tool_name=str(payload.get('name','source_tool')),
                tool_use_id=str(payload.get('call_id',ident)),
                tool_input={} if is_output else payload.get('arguments',payload.get('input',{})),
                tool_response=payload.get('output',{}) if is_output else {})
        elif kind in {'web_search_call','tool_search_call','tool_search_output'}:
            # Keep the historical protocol identity stable so a corrected
            # adapter can replace an unsupported source with an explicit revision.
            event.update(hook_event_name='PostToolUse',event_id='protocol:'+str(order),
                tool_name='WebSearch' if kind=='web_search_call' else 'ToolSearch',
                tool_use_id=str(payload.get('call_id') or order),
                tool_input={key:payload[key] for key in ('action','arguments') if key in payload},
                tool_response={key:payload[key] for key in ('tools','status','execution') if key in payload})
        elif kind == 'agent_message':
            from agentclient.desktop_capture import text_content
            # Only the visible report, not encrypted/hidden companion parts.
            event.update(hook_event_name='AssistantMessage',event_id='protocol:'+str(order),
                channel='collaboration',speaker=str(payload.get('author','')),
                parent_id=str(payload.get('recipient','')),message=text_content(payload.get('content')))
        else:
            event.update(hook_event_name='UnsupportedProtocol',event_id='protocol:'+str(order))
    elif typ == 'event_msg':
        if payload.get('type') == 'task_complete':
            event.update(hook_event_name='Stop',event_id='stop:'+str(event['turn_id']),
                last_assistant_message=payload.get('last_agent_message',''))
        elif payload.get('type') == 'item_completed' and payload.get('thread_id') == session:
            item=payload.get('item',{})
            if not isinstance(item,dict) or not item.get('id'): return None
            event['event_id']=str(item['id']);kind=item.get('type')
            if kind == 'UserMessage':
                from agentclient.desktop_capture import text_content
                event.update(hook_event_name='UserPromptSubmit',prompt=text_content(item.get('content')))
            elif kind == 'AgentMessage':
                from agentclient.desktop_capture import text_content
                content=item.get('content')
                message=content if isinstance(content,str) else text_content(content)
                event.update(hook_event_name='AssistantMessage',channel=item.get('phase') or 'unknown',message=message)
            elif kind == 'CommandExecution':
                event.update(hook_event_name='PostToolUse',tool_name=kind,tool_use_id=item['id'],
                    cwd=item.get('cwd',''),tool_input={'command':item.get('command','')},
                    tool_response={key:item[key] for key in ('aggregated_output','stdout','stderr','status','exit_code','duration') if key in item})
            elif kind == 'McpToolCall':
                event.update(hook_event_name='PostToolUse',tool_name=item.get('tool',''),tool_use_id=item['id'],
                    tool_input=item.get('arguments',{}),tool_response={key:item[key] for key in ('result','status','duration') if key in item})
            elif kind == 'FileChange':
                event.update(hook_event_name='PostToolUse',tool_name=kind,tool_use_id=item['id'],
                    tool_input={'changes':item.get('changes',[])},
                    tool_response={key:item[key] for key in ('status','stdout','stderr') if key in item})
            elif kind == 'FunctionCallOutput':
                event.update(hook_event_name='PostToolUse',tool_name=item.get('name',''),tool_use_id=item['id'],
                    tool_response=item.get('output',{}))
            elif kind == 'ContextCompaction':
                event.update(hook_event_name='PostCompact',trigger='host_item',event_id=item['id'])
            elif kind == 'Plan':
                event.update(hook_event_name='AssistantMessage',channel='plan',message=item.get('text',''))
            elif kind == 'WebSearch':
                event.update(hook_event_name='PostToolUse',tool_name='WebSearch',tool_use_id=item['id'],
                    tool_input={key:item[key] for key in ('query','action') if key in item},tool_response={})
            elif kind == 'Extension':
                event.update(hook_event_name='PostToolUse',tool_name='Extension:'+str(item.get('kind','unknown')),
                    tool_use_id=item['id'],tool_input={key:item[key] for key in ('query','action') if key in item},
                    tool_response={key:item[key] for key in ('results','status','durationMs') if key in item})
            elif kind == 'DynamicToolCall':
                event.update(hook_event_name='PostToolUse',tool_name=str(item.get('tool','DynamicToolCall')),
                    tool_use_id=item['id'],tool_input=item.get('arguments',{}),
                    tool_response={key:item[key] for key in ('content_items','status','success','duration') if key in item})
            elif kind == 'SubAgentActivity':
                event.update(hook_event_name='PostToolUse',tool_name='SubAgentActivity',tool_use_id=item['id'],
                    tool_input={key:item[key] for key in ('kind','agent_path','agent_thread_id') if key in item},
                    tool_response={})
            elif kind == 'CollabAgentToolCall':
                event.update(hook_event_name='PostToolUse',tool_name=str(item.get('tool','CollabAgentToolCall')),
                    tool_use_id=item['id'],tool_input={key:item[key] for key in ('receiver_agents','receiver_thread_ids','sender_thread_id') if key in item},
                    tool_response={key:item[key] for key in ('agents_states','status') if key in item})
            elif kind == 'Reasoning': return None
            else: event.update(hook_event_name='UnsupportedHostItem')
        else: return None
    else: return None
    return normalize_capture(event,project,connection,origin='desktop_rollout')


def poll(home, config, allowed_root=None):
    from agentclient.desktop_capture import checked_path
    from agentclient.capture_outbox import Outbox
    from agentclient.transport import enterprise_client, capture_connection
    folder=Path(home);path=folder/'enterprise-transcript-cursors.json'
    cursors=json.loads(path.read_text()) if path.exists() else {}
    backend_config=config['knowledge_backend']
    if backend_config.get('capture_owner') != 'desktop': raise ValueError('desktop_capture_owner_required')
    def save():
        tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(cursors,sort_keys=True));tmp.chmod(0o600);os.replace(tmp,path)
    outbox=Outbox(home);captured=0;gaps=0
    try:outbox.queue_diagnostics(config)
    except ValueError:pass  # Durable metadata remains pending under backpressure.
    def gap_event(session,project,cursor,reason,identity):
        event=normalize_capture({'hook_event_name':'gap','session_id':session,
            'turn_id':cursor.get('turn',''),'event_id':'gap:'+hashlib.sha256(
                json.dumps([session,cursor.get('offset'),reason,identity],sort_keys=True).encode()).hexdigest()},
            project,capture_connection(config,project))
        event['blocks']=[{'type':'record_fields','value':{'capture_gap':reason}}]
        outbox.put(event)
    try:
        for session,source_path in config.get('desktop_transcripts',{}).items():
            project=config.get('sessions',{}).get(session)
            if not project: continue
            source=checked_path(source_path,session,allowed_root);stat=source.stat()
            key=hashlib.sha256(session.encode()).hexdigest()
            identity={'path_hash':hashlib.sha256(str(source).encode()).hexdigest(),'device':stat.st_dev,'inode':stat.st_ino}
            cursor=cursors.get(key)
            if cursor is None:
                cursors[key]={**identity,'offset':stat.st_size,'status':'following','turn':''};save();continue
            if any(cursor.get(k)!=v for k,v in identity.items()) or stat.st_size<cursor['offset']:
                gap_event(session,project,cursor,'transcript_changed',identity)
                cursor['status']='transcript_changed';save();gaps+=1;continue
            if config.get('paused') or project in config.get('capture_disabled_projects',[]):
                if stat.st_size>cursor['offset']: gap_event(session,project,cursor,'paused_capture',identity)
                cursor['status']='paused_gap';cursor['offset']=stat.st_size;save();gaps+=1;continue
            with source.open('rb') as stream:
                stream.seek(cursor['offset']);count=0
                while count<64:
                    before=stream.tell();line=stream.readline(32*1024*1024+1)
                    if not line: break
                    if not line.endswith(b'\n'):
                        if len(line)>32*1024*1024: gap_event(session,project,cursor,'oversize_line',identity)
                        cursor['status']='partial_or_oversize_line';gaps+=1;break
                    try:
                        record=json.loads(line);payload=record.get('payload',{})
                        if isinstance(payload,dict) and payload.get('turn_id'): cursor['turn']=payload['turn_id']
                        event=convert(record,session,project,capture_connection(config,project),turn=cursor.get('turn',''),order=before)
                    except (ValueError,TypeError,KeyError):
                        event=normalize_capture({'hook_event_name':'gap','session_id':session,
                            'turn_id':cursor.get('turn',''),'event_id':'parse-gap:'+str(before)},project,capture_connection(config,project))
                    if event:
                        try: outbox.put(event)
                        except ValueError as exc:
                            if str(exc)=='logical_event_too_large':
                                event=normalize_capture({'hook_event_name':'gap','session_id':session,
                                    'turn_id':cursor.get('turn',''),'event_id':'oversize-gap:'+str(before)},project,capture_connection(config,project));outbox.put(event)
                            else: cursor['status']='outbox_backpressure';gaps+=1;break
                        captured+=1;gaps+=event['disposition'] in {'unsupported','excluded','incomplete'}
                    # Durable redacted spool allows restart/replay; server ACK deletes it.
                    cursor['offset']=stream.tell();cursor['status']='following';count+=1;save()
        receipt=outbox.drain(enterprise_client(config,timeout=1),max_events=32,max_seconds=10)
        return {'captured':captured,'gaps':gaps,'transport':receipt,'cursor_file':str(path)}
    finally: outbox.close()
