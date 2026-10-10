"""Explicit cloud-local response capabilities without expanding legacy envelopes."""
import json
VERSION='cloud-local-1'

def validate_response(path,value):
    if not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=True))>524288:
        raise ValueError('invalid_cloud_response')
    route=path.removeprefix('/enterprise/v3/')
    if route in {'auth/credential','auth/renew'}:
        # Renewal also reports the rotated refresh token's idle and absolute limits.
        expiries=('expires_at',)+(('refresh_expires_at','session_expires_at') if route=='auth/renew' else ())
        if set(value)!={'tenant','principal','actor','enrollment','actions',*expiries}:
            raise ValueError('invalid_credential_metadata')
        if (any(not isinstance(value[k],str) or not value[k] for k in ('tenant','principal','actor','enrollment'))
                or not isinstance(value['actions'],list) or not all(isinstance(v,str) for v in value['actions'])
                or any(type(value[k]) not in (int,float) for k in expiries)):
            raise ValueError('invalid_credential_metadata')
    elif route=='auth/revoke':
        if value!={'revoked':True}:raise ValueError('invalid_revocation_response')
    elif route=='search':
        if not {'results','answerable'}<=set(value) or set(value)-{'results','answerable','coverage_gaps','retrieval','support'}:
            raise ValueError('invalid_cloud_search')
        if type(value['answerable']) is not bool or not isinstance(value['results'],list) or len(value['results'])>20:
            raise ValueError('invalid_cloud_search')
        if 'support' in value:
            expected='complete' if value['answerable'] else ('partial' if value['results'] else 'none')
            if value['support']!=expected:raise ValueError('invalid_cloud_support')
        for card in value['results']:
            if not {'id','revision','title','lesson','evidence_status'}<=set(card) or len(json.dumps(card,ensure_ascii=True))>1300:
                raise ValueError('invalid_cloud_card')
        if not value['results'] and value['answerable']:raise ValueError('unsupported_cloud_answer')
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
    elif route=='feedback':
        if (set(value)!={'status','id','classification','independently_verified'} or value['status']!='recorded' or value['classification']!='agent_self_report' or value['independently_verified'] is not False):raise ValueError('invalid_feedback_response')
    elif route=='preferences':
        if not isinstance(value.get('preferences'),list):raise ValueError('invalid_preference_response')
    else:raise ValueError('unknown_cloud_response')
    return value
