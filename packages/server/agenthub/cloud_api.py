"""Portable ASGI boundary around the canonical enterprise facade.

Default listener/proxy configuration is explicit. Startup is provider-free; errors
and access logs contain no source body. Authenticate again at delivery checkpoint.
"""
import json
import tempfile
import uuid
from contextlib import ExitStack

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from agenthub.enterprise import Denied, Conflict
from agenthub.cloud_identity import IdentityError
from agenthub.request_timing import capture, span, timed, emit

JSON_LIMIT=262144
FILE_LIMIT=50*1024*1024

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


def create_app(registry,*,allowed_hosts=('127.0.0.1','localhost'),allowed_origins=(),
               document_factory=None,brokers=None,enrollment_actions=None,build_ids=None,timing_sink=None):
    documents={};brokers=brokers or {}
    def document_store(store):
        if document_factory is None:raise ValueError('original_documents_unavailable')
        if store.tenant_id not in documents:documents[store.tenant_id]=document_factory(store)
        return documents[store.tenant_id]
    @timed('authenticate')
    def authenticate(request):
        header=request.headers.get('authorization','')
        if not header.startswith('Bearer '):raise Denied()
        token=header[7:];store=registry.store_for_token(token)
        return store,store.authenticate(token,request.headers.get('x-request-id') or uuid.uuid4().hex)
    def meter_search(store,ctx,result):
        with span('meter'):
            if not getattr(store,'dsn',None):return
            from agenthub.cloud_ops import Meter
            meter=Meter(store);ident='retrieval:'+ctx['request_id']
            meter.metric(ident,'retrieval_requests',1)
            meter.metric(ident+':cards','delivered_cards',len(result.get('results',[])))
    def deliver(request,method,path,data):
        store,ctx=authenticate(request)
        with span('ready'):store.require_ready()
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
                        return store.validate_search_delivery(current,data,value)
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
            meter_search(store,ctx,result)
            return result
        with getattr(store,'delivery_read_lock',store.delivery_lock)():
            store,ctx=authenticate(request)
            if method=='GET' and path=='/enterprise/v1/status':return store.status(ctx)
            if method=='GET' and path=='/enterprise/v3/auth/credential':return store.credential_status(ctx)
            if method=='GET' and path=='/enterprise/v1/processing-status':return store.processing_status(ctx)
            if method=='GET' and path.startswith('/enterprise/v1/documents/'):return store.detail(ctx,path.rsplit('/',1)[1])
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
                return store.detail(ctx,data['id'],as_of=data.get('as_of'),time_mode=data.get('time_mode'))
            if method=='GET' and path.startswith('/enterprise/v1/timeline/'):return store.timeline(ctx,path.rsplit('/',1)[1])
            if method=='GET' and path.startswith('/enterprise/v1/sources/'):return store.source(ctx,path.rsplit('/',1)[1])
            if method=='GET' and path.startswith('/enterprise/v2/sources/'):return store.general_source(ctx,path.rsplit('/',1)[1])
            if method=='GET' and path.startswith('/enterprise/v1/explain/'):return store.explain(ctx,path.rsplit('/',1)[1])
            if method=='POST' and path in ('/enterprise/v1/search','/enterprise/v3/search'):
                result=store.search(ctx,data);meter_search(store,ctx,result);return result
            if method=='POST' and path=='/enterprise/v3/timeline':
                if set(data)-{'id','offset','limit','cursor'} or not isinstance(data.get('id'),str):
                    raise ValueError('invalid_timeline_request')
                if 'cursor' in data and 'offset' in data:raise ValueError('timeline_cursor_request')
                return store.timeline(ctx,data['id'],offset=data.get('offset',0),
                    limit=data.get('limit',3),cursor=data.get('cursor'),
                    cursor_mode=('offset' not in data))
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
            if method=='POST' and path=='/enterprise/v3/preferences':
                return store.preferences(ctx,project=data['project'],task=data.get('task'),explicit=data.get('explicit'),
                    session=data.get('session'),mode=data.get('mode','explicit'))
        # These business methods own their policy/write transaction boundary.
        if method=='POST' and path=='/enterprise/v3/preferences/curate':
            return store.curate_preference(ctx,data['source_id'],data['candidate'],replace_document=data.get('replace_document'))
        if method=='POST' and path=='/enterprise/v3/preferences/withdraw':return store.withdraw_preference(ctx,data['document_id'])
        if method=='POST' and path=='/enterprise/v3/auth/rotate':return store.rotate_credential(ctx)
        if method=='POST' and path=='/enterprise/v3/auth/renew':
            if set(data)!={'replacement_token'}:raise ValueError('invalid_credential_renewal')
            token=store.rotate_credential(ctx,replacement_token=data['replacement_token'])
            return store.credential_status(store.authenticate(token))
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
        raise FileNotFoundError()
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
        path=request.url.path
        if request.url.query:return JSONResponse({'error':'unsupported_query'},status_code=400)
        try:
            if path=='/health':return JSONResponse({'service':'Vaelius','models':'idle',**({'build_ids':build_ids} if build_ids else {})})
            if path=='/ready':
                def ready():
                    if not hasattr(registry,'open_control'):return
                    with registry.open_control() as db:
                        tenants=[r[0] for r in db.execute('SELECT id FROM cloud_tenants WHERE active=1 ORDER BY id LIMIT ?', (registry.max_stores+1,))]
                    if len(tenants)>registry.max_stores:raise ValueError('readiness_tenant_bound')
                    for tenant in tenants:registry.resolve(tenant).require_ready()
                try:await run_in_threadpool(ready)
                except Exception:return JSONResponse({'ready':False,'provider_dispatch':'off'},status_code=503)
                return JSONResponse({'ready':True,'provider_dispatch':'off'})
            if path=='/enterprise/v3/source-documents/upload':
                store,ctx=await run_in_threadpool(authenticate,request)
                metadata=request.headers.get('x-document-metadata','')
                if len(metadata.encode())>4096:raise ValueError('metadata_bound')
                data=json.loads(metadata)
                if set(data)-{'connection','external_id','version','filename','title','media_type'}:raise ValueError('upload_metadata')
                with tempfile.SpooledTemporaryFile(max_size=1024*1024) as temporary:
                    size=0
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>FILE_LIMIT:raise OverflowError()
                        temporary.write(chunk)
                    temporary.seek(0)
                    value=await run_in_threadpool(document_store(store).ingest,ctx,data['connection'],data['external_id'],
                        data['version'],data['filename'],temporary,title=data.get('title'),media_type=data.get('media_type'))
                return JSONResponse(value)
            data=None
            if request.method=='POST':
                raw=await bounded_body(request,JSON_LIMIT);data=json.loads(raw)
                if not isinstance(data,dict):raise ValueError('json_object_required')
            if path=='/enterprise/v3/auth/begin':
                store=registry.resolve(data['tenant']);broker=brokers[data['broker']]
                return JSONResponse(await run_in_threadpool(store.begin_enrollment,broker))
            if path=='/enterprise/v3/auth/complete':
                store=registry.resolve(data['tenant']);broker=brokers[data['broker']]
                token=await run_in_threadpool(store.complete_enrollment,broker,state=data['state'],code=data['code'],
                    callback_uri=data['callback_uri'],enrollment=data['enrollment'],
                    actions=enrollment_actions or ['ingest','read','source_read','correct','withdraw'])
                return JSONResponse({'credential':token})
            if path.startswith('/enterprise/v3/scim/'):
                parts=path.removeprefix('/enterprise/v3/scim/').split('/')
                if len(parts)!=2 or parts[0] not in ('Users','Groups'):raise ValueError('unsupported_scim_operation')
                store,ctx=await run_in_threadpool(authenticate,request)
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
                source=data['source_id'];version=data.get('version');service=document_store(store)
                file,headers=await run_in_threadpool(service.fetch,ctx,source,version=version)
                def content():
                    try:
                        # Defined delivery authorization check point, immediately before first byte.
                        with getattr(store,'delivery_read_lock',store.delivery_lock)():
                            _,current=authenticate(request);service.describe(current,source,version=version)
                        while True:
                            chunk=file.read(65536)
                            if not chunk:break
                            yield chunk
                    finally:file.close()
                return StreamingResponse(content(),media_type='application/octet-stream',headers=headers)
            value=await run_in_threadpool(deliver,request,request.method,path,data)
            return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except OverflowError:return JSONResponse({'error':'body_size'},status_code=413)
        except (Denied,IdentityError,FileNotFoundError):return JSONResponse({'error':'not_found'},status_code=404)
        except Conflict:return JSONResponse({'error':'conflict'},status_code=409)
        except (ValueError,TypeError,KeyError):return JSONResponse({'error':'invalid_request'},status_code=400)
        except Exception:return JSONResponse({'error':'service_error'},status_code=503)
    async def timed_dispatch(request):
        if request.method!='POST' or request.url.path not in ('/enterprise/v1/search','/enterprise/v3/search'):
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
    mcp,lifespan=build_mcp(authenticate,deliver,allowed_hosts=allowed_hosts,allowed_origins=allowed_origins)
    app=Starlette(routes=[Route('/mcp',mcp,methods=['GET','POST','DELETE']),
        Route('/{path:path}',timed_dispatch,methods=['GET','POST','PUT','DELETE','PATCH'])],lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=list(allowed_hosts))
    return app
