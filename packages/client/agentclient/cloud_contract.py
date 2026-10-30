"""Explicit cloud-local response capabilities without expanding legacy envelopes."""
import json
VERSION='cloud-local-1'

def validate_response(path,value):
    if not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=True))>524288:
        raise ValueError('invalid_cloud_response')
    route=path.removeprefix('/enterprise/v3/')
    if route in {'auth/credential','auth/renew','auth/adopt'}:
        # Renewal also reports the rotated refresh token's idle and absolute limits.
        expiries=('expires_at',)+(('refresh_expires_at','session_expires_at') if route=='auth/renew' else ())
        if set(value)!={'tenant','principal','actor','enrollment','actions',*expiries}:
            raise ValueError('invalid_credential_metadata')
        if (any(not isinstance(value[k],str) or not value[k] for k in ('tenant','principal','actor','enrollment'))
                or not isinstance(value['actions'],list) or not all(isinstance(v,str) for v in value['actions'])
                or any(type(value[k]) not in (int,float) for k in expiries)):
            raise ValueError('invalid_credential_metadata')
    elif route=='auth/aws-sts':
        if set(value)!={'credential','tenant','principal','actor','enrollment','actions','expires_at'}:
            raise ValueError('invalid_credential_metadata')
        if (any(not isinstance(value[k],str) or not value[k] for k in ('credential','tenant','principal','actor','enrollment'))
                or len(value['credential'])>256 or not isinstance(value['actions'],list)
                or not all(isinstance(v,str) for v in value['actions']) or type(value['expires_at']) not in (int,float)):
            raise ValueError('invalid_credential_metadata')
    elif route=='auth/revoke':
        if value!={'revoked':True}:raise ValueError('invalid_revocation_response')
    elif route=='search':
        if not {'results','answerable'}<=set(value) or set(value)-{'results','answerable','coverage_gaps','retrieval','support',
                                                                    'session_epoch'}:
            raise ValueError('invalid_cloud_search')
        if 'session_epoch' in value and type(value['session_epoch']) is not int:raise ValueError('invalid_cloud_search')
        if type(value['answerable']) is not bool or not isinstance(value['results'],list) or len(value['results'])>20:
            raise ValueError('invalid_cloud_search')
        if 'support' in value:
            expected='complete' if value['answerable'] else ('partial' if value['results'] else 'none')
            if value['support']!=expected:raise ValueError('invalid_cloud_support')
        for card in value['results']:
            if not {'id','revision','title','lesson','evidence_status'}<=set(card) or len(json.dumps(card,ensure_ascii=True))>1300:
                raise ValueError('invalid_cloud_card')
        if not value['results'] and value['answerable']:raise ValueError('unsupported_cloud_answer')
    elif route=='workspaces/descriptor':
        # The session label's advisory descriptor (agentclient.session_label).
        lists=('contains_session_audience','readable')
        if (not {'workspace','epoch','role','session','fetched_at','refresh_after','expires_at',*lists}<=set(value)
                or not isinstance(value['workspace'],dict) or not isinstance(value['session'],dict)
                or type(value['epoch']) is not int
                or any(not isinstance(value[k],list) or len(value[k])>10000
                       or any(not isinstance(item,dict) for item in value[k]) for k in lists)
                or any(type(value[k]) not in (int,float) for k in ('fetched_at','refresh_after','expires_at'))
                # Optional (newer services): who of the session audience each narrower workspace leaves out.
                or not isinstance(value.get('narrower',[]),list) or len(value.get('narrower',[]))>10000
                or any(not isinstance(item,dict) or not isinstance(item.get('missing_readers',{}),dict)
                       for item in value.get('narrower',[]))):
            raise ValueError('invalid_session_descriptor')
    elif route=='document-evidence':
        if len(json.dumps(value,ensure_ascii=True))>4000:raise ValueError('source_evidence_bound')
        if value.get('status')=='invalidated':
            if set(value)!={'id','status','current_revision'}:raise ValueError('invalid_source_evidence')
        else:
            if not {'id','revision','sources','coverage_gaps','next_offset','diagnostic_source_inspection'}<=set(value):
                raise ValueError('invalid_source_evidence')
            if value['diagnostic_source_inspection'] is not True or not isinstance(value['sources'],list) or len(value['sources'])>3:
                raise ValueError('invalid_source_evidence')
            offset=value['next_offset']
            if offset is not None and (type(offset) is not int or not 0<=offset<=10000):raise ValueError('invalid_source_evidence_offset')
            for source in value['sources']:
                if not isinstance(source,dict) or not {'source_id','segment_id','text','start','end','source_version','owner','kind','occurred_at'}<=set(source):
                    raise ValueError('invalid_source_evidence_span')
                if (not isinstance(source['text'],str) or type(source['start']) is not int or
                    type(source['end']) is not int or source['start']<0 or source['end']-source['start']!=len(source['text'])):
                    raise ValueError('invalid_source_evidence_span')
    elif route=='timeline':
        if not {'id','episodes','coverage_gaps','has_more'}<=set(value):raise ValueError('invalid_cloud_timeline')
        if 'next_cursor' in value and (value['next_cursor'] is not None and
                (not isinstance(value['next_cursor'],str) or len(value['next_cursor'])!=68 or
                 not value['next_cursor'].startswith('tc1_'))):
            raise ValueError('invalid_cloud_timeline_cursor')
    elif route=='temporal-detail':
        if not {'id','revision','claim'}<=set(value) or not str(value['id']).startswith('ta_'):
            raise ValueError('invalid_cloud_temporal_detail')
    elif route=='project-history':
        if not {'project','summary_revision','phase_anchors','episodes','coverage_gaps','has_more','next_cursor'}<=set(value):
            raise ValueError('invalid_cloud_project_history')
        if not isinstance(value['episodes'],list) or len(value['episodes'])>20:
            raise ValueError('invalid_cloud_project_history')
    elif route=='project-episode':
        if not {'episode_id','assertions','coverage_gaps'}<=set(value):
            raise ValueError('invalid_cloud_project_episode')
    elif route.startswith('source-documents/'):
        if route.endswith('describe'):
            if not {'source_id','version'}<=set(value) or len(json.dumps(value))>8000:raise ValueError('invalid_source_descriptor')
        elif route.endswith('list'):
            if not isinstance(value.get('documents'),list):raise ValueError('invalid_source_list')
        else:raise ValueError('unsupported_cloud_source_response')
    elif route=='sharing':
        # The access model replaces per-person `max_sharing` with the two-state
        # `contributions_default` (shared|private); `max_sharing` and
        # `tenant_ceiling` are still echoed for old clients, `widest_entry` is
        # optional, and `existing` carries the preview/confirm flow of
        # `--apply-to-existing`.
        legacy_levels={'private','team','organization'}
        allowed={'principal','max_sharing','contributions_default','changed','tenant_ceiling','widest_entry','existing'}
        if (not isinstance(value,dict) or 'principal' not in value or set(value)-allowed
                or type(value.get('changed',False)) is not bool):
            raise ValueError('invalid_sharing_response')
        if 'max_sharing' in value and value['max_sharing'] not in legacy_levels:
            raise ValueError('invalid_sharing_response')
        if 'contributions_default' in value and value['contributions_default'] not in {'shared','private'}:
            raise ValueError('invalid_sharing_response')
        if 'max_sharing' not in value and 'contributions_default' not in value:
            raise ValueError('invalid_sharing_response')
        if value.get('tenant_ceiling','private') not in legacy_levels:
            raise ValueError('invalid_sharing_response')
        if 'widest_entry' in value and value['widest_entry'] not in {'none','people_and_groups','organization'}:
            raise ValueError('invalid_sharing_response')
        if 'existing' in value:
            existing=value['existing']
            if not isinstance(existing,dict) or set(existing)-{'sources','confirm_token','applied'}:
                raise ValueError('invalid_sharing_response')
            if type(existing.get('sources',0)) is not int:
                raise ValueError('invalid_sharing_response')
            token=existing.get('confirm_token')
            if token is not None and (not isinstance(token,str) or len(token)!=32):
                raise ValueError('invalid_sharing_response')
            if 'applied' in existing and type(existing['applied']) is not int:
                raise ValueError('invalid_sharing_response')
    elif route=='workspaces':
        if (not isinstance(value,dict) or set(value)!={'workspaces'}
                or not isinstance(value['workspaces'],list)):
            raise ValueError('invalid_workspace_listing')
        for workspace in value['workspaces']:
            if (not isinstance(workspace,dict) or
                    not {'id','name','role','item_default','active','organization_entry'}<=set(workspace) or
                    not isinstance(workspace['id'],str) or not isinstance(workspace['name'],str) or
                    workspace['item_default'] not in {'shared','private'} or
                    type(workspace['active']) not in (bool,int) or
                    (workspace['role'] is not None and workspace['role'] not in {'reader','writer','admin'}) or
                    (workspace['organization_entry'] is not None and
                        workspace['organization_entry'] not in {'reader','writer','admin'})):
                raise ValueError('invalid_workspace_listing')
    elif route in {'workspaces/create','workspaces/show','workspaces/settings'}:
        if not isinstance(value,dict) or not isinstance(value.get('workspace'),dict):
            raise ValueError('invalid_workspace_response')
        workspace=value['workspace']
        if (not {'id','name','item_default','active'}<=set(workspace)
                or workspace['item_default'] not in {'shared','private'}):
            raise ValueError('invalid_workspace_response')
        if route=='workspaces/create':
            if value.get('role')!='admin':raise ValueError('invalid_workspace_response')
        if route=='workspaces/show':
            if (not {'role','entries','repos','audience_size'}<=set(value)
                    or not isinstance(value['entries'],list) or not isinstance(value['repos'],list)
                    or type(value['audience_size']) is not int):
                raise ValueError('invalid_workspace_response')
            for entry in value['entries']:
                if (not isinstance(entry,dict)
                        or not {'subject_kind','role'}<=set(entry)
                        or entry['subject_kind'] not in {'principal','group','organization'}
                        or entry['role'] not in {'reader','writer','admin'}):
                    raise ValueError('invalid_workspace_response')
        if route=='workspaces/settings' and type(value.get('changed',False)) is not bool:
            raise ValueError('invalid_workspace_response')
    elif route=='workspaces/entries/set':
        if (not {'workspace','entry','operation','audience_before','audience_after'}<=set(value)
                or value['operation'] not in {'added','changed','unchanged'}
                or type(value['audience_before']) is not int or type(value['audience_after']) is not int):
            raise ValueError('invalid_workspace_entry_response')
    elif route=='workspaces/entries/remove':
        if (not {'workspace','removed','audience_before','audience_after'}<=set(value)
                or type(value['removed']) not in (bool,int)
                or type(value['audience_before']) is not int or type(value['audience_after']) is not int):
            raise ValueError('invalid_workspace_entry_response')
    elif route in {'workspaces/repos/link','workspaces/repos/unlink'}:
        expected=(route.endswith('/link'))
        if (not {'workspace','remote','linked','changed'}<=set(value)
                or type(value['linked']) not in (bool,int) or bool(value['linked'])!=expected
                or type(value['changed']) not in (bool,int)):
            raise ValueError('invalid_repo_link_response')
    elif route=='groups':
        if (not isinstance(value,dict) or set(value)!={'groups'} or not isinstance(value['groups'],list)):
            raise ValueError('invalid_group_listing')
        for group in value['groups']:
            if (not isinstance(group,dict) or
                    not {'id','name','source','sync_state','active','admin','member'}<=set(group) or
                    group['source'] not in {'github','scim','local'} or
                    group['sync_state'] not in {'current','stale','failed'} or
                    type(group['active']) not in (bool,int) or
                    type(group['admin']) not in (bool,int) or type(group['member']) not in (bool,int)):
                raise ValueError('invalid_group_listing')
    elif route in {'groups/create','groups/show'}:
        if not isinstance(value,dict) or not isinstance(value.get('group'),dict):
            raise ValueError('invalid_group_response')
        group=value['group']
        if (not {'id','name','source'}<=set(group)
                or group['source'] not in {'github','scim','local'}):
            raise ValueError('invalid_group_response')
        if route=='groups/show':
            if not isinstance(value.get('members'),list):raise ValueError('invalid_group_response')
            for member in value['members']:
                if (not isinstance(member,dict) or not {'kind','id'}<=set(member)
                        or member['kind'] not in {'principal','group'}):
                    raise ValueError('invalid_group_response')
    elif route=='groups/members/add':
        if (not {'group','member_kind','member','role','changed'}<=set(value)
                or value['member_kind'] not in {'principal','group'}
                or value['role'] not in {'member','admin'}
                or type(value['changed']) not in (bool,int)):
            raise ValueError('invalid_group_member_response')
    elif route=='groups/members/remove':
        if (not {'group','member_kind','member','removed'}<=set(value)
                or value['member_kind'] not in {'principal','group'}
                or type(value['removed']) not in (bool,int)):
            raise ValueError('invalid_group_member_response')
    elif route=='groups/delete':
        if (not {'group','deleted'}<=set(value) or type(value['deleted']) not in (bool,int)):
            raise ValueError('invalid_group_delete_response')
    elif route in {'identity/github','identity/github/link/poll'}:
        # GitHub identity link: the account id and login only, never a token.
        statuses={'linked','pending','expired','denied'}
        if (set(value)-{'provider','linked','user_id','login','linked_at','github_groups','status','interval'}
                or ('status' in value and value['status'] not in statuses)
                or ('linked' in value and type(value['linked']) is not bool)
                or ('user_id' in value and type(value['user_id']) is not int)
                or ('login' in value and not isinstance(value['login'],str))
                or ('interval' in value and type(value['interval']) is not int)
                or (route=='identity/github' and 'linked' not in value)
                or (route=='identity/github/link/poll' and 'status' not in value)):
            raise ValueError('invalid_identity_link_response')
    elif route=='identity/github/link/start':
        if (set(value)!={'flow','user_code','verification_uri','expires_in','interval'}
                or not all(isinstance(value[k],str) and value[k] for k in ('flow','user_code','verification_uri'))
                or not value['verification_uri'].startswith('https://')
                or any(type(value[k]) is not int or value[k]<=0 for k in ('expires_in','interval'))):
            raise ValueError('invalid_identity_link_response')
    elif route=='identity/github/unlink':
        if set(value)!={'provider','principal','unlinked'} or type(value['unlinked']) is not bool:
            raise ValueError('invalid_identity_link_response')
    elif route=='my-data':
        listed=value.get('items') if value.get('view')=='sources' else value.get('sessions')
        if (value.get('view') not in ('sources','sessions') or not isinstance(listed,list) or len(listed)>200
                or type(value.get('more')) is not bool or not all(isinstance(item,dict) for item in listed)):
            raise ValueError('invalid_my_data_response')
    elif route in {'my-data/forget','my-data/sharing','my-data/grant','moderation/withdraw'}:
        # A preview carries dry_run=True and a confirm_token (null when nothing applies).
        if (type(value.get('dry_run')) is not bool or value.get('operation') not in
                ('forget','change_sharing','grant','moderate')
                or (value['dry_run'] and 'confirm_token' not in value)):
            raise ValueError('invalid_owner_operation_response')
        token=value.get('confirm_token')
        if token is not None and (not isinstance(token,str) or len(token)!=32):
            raise ValueError('invalid_owner_operation_response')
    elif route=='my-data/revoke':
        if value.get('operation')!='revoke' or type(value.get('revoked')) is not int:
            raise ValueError('invalid_revoke_response')
    elif route=='my-data/grants':
        if set(value)!={'issued','received'} or not all(isinstance(value[k],list) for k in value):
            raise ValueError('invalid_grants_response')
    elif route=='feedback':
        if (set(value)!={'status','id','classification','independently_verified'} or value['status']!='recorded' or value['classification']!='agent_self_report' or value['independently_verified'] is not False):raise ValueError('invalid_feedback_response')
    elif route=='preferences':
        if not isinstance(value.get('preferences'),list):raise ValueError('invalid_preference_response')
    elif route=='capture-notice':
        if (not {'notice','policy_url','captured','notice_version','source'}<=set(value)
                or set(value)-{'notice','policy_url','captured','notice_version','source','readers'}
                or not isinstance(value['notice'],str) or not 1<=len(value['notice'])<=2000
                or (value['policy_url'] is not None and (not isinstance(value['policy_url'],str)
                    or not value['policy_url'].startswith('https://') or len(value['policy_url'])>512))
                or not isinstance(value['captured'],list)
                or ('readers' in value and (not isinstance(value['readers'],dict)
                    or not isinstance(value['readers'].get('description'),str)))):
            raise ValueError('invalid_capture_notice')
    else:raise ValueError('unknown_cloud_response')
    return value
