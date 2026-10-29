"""Portable ASGI boundary around the canonical enterprise facade.

Default listener/proxy configuration is explicit. Startup is provider-free; errors
and access logs contain no source body. Authenticate again at delivery checkpoint.
"""
import json
import re
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from agenthub.enterprise import Denied, Conflict
from agenthub.workspace_access import RepoNotLinked
from agenthub.cloud_identity import IdentityError
from agenthub.request_timing import capture, span, timed, emit
from agenthub.write_limits import RateLimited, WriteTooLarge, check as check_write_limit

JSON_LIMIT=262144
FILE_LIMIT=50*1024*1024
NO_STORE={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
# JSON-only API: every response is private and never framed, sniffed or cached.
SECURITY_HEADERS=((b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
    (b'referrer-policy',b'no-referrer'),(b'x-frame-options',b'DENY'),
    (b'content-security-policy',b"default-src 'none'; frame-ancestors 'none'"))
HSTS=(b'strict-transport-security',b'max-age=31536000; includeSubDomains')
PROBES=('/health','/ready')


class SecureTransport:
    """Add secure response headers; with https_only, refuse non-HTTPS requests.

    Liveness/readiness probes stay reachable over plain HTTP so a load balancer can
    check the container directly. Every other request must arrive as HTTPS: direct
    TLS, or X-Forwarded-Proto from a configured trusted proxy (see cloud_runtime).
    """
    def __init__(self,app,*,https_only=False):
        self.app=app;self.https_only=https_only
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        extra=SECURITY_HEADERS+((HSTS,) if self.https_only else ())
        async def secure_send(message):
            if message['type']=='http.response.start':
                headers=list(message.get('headers',[]))
                present={name.lower() for name,_ in headers}
                message=dict(message,headers=headers+[item for item in extra if item[0] not in present])
            await send(message)
        if self.https_only and scope.get('scheme')!='https' and scope.get('path') not in PROBES:
            return await JSONResponse({'error':'https_required'},status_code=403)(scope,receive,secure_send)
        await self.app(scope,receive,secure_send)

class AuthenticationRequired(Denied):pass


# Action gate for every authenticated operation, checked right after
# authentication and before the business method runs. Business methods keep their
# finer rules (ownership, visibility, the administrator flag); this table makes
# sure no operation is reachable with an unrelated action, and an operation that
# is missing from it is refused (404), so each new route has to be classified.
# A rule lists alternatives; a credential needs every action of one of them.
ANY_CREDENTIAL=((),)
READ=(('read',),)
ROUTE_ACTIONS={
    ('GET','/enterprise/v1/status'):READ,
    ('GET','/enterprise/v3/service-status'):(('audit',),('settings',)),
    ('GET','/enterprise/v3/auth/credential'):ANY_CREDENTIAL,
    ('GET','/enterprise/v1/processing-status'):(('audit',),),
    ('POST','/enterprise/v1/search'):READ,
    ('POST','/enterprise/v3/search'):READ,
    ('POST','/enterprise/v3/document-evidence'):READ,
    ('POST','/enterprise/v3/document-context'):READ,
    ('POST','/enterprise/v3/temporal-detail'):READ,
    ('POST','/enterprise/v3/timeline'):READ,
    ('POST','/enterprise/v3/project-history'):READ,
    ('POST','/enterprise/v3/project-episode'):READ,
    ('POST','/enterprise/v3/source-documents/describe'):READ,
    ('POST','/enterprise/v3/source-documents/list'):READ,
    ('POST','/enterprise/v3/source-documents/download'):READ,
    ('POST','/enterprise/v3/source-documents/upload'):(('ingest',),),
    ('POST','/enterprise/v3/capture-notice'):ANY_CREDENTIAL,
    # Reading one's own sharing setting is open; changing it needs a capture or
    # policy action (see route_rule).
    ('POST','/enterprise/v3/sharing'):ANY_CREDENTIAL,
    # Owner self-service over the caller's own sources (agenthub.owner_controls).
    # Each business method also checks ownership; raising sharing additionally
    # needs `share`, and granting is refused for delegated credentials.
    ('POST','/enterprise/v3/my-data'):READ,
    ('POST','/enterprise/v3/my-data/forget'):(('withdraw',),),
    ('POST','/enterprise/v3/my-data/sharing'):(('withdraw',),('share',)),
    ('POST','/enterprise/v3/my-data/grants'):READ,
    ('POST','/enterprise/v3/my-data/grant'):(('share',),('policy',)),
    ('POST','/enterprise/v3/my-data/revoke'):(('share',),('withdraw',),('policy',)),
    # Workspace admins hide (withdraw) others' content in their workspace; the
    # business method checks the admin role. Older credentials holding `moderate`
    # keep working; `manage` is the admin role's credential action.
    ('POST','/enterprise/v3/moderation/withdraw'):(('manage',),('moderate',)),
    # Workspaces, access lists, repository links and groups (agenthub.workspace_access).
    # Reading needs `read` and a role (or the settings administrator flag); every
    # change needs `manage` and the admin role, or the settings administrator's
    # audited break-glass override (`settings`).
    ('POST','/enterprise/v3/workspaces'):READ,
    ('POST','/enterprise/v3/workspaces/show'):READ,
    # The session label's advisory descriptor (docs/ACCESS_MODEL.md sections 5 and 7):
    # needs `read` and a role in every workspace it names.
    ('POST','/enterprise/v3/workspaces/descriptor'):READ,
    ('POST','/enterprise/v3/workspaces/create'):(('manage',),),
    ('POST','/enterprise/v3/workspaces/settings'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/workspaces/entries/set'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/workspaces/entries/remove'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/workspaces/repos/link'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/workspaces/repos/unlink'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/groups'):READ,
    ('POST','/enterprise/v3/groups/show'):READ,
    ('POST','/enterprise/v3/groups/create'):(('manage',),),
    ('POST','/enterprise/v3/groups/members/add'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/groups/members/remove'):(('manage',),('settings',)),
    ('POST','/enterprise/v3/groups/delete'):(('manage',),('settings',)),
    # Tenant policy: settings administrators only (the business method checks the flag).
    ('POST','/enterprise/v3/tenant-policy'):(('settings',),),
    ('POST','/enterprise/v3/preferences'):READ,
    ('POST','/enterprise/v3/preferences/curate'):(('curate',),),
    ('POST','/enterprise/v3/preferences/withdraw'):(('withdraw',),),
    ('POST','/enterprise/v3/auth/rotate'):ANY_CREDENTIAL,
    ('POST','/enterprise/v3/auth/adopt'):ANY_CREDENTIAL,
    ('POST','/enterprise/v3/directory/events'):(('settings',),),
    ('POST','/enterprise/v2/parts'):(('ingest',),),
    ('POST','/enterprise/v1/sources'):(('ingest',),),
    ('POST','/enterprise/v1/boundary'):(('ingest',),),
    ('POST','/enterprise/v3/feedback'):(('feedback','read'),),
    ('POST','/enterprise/v1/receipts'):(('ingest','read'),),
    ('POST','/enterprise/v1/reviewed-notes'):(('curate',),),
    ('POST','/enterprise/v2/release'):(('policy','read'),),
}
# Path-parameter routes (the identifier follows the prefix).
PREFIX_ACTIONS={
    ('GET','/enterprise/v1/documents/'):READ,
    ('GET','/enterprise/v1/timeline/'):READ,
    ('GET','/enterprise/v1/sources/'):(('source_read',),),
    ('GET','/enterprise/v2/sources/'):(('source_read',),),
    ('GET','/enterprise/v1/explain/'):(('read',),('settings',)),
    ('POST','/enterprise/v3/access-review/'):(('audit',),('policy',)),
    # PATCH is gated too so a settings administrator learns it is unsupported (400).
    **{(method,'/enterprise/v3/scim/'):(('settings',),) for method in ('GET','POST','PUT','DELETE','PATCH')},
}
# Lifecycle needs the action of the requested operation.
LIFECYCLE_ACTIONS={'correct':'correct','withdraw':'withdraw','delete':'withdraw','policy':'policy'}
# Changing one's own sharing setting needs the action that creates shareable
# content (ingest) or the owner policy action; reading it needs neither.
SHARING_CHANGE_ACTIONS=(('ingest',),('policy',))
# Why each ANY_CREDENTIAL route is open to every authenticated credential. Each
# acts only on the presented credential or returns operator-wide notice text.
SELF_SERVICE={
    ('GET','/enterprise/v3/auth/credential'):'reports only the presented credential (tenant, principal, '
        'enrollment, actions, expiry); every credential needs it to schedule its own renewal',
    ('POST','/enterprise/v3/auth/rotate'):'replaces the presented access token with one that has the same '
        'actions and no later expiry; it cannot widen access',
    ('POST','/enterprise/v3/auth/adopt'):'binds the presented provider token to an enrollment of its own '
        'principal; an enrollment held by another principal is a conflict',
    ('POST','/enterprise/v3/capture-notice'):'returns the capture notice shown before capture starts; '
        'connection reader detail is returned only for a connection the caller owns',
    ('POST','/enterprise/v3/sharing'):'reports the presented principal\'s own contributions_default; changing it '
        'needs ingest or policy, acts only on that principal and is refused for delegated credentials',
}
# Owner self-service and moderation routes and the business method each calls.
OWNER_ROUTES={'/enterprise/v3/my-data':'mine','/enterprise/v3/my-data/forget':'forget',
    '/enterprise/v3/my-data/sharing':'change_sharing','/enterprise/v3/my-data/grants':'grants',
    '/enterprise/v3/my-data/grant':'grant','/enterprise/v3/my-data/revoke':'revoke',
    '/enterprise/v3/moderation/withdraw':'moderate'}
# Workspace, group and tenant-policy routes and the business method each calls.
WORKSPACE_ROUTES={'/enterprise/v3/workspaces':'list_workspaces','/enterprise/v3/workspaces/show':'show_workspace',
    '/enterprise/v3/workspaces/descriptor':'session_descriptor',
    '/enterprise/v3/workspaces/create':'create_workspace','/enterprise/v3/workspaces/settings':'workspace_settings',
    '/enterprise/v3/workspaces/entries/set':'set_entry','/enterprise/v3/workspaces/entries/remove':'remove_entry',
    '/enterprise/v3/workspaces/repos/link':'link_repo','/enterprise/v3/workspaces/repos/unlink':'unlink_repo',
    '/enterprise/v3/groups':'list_groups','/enterprise/v3/groups/show':'show_group',
    '/enterprise/v3/groups/create':'create_group','/enterprise/v3/groups/members/add':'add_group_member',
    '/enterprise/v3/groups/members/remove':'remove_group_member','/enterprise/v3/groups/delete':'delete_group',
    '/enterprise/v3/tenant-policy':'tenant_policy'}
# Routes that carry their own credential in the body (or are a credential
# exchange) and so run before Bearer authentication.
CREDENTIAL_ROUTES=('/enterprise/v3/auth/begin','/enterprise/v3/auth/complete','/enterprise/v3/auth/aws-sts',
    '/enterprise/v3/auth/renew','/enterprise/v3/auth/revoke')
INVALID_BODY=object()
from agenthub.workspace_access import LABELLED_ROUTES  # recall routes that honor a session label
TENANT_PATH=re.compile(r'[A-Za-z0-9_.:-]{1,128}')


def route_rule(method,path,data=None):
    """Required action alternatives for an authenticated route, or None if unknown."""
    if (method,path)==('POST','/enterprise/v1/lifecycle'):
        action=LIFECYCLE_ACTIONS.get(data.get('operation')) if isinstance(data,dict) else None
        if action is None:raise ValueError('invalid_lifecycle_operation')
        return ((action,),)
    if ((method,path)==('POST','/enterprise/v3/sharing') and isinstance(data,dict)
            and ({'max_sharing','contributions_default','apply_to_existing'}&set(data))):
        return SHARING_CHANGE_ACTIONS
    rule=ROUTE_ACTIONS.get((method,path))
    if rule is not None:return rule
    for (allowed,prefix),rule in PREFIX_ACTIONS.items():
        if method==allowed and path.startswith(prefix) and len(path)>len(prefix):return rule
    return None


def authorize_route(ctx,method,path,data=None):
    rule=route_rule(method,path,data)
    if rule is None:raise FileNotFoundError()
    actions=set(ctx['actions'])
    if not any(set(group)<=actions for group in rule):raise Denied()

# Requests that create content a different reader could later receive. Removal
# (withdraw/delete), policy narrowing and reads are never rate limited.
CONTENT_WRITES=('/enterprise/v1/sources','/enterprise/v2/parts','/enterprise/v1/reviewed-notes')


def limits_apply(path,data):
    return path in CONTENT_WRITES or (path=='/enterprise/v1/lifecycle' and isinstance(data,dict)
        and data.get('operation')=='correct')

def pack_search_result(request,result):
    """Pack exact evidence after selection; partial useful evidence stays explicit."""
    output={key:value for key,value in result.items()
        if key in ('results','answerable','support','coverage_gaps')}
    bound=1500 if request.get('mode')=='automatic' else 4000
    selected=[];omitted=False
    for card in output['results']:
        if card.get('evidence_status')=='discovery_only' and len(json.dumps(card,ensure_ascii=True))>1300:
            # A selected original can be read in full through its revision-bound
            # evidence tool. Deliver an exact prefix, never a generated summary.
            card=dict(card,excerpt_truncated=True);omitted=True
            while card.get('lesson') and len(json.dumps(card,ensure_ascii=True))>1300:
                card['lesson']=card['lesson'][:-64]
        if len(selected)>=request.get('limit',8) or len(json.dumps(card,ensure_ascii=True))>1300:
            omitted=True;continue
        trial=dict(output,results=[*selected,card],answerable=False,support='partial',
            coverage_gaps=list(dict.fromkeys([*output.get('coverage_gaps',[]),'delivery_evidence_omitted'])))
        if len(json.dumps(trial,ensure_ascii=True))>bound:omitted=True;continue
        selected.append(card)
    output['results']=selected
    if omitted:
        output['answerable']=False
        output['coverage_gaps']=list(dict.fromkeys([*output.get('coverage_gaps',[]),'delivery_evidence_omitted']))
    if not selected:output['answerable']=False
    if 'support' in output:
        output['support']='complete' if output['answerable'] else ('partial' if selected else 'none')
    if len(json.dumps(output,ensure_ascii=True))>bound:raise ValueError('search_response_bound')
    return output


# Default actions of an interactive enrollment. 'feedback' keeps the outcome
# reporting that 'read' used to imply. 'curate' (reviewed notes, preference
# curation) is privileged and granted explicitly through `enrollment_actions`.
# 'withdraw' and 'share' are the owner's self-service over their own content
# (agenthub.owner_controls); 'manage' creates and administers workspaces, always
# bounded by the role in each workspace (agenthub.workspace_access). 'moderate'
# follows the admin role and is no longer issued by default.
DEFAULT_ENROLLMENT_ACTIONS=('ingest','read','feedback','correct','withdraw','share','manage')
# Unauthenticated /ready reuses one bounded evaluation for this many seconds.
READINESS_TTL_SECONDS=10.0


def create_app(registry,*,allowed_hosts=('127.0.0.1','localhost'),allowed_origins=(),
               document_factory=None,brokers=None,enrollment_actions=None,build_ids=None,timing_sink=None,
               https_only=False,readiness_ttl=READINESS_TTL_SECONDS):
    documents={};brokers=brokers or {}
    readiness={'checked':None,'ready':False};readiness_lock=threading.Lock()
    def document_store(store):
        if document_factory is None:raise ValueError('original_documents_unavailable')
        if store.tenant_id not in documents:documents[store.tenant_id]=document_factory(store)
        return documents[store.tenant_id]
    @timed('authenticate')
    def authenticate(request):
        header=request.headers.get('authorization','')
        if not header.startswith('Bearer '):raise AuthenticationRequired()
        # Server-generated: a client X-Request-ID must never key audit or metering.
        token=header[7:];request_id=uuid.uuid4().hex
        if hasattr(registry,'authenticate_token'):
            try:return registry.authenticate_token(token,request_id)
            except (Denied,IdentityError):raise AuthenticationRequired() from None
        store=registry.store_for_token(token)
        return store,store.authenticate(token,request_id)
    def meter_search(store,ctx,result):
        with span('meter'):
            if not getattr(store,'dsn',None):return
            from agenthub.cloud_ops import Meter
            # One fresh server-side identity per delivered search. Reusing any
            # client-influenced value here would let retries collapse (undercount)
            # or collide across principals (409).
            meter=Meter(store);ident='retrieval:'+uuid.uuid4().hex
            meter.metric(ident,'retrieval_requests',1)
            meter.metric(ident+':cards','delivered_cards',len(result.get('results',[])))
    def provenance(store,path,result,data=None):
        # Added to v3 envelopes. The strict v1 envelopes stay unchanged for HTTP
        # clients; v1 document detail is decorated only for server-side MCP results.
        decorate=getattr(store,'decorate_delivery',None)
        if decorate is None or not getattr(store,'dsn',None):return result
        with span('provenance'):return decorate(path,result,data)
    def deliver(request,method,path,data,*,mcp=False):
        store,ctx=authenticate(request)
        # A malformed body is reported only to an authenticated caller.
        if data is INVALID_BODY:raise ValueError('json_object_required')
        authorize_route(ctx,method,path,data)
        label=None
        if method=='POST' and path in LABELLED_ROUTES and isinstance(data,dict) and 'session_label' in data:
            data=dict(data);label=data.pop('session_label')
            if path=='/enterprise/v3/search':
                # The label decides which workspaces recall reaches (folds in `scope`).
                data.pop('scope',None)
        session={}
        if label is not None:
            from agenthub.workspace_access import validate_session_label
            label=validate_session_label(label)
        def labelled(context):
            # Computed lazily, once per request, inside the request's own transaction on
            # current state (WorkspaceAccessMixin._label); shared after re-authentication.
            if label is None:return context
            return dict(context,session_label_request=label,session_label_cache=session)
        def epoch():
            if 'filter' not in session:session['filter']=store.session_filter(ctx,label)['session_label']
            return session['filter']['epoch']
        ctx=labelled(ctx)
        with span('ready'):store.require_ready()
        if method=='POST' and limits_apply(path,data):
            check_write_limit(store,ctx,len(json.dumps(data,ensure_ascii=False).encode()))
        if (method=='POST' and path in ('/enterprise/v1/search','/enterprise/v3/search')
                and hasattr(store,'validate_search_delivery')):
            # Ranking/embedding uses MVCC without holding up policy writers.
            # Serialize only the final current-identity/card authorization check.
            ranker=getattr(store,'serving_reranker',None)
            result=store.search(ctx,data,candidate_pool=True) if ranker is not None else store.search(ctx,data)
            def checked(value):
                with ExitStack() as stack:
                    with span('delivery_lock_wait'):stack.enter_context(store.delivery_read_lock())
                    with span('delivery_check'):
                        _,current=authenticate(request)
                        return store.validate_search_delivery(labelled(current),data,value)
            result=checked(result)
            if ranker is not None and result.get('results'):
                previous_gaps=result.get('coverage_gaps',[])
                candidate_pool=result.get('_candidate_pool',False)
                with span('rerank'):
                    selected=ranker.rerank(data['query'],result['results'],context={
                        'actor':ctx['actor'],**{k:data[k] for k in ('project','as_of','time_mode') if k in data}})
                if selected.get('reranking',{}).get('status') in {'busy','invalid','unavailable'}:
                    # Operational failure is not evidence that knowledge is absent.
                    # Reuse the canonical deterministic policy as a bounded fallback.
                    result=store.search(ctx,data)
                    result['coverage_gaps']=list(dict.fromkeys([*result.get('coverage_gaps',[]),
                        *selected.get('coverage_gaps',[]),'deterministic_ranking_fallback']))
                else:
                    prior_answerable=result.get('answerable',False)
                    result={key:value for key,value in selected.items()
                        if key in ('results','answerable','support','coverage_gaps')}
                    # Typed historical routing retains its independent validity gate.
                    if not candidate_pool and not prior_answerable:
                        result['answerable']=False
                        result['support']='partial' if result['results'] else 'none'
                    gaps=list(dict.fromkeys([*previous_gaps,*result.get('coverage_gaps',[])]))
                    if gaps:result['coverage_gaps']=gaps
                result=checked(result)
            result.pop('_candidate_pool',None)
            if ranker is not None:result=pack_search_result(data,result)
            if path=='/enterprise/v3/search':result=provenance(store,path,result,data)
            if label is not None:result=dict(result,session_epoch=epoch())
            meter_search(store,ctx,result)
            return result
        with getattr(store,'delivery_read_lock',store.delivery_lock)():
            store,ctx=authenticate(request)
            ctx=labelled(ctx)
            if method=='GET' and path=='/enterprise/v1/status':return store.status(ctx)
            if method=='GET' and path=='/enterprise/v3/service-status':
                # Build identity is operator detail; unauthenticated /health is liveness only.
                if not {'audit','settings'}&set(ctx['actions']):raise Denied()
                return {'service':'Vaelius','models':'idle','build_ids':build_ids}
            if method=='GET' and path=='/enterprise/v3/auth/credential':return store.credential_status(ctx)
            if method=='GET' and path=='/enterprise/v1/processing-status':return store.processing_status(ctx)
            if method=='GET' and path.startswith('/enterprise/v1/documents/'):
                detail=store.detail(ctx,path.rsplit('/',1)[1])
                return provenance(store,path,detail) if mcp else detail
            if method=='POST' and path=='/enterprise/v3/document-evidence':
                if set(data)-{'id','revision','offset'} or not all(isinstance(data.get(k),str) for k in ('id','revision')):
                    raise ValueError('invalid_source_evidence_request')
                return store.document_evidence(ctx,data['id'],revision=data['revision'],offset=data.get('offset',0))
            if method=='POST' and path=='/enterprise/v3/document-context':
                if set(data)-{'id','revision','query','offset'}:raise ValueError('context_request')
                from agenthub.source_context import document_context
                try:
                    return document_context(store,ctx,data.get('id'),revision=data.get('revision'),
                        query=data.get('query',''),offset=data.get('offset',0))
                except PermissionError:raise Denied()
            if method=='POST' and path=='/enterprise/v3/temporal-detail':
                if set(data)-{'id','as_of','time_mode'} or not isinstance(data.get('id'),str):
                    raise ValueError('invalid_temporal_detail')
                return provenance(store,path,store.detail(ctx,data['id'],as_of=data.get('as_of'),time_mode=data.get('time_mode')))
            if method=='GET' and path.startswith('/enterprise/v1/timeline/'):return store.timeline(ctx,path.rsplit('/',1)[1])
            if method=='GET' and path.startswith('/enterprise/v1/sources/'):return store.source(ctx,path.rsplit('/',1)[1])
            if method=='GET' and path.startswith('/enterprise/v2/sources/'):return store.general_source(ctx,path.rsplit('/',1)[1])
            if method=='GET' and path.startswith('/enterprise/v1/explain/'):return store.explain(ctx,path.rsplit('/',1)[1])
            if method=='POST' and path in ('/enterprise/v1/search','/enterprise/v3/search'):
                result=store.search(ctx,data)
                if path=='/enterprise/v3/search':result=provenance(store,path,result,data)
                if label is not None:result=dict(result,session_epoch=epoch())
                meter_search(store,ctx,result);return result
            if method=='POST' and path=='/enterprise/v3/timeline':
                if set(data)-{'id','offset','limit','cursor'} or not isinstance(data.get('id'),str):
                    raise ValueError('invalid_timeline_request')
                if 'cursor' in data and 'offset' in data:raise ValueError('timeline_cursor_request')
                return provenance(store,path,store.timeline(ctx,data['id'],offset=data.get('offset',0),
                    limit=data.get('limit',3),cursor=data.get('cursor'),
                    cursor_mode=('offset' not in data)))
            if method=='POST' and path=='/enterprise/v3/project-history':
                if set(data)-{'project','cursor','limit'} or not isinstance(data.get('project'),str):
                    raise ValueError('invalid_project_history_request')
                return store.project_history(ctx,data['project'],cursor=data.get('cursor'),
                    limit=data.get('limit',8))
            if method=='POST' and path=='/enterprise/v3/project-episode':
                if set(data)-{'project','episode_id','offset','limit','text_offset'} or not all(
                        isinstance(data.get(key),str) for key in ('project','episode_id')):
                    raise ValueError('invalid_project_episode_request')
                return store.project_episode(ctx,data['project'],data['episode_id'],
                    offset=data.get('offset',0),limit=data.get('limit',8),
                    text_offset=data.get('text_offset',0))
            if method=='POST' and path=='/enterprise/v3/source-documents/describe':
                return document_store(store).describe(ctx,data['source_id'],version=data.get('version'))
            if method=='POST' and path=='/enterprise/v3/source-documents/list':
                return {'documents':document_store(store).list(ctx,title=data.get('title',''),limit=data.get('limit',20))}
            if method=='POST' and path=='/enterprise/v3/capture-notice' and hasattr(store,'capture_notice'):
                return store.capture_notice(ctx,data)
            if method=='POST' and path=='/enterprise/v3/preferences':
                return store.preferences(ctx,project=data['project'],task=data.get('task'),explicit=data.get('explicit'),
                    session=data.get('session'),mode=data.get('mode','explicit'))
        # These business methods own their policy/write transaction boundary.
        if method=='POST' and path=='/enterprise/v3/sharing':return store.sharing_setting(ctx,data)
        if method=='POST' and path in OWNER_ROUTES:return getattr(store,OWNER_ROUTES[path])(ctx,data)
        if method=='POST' and path in WORKSPACE_ROUTES:return getattr(store,WORKSPACE_ROUTES[path])(ctx,data)
        if method=='POST' and path=='/enterprise/v3/preferences/curate':
            return store.curate_preference(ctx,data['source_id'],data['candidate'],replace_document=data.get('replace_document'))
        if method=='POST' and path=='/enterprise/v3/preferences/withdraw':return store.withdraw_preference(ctx,data['document_id'])
        if method=='POST' and path=='/enterprise/v3/auth/rotate':return store.rotate_credential(ctx)
        if method=='POST' and path=='/enterprise/v3/auth/adopt':
            if set(data)!={'enrollment'}:raise ValueError('invalid_enrollment')
            from agenthub.oauth_provider import adopt_enrollment
            return adopt_enrollment(store,ctx,data['enrollment'])
        if method=='POST' and path=='/enterprise/v3/directory/events':
            return store.apply_directory_event(ctx,resource=data['resource'],value=data['value'],sequence=data['sequence'],
                key=data['key'],reconcile=data.get('reconcile',False),deleted=data.get('deleted',False))
        if method=='POST' and path=='/enterprise/v2/parts':return store.ingest_part(ctx,data)
        if method=='POST' and path=='/enterprise/v1/sources':return store.ingest(ctx,data)
        if method=='POST' and path=='/enterprise/v1/lifecycle':return store.lifecycle(ctx,data)
        if method=='POST' and path=='/enterprise/v1/boundary':return store.boundary(ctx,data)
        if method=='POST' and path=='/enterprise/v3/feedback':
            from agenthub.feedback import record_feedback
            return record_feedback(store,ctx,data)
        if method=='POST' and path=='/enterprise/v1/receipts':return store.receipt(ctx,data)
        if method=='POST' and path=='/enterprise/v1/reviewed-notes':return store.accept_reviewed_note(ctx,data['source_id'],data['title'],data['lesson'])
        if method=='POST' and path=='/enterprise/v2/release':
            return store.reviewed_release(ctx,data['document_id'],data['expected_revision'],data['project'],data['readers'],data['key'])
        if path.startswith('/enterprise/v3/access-review/'):
            # Tenant access review; takes the exclusive policy lock for decisions.
            from agenthub.access_review import route
            return route(store,ctx,method,path,data)
        raise FileNotFoundError()
    def evaluate_readiness():
        if not hasattr(registry,'open_control'):return
        with registry.open_control() as db:
            tenants=[r[0] for r in db.execute('SELECT id FROM cloud_tenants WHERE active=1 ORDER BY id LIMIT ?', (registry.max_stores+1,))]
        if len(tenants)>registry.max_stores:raise ValueError('readiness_tenant_bound')
        for tenant in tenants:registry.resolve(tenant).require_ready()
    def ready():
        """Unauthenticated readiness: at most one tenant sweep per interval.

        Concurrent probes do not queue behind a running sweep; they get the last
        result. The response carries no per-tenant detail.
        """
        now=time.monotonic()
        if readiness['checked'] is not None and now-readiness['checked']<readiness_ttl:return readiness['ready']
        if not readiness_lock.acquire(blocking=False):return readiness['ready']
        try:
            if readiness['checked'] is not None and time.monotonic()-readiness['checked']<readiness_ttl:
                return readiness['ready']
            try:evaluate_readiness();ok=True
            except Exception:ok=False
            readiness.update(checked=time.monotonic(),ready=ok)
            return ok
        finally:readiness_lock.release()
    async def bounded_body(request,limit):
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>limit:raise OverflowError()
            body.extend(chunk)
        return body
    async def dispatch(request):
        origin=request.headers.get('origin')
        own=str(request.base_url).rstrip('/')
        if origin is not None and origin not in (*allowed_origins,own):return JSONResponse({'error':'forbidden'},status_code=403)
        # Dispatch on the ASGI path the router matched. request.url is rebuilt
        # from the Host header, so it must not choose the operation.
        path=request.scope['path']
        if request.scope.get('query_string'):return JSONResponse({'error':'unsupported_query'},status_code=400)
        try:
            if path=='/health':return JSONResponse({'service':'Vaelius','models':'idle'})
            if path.startswith('/.well-known/oauth-protected-resource') and hasattr(registry,'protected_metadata'):
                prefix='/.well-known/oauth-protected-resource'
                suffix=path.removeprefix(prefix)
                if suffix not in ('','/mcp') and not suffix.startswith('/mcp/'):raise FileNotFoundError()
                # Every well-formed tenant path gets the same resource-wide document,
                # so this unauthenticated route does not reveal which tenants exist.
                # Clients learn their tenant and issuer from their enrollment details.
                if suffix.startswith('/mcp/') and not TENANT_PATH.fullmatch(suffix.removeprefix('/mcp/')):
                    raise FileNotFoundError()
                return JSONResponse(registry.protected_metadata(),headers=NO_STORE)
            if path=='/ready':
                ok=await run_in_threadpool(ready)
                return JSONResponse({'ready':ok,'provider_dispatch':'off'},status_code=200 if ok else 503)
            if path=='/enterprise/v3/source-documents/upload':
                store,ctx=await run_in_threadpool(authenticate,request)
                authorize_route(ctx,request.method,path)
                metadata=request.headers.get('x-document-metadata','')
                if len(metadata.encode())>4096:raise ValueError('metadata_bound')
                data=json.loads(metadata)
                if set(data)-{'connection','external_id','version','filename','title','media_type','sharing'}:raise ValueError('upload_metadata')
                with tempfile.SpooledTemporaryFile(max_size=1024*1024) as temporary:
                    size=0
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>FILE_LIMIT:raise OverflowError()
                        temporary.write(chunk)
                    temporary.seek(0)
                    await run_in_threadpool(check_write_limit,store,ctx,size)
                    value=await run_in_threadpool(document_store(store).ingest,ctx,data['connection'],data['external_id'],
                        data['version'],data['filename'],temporary,title=data.get('title'),media_type=data.get('media_type'),
                        sharing=data.get('sharing'))
                return JSONResponse(value)
            data=None
            if request.method=='POST':
                raw=await bounded_body(request,JSON_LIMIT)
                try:
                    data=json.loads(raw)
                    if not isinstance(data,dict):raise ValueError('json_object_required')
                except ValueError:
                    # Only credential routes answer a malformed body before authentication.
                    if path in CREDENTIAL_ROUTES:raise
                    data=INVALID_BODY
            if path in ('/enterprise/v3/auth/begin','/enterprise/v3/auth/complete'):
                if hasattr(registry,'legacy_interactive') and not registry.legacy_interactive:raise Denied()
                # Broker and field checks depend only on configuration and the request,
                # never on the tenant; the tenant itself is consulted only through the
                # registry's control-plane login requests (no tenant enumeration).
                broker=brokers[data['broker']]
                if path=='/enterprise/v3/auth/begin':
                    return JSONResponse(await run_in_threadpool(registry.begin_login,data['tenant'],data['broker'],broker),
                        headers=NO_STORE)
                values={key:data[key] for key in ('state','code','callback_uri','enrollment')}
                issued=await run_in_threadpool(lambda:registry.complete_login(data['tenant'],data['broker'],broker,
                    **values,actions=list(enrollment_actions or DEFAULT_ENROLLMENT_ACTIONS)))
                # `credential` keeps its meaning (the access token) for older clients.
                return JSONResponse({'credential':issued['access_token'],
                    **{key:issued[key] for key in ('expires_at','refresh_token','refresh_expires_at','session_expires_at')}},
                    headers=NO_STORE)
            if path=='/enterprise/v3/auth/aws-sts':
                # A signed sts:GetCallerIdentity request is the only credential
                # accepted here. Absent configuration this route does not exist.
                if getattr(registry,'aws_sts',None) is None:raise FileNotFoundError()
                request_id=request.headers.get('x-request-id') or uuid.uuid4().hex
                return JSONResponse(await run_in_threadpool(registry.exchange_aws_sts,data,request_id),headers=NO_STORE)
            if path=='/enterprise/v3/auth/renew':
                # The refresh token is the only credential accepted here (RFC 6749
                # section 6); a Bearer access token is neither required nor used.
                if set(data)!={'refresh_token','replacement_access_token','replacement_refresh_token'}:
                    raise ValueError('invalid_credential_renewal')
                request_id=uuid.uuid4().hex
                def renew():
                    store=registry.store_for_token(data['refresh_token'])
                    return store.refresh_credential(data['refresh_token'],
                        replacement_access_token=data['replacement_access_token'],
                        replacement_refresh_token=data['replacement_refresh_token'],request_id=request_id)
                return JSONResponse(await run_in_threadpool(renew),headers=NO_STORE)
            if path=='/enterprise/v3/auth/revoke':
                # RFC 7009: the body `token` (refresh or access) or the Bearer access
                # token identifies the family. Unknown tokens are acknowledged too.
                if set(data)-{'token','token_type_hint'}:raise ValueError('invalid_revocation_request')
                header=request.headers.get('authorization','')
                token=data.get('token',header[7:] if header.startswith('Bearer ') else None)
                if not isinstance(token,str) or not 1<=len(token)<=16384:raise ValueError('invalid_revocation_request')
                request_id=uuid.uuid4().hex
                def revoke():
                    # One answer for valid, invalid, expired and already revoked tokens
                    # (RFC 7009 section 2.2), so revocation is not a validity oracle.
                    try:registry.store_for_token(token).revoke_credential_family(token,request_id=request_id)
                    except (Denied,IdentityError,ValueError):pass
                    return {'revoked':True}
                return JSONResponse(await run_in_threadpool(revoke),headers=NO_STORE)
            if path.startswith('/enterprise/v3/scim/'):
                store,ctx=await run_in_threadpool(authenticate,request)
                authorize_route(ctx,request.method,path)
                parts=path.removeprefix('/enterprise/v3/scim/').split('/')
                if len(parts)!=2 or parts[0] not in ('Users','Groups'):raise ValueError('unsupported_scim_operation')
                if data is INVALID_BODY:raise ValueError('json_object_required')
                resource,external_id=parts
                if request.method=='GET':
                    def read_scim():
                        with getattr(store,'delivery_read_lock',store.delivery_lock)():
                            _,current=authenticate(request)
                            return store.scim_resource(current,resource,external_id)
                    return JSONResponse(await run_in_threadpool(read_scim))
                if request.method not in ('POST','PUT','DELETE'):raise ValueError('unsupported_scim_operation')
                if request.method=='PUT':
                    raw=await bounded_body(request,JSON_LIMIT);data=json.loads(raw)
                if request.method=='DELETE':data={'id':external_id}
                if not isinstance(data,dict) or data.get('id')!=external_id:raise ValueError('scim_resource_id_mismatch')
                sequence=int(request.headers['x-directory-sequence']);key=request.headers['x-idempotency-key']
                value=await run_in_threadpool(store.apply_directory_event,ctx,resource=resource,value=data,
                    sequence=sequence,key=key,deleted=request.method=='DELETE')
                return JSONResponse(value)
            if path=='/enterprise/v3/source-documents/download':
                store,ctx=await run_in_threadpool(authenticate,request)
                if data is INVALID_BODY:raise ValueError('json_object_required')
                authorize_route(ctx,request.method,path)
                source=data['source_id'];version=data.get('version');service=document_store(store)
                file,headers=await run_in_threadpool(service.fetch,ctx,source,version=version)
                def content():
                    try:
                        # Defined delivery authorization check point, immediately before first byte.
                        with getattr(store,'delivery_read_lock',store.delivery_lock)():
                            _,current=authenticate(request);service.authorize_original(current,source,version=version)
                        while True:
                            chunk=file.read(65536)
                            if not chunk:break
                            yield chunk
                    finally:file.close()
                return StreamingResponse(content(),media_type='application/octet-stream',headers=headers)
            value=await run_in_threadpool(deliver,request,request.method,path,data)
            return JSONResponse(value,headers=NO_STORE)
        except RateLimited as limited:
            return JSONResponse({'error':'rate_limited','scope':limited.scope},status_code=429,
                headers=NO_STORE|{'Retry-After':str(limited.retry_after)})
        except (OverflowError,WriteTooLarge):return JSONResponse({'error':'body_size'},status_code=413)
        except AuthenticationRequired:
            if hasattr(registry,'protected_metadata'):
                return JSONResponse({'error':'authentication_required'},status_code=401,
                    headers=NO_STORE|{'WWW-Authenticate':challenge})
            return JSONResponse({'error':'not_found'},status_code=404)
        except (Denied,IdentityError,FileNotFoundError):return JSONResponse({'error':'not_found'},status_code=404)
        except RepoNotLinked:return JSONResponse({'error':'repo_not_linked'},status_code=409)
        except Conflict:return JSONResponse({'error':'conflict'},status_code=409)
        except (ValueError,TypeError,KeyError):return JSONResponse({'error':'invalid_request'},status_code=400)
        except Exception:return JSONResponse({'error':'service_error'},status_code=503)
    async def timed_dispatch(request):
        if request.method!='POST' or request.scope['path'] not in ('/enterprise/v1/search','/enterprise/v3/search'):
            return await dispatch(request)
        with capture() as trace:
            status=503
            try:
                response=await dispatch(request);status=response.status_code
                if status==200:response.headers['X-AgentHub-Request-ID']=trace.request_id
                return response
            finally:
                # Diagnostics must neither fail delivery nor reveal sink errors.
                try:(timing_sink or emit)(trace.record(status))
                except Exception:pass
    from agenthub.mcp_server import build_mcp
    challenge=None
    if hasattr(registry,'protected_metadata'):
        origin=registry.resource.removesuffix('/mcp').rstrip('/')
        challenge='Bearer resource_metadata="'+origin+'/.well-known/oauth-protected-resource/mcp"'
    mcp,lifespan=build_mcp(authenticate,deliver,allowed_hosts=allowed_hosts,allowed_origins=allowed_origins,challenge=challenge)
    app=Starlette(routes=[Route('/mcp',mcp,methods=['GET','POST','DELETE']),
        Route('/{path:path}',timed_dispatch,methods=['GET','POST','PUT','DELETE','PATCH'])],lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=list(allowed_hosts))
    app.add_middleware(SecureTransport,https_only=https_only)
    return app
