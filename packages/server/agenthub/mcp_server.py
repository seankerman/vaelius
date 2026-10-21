"""Authenticated Streamable HTTP MCP over the canonical service operations."""
import ipaddress
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from agenthub.enterprise import Denied
from agenthub.cloud_identity import IdentityError
from agenthub.mcp_tools import CONTEXT_READ_INSTRUCTIONS, MemoryTools, enterprise_tools


def transport_security(allowed_hosts, allowed_origins):
    """SDK DNS-rebinding protection from the operator's host/origin allowlist.

    The SDK compares the raw Host header (with port), so each exact name is also
    allowed with any port, matching Starlette's TrustedHostMiddleware. Wildcards
    cannot be expressed exactly and an empty list would admit nothing, so both
    are configuration errors (fail closed) rather than a disabled check.
    """
    hosts=[];origins=list(allowed_origins)
    for host in allowed_hosts:
        if not isinstance(host,str) or not host or any(c in host for c in '*/[] '):
            raise ValueError('mcp_allowed_hosts_require_exact_names')
        try:name='['+host+']' if ipaddress.ip_address(host).version==6 else host
        except ValueError:name=host
        hosts+=[name,name+':*']
        origins+=[scheme+'://'+name+port for scheme in ('https','http') for port in ('',':*')]
    if not hosts:raise ValueError('mcp_allowed_hosts_required')
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,allowed_origins=list(dict.fromkeys(origins)))


def build_mcp(authenticate, deliver, *, allowed_hosts, allowed_origins,challenge=None):
    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool(**item) for item in enterprise_tools()])

    async def call_tool(context, params):
        try:
            request=context.request
            arguments=dict(params.arguments or {})
            project=arguments.get('project') or request.headers.get('x-agentnetwork-project')
            if project is not None and (not isinstance(project,str) or not 1<=len(project)<=128):
                raise ValueError('invalid_project')
            session=request.headers.get('x-agentnetwork-session')
            if session is not None and (not 1<=len(session)<=128):raise ValueError('invalid_session')
            def invoke():
                def operation(path, value=None):
                    # Each call resolves current identity through the same REST
                    # operation boundary; no token or permission result is cached.
                    return deliver(request, 'GET' if value is None else 'POST', path, value)
                tools=MemoryTools(project,SimpleNamespace(request=operation),session=session)
                return tools.call(params.name,arguments)
            result=await run_in_threadpool(invoke)
            return types.CallToolResult(content=[types.TextContent(type='text',text=json.dumps(result,ensure_ascii=True))])
        except Exception:
            return types.CallToolResult(isError=True,content=[types.TextContent(type='text',
                text='Memory request unavailable or invalid. Check identity, project and revision; reuse the same request_key for identical writes.')])

    server=Server('vaelius-memory',version='0.3.0',instructions=CONTEXT_READ_INSTRUCTIONS,
        on_list_tools=list_tools,on_call_tool=call_tool)
    # Defense in depth: the SDK re-checks Host/Origin even if this endpoint is
    # mounted without TrustedHostMiddleware; guarded() keeps the exact own-origin rule.
    security=transport_security(allowed_hosts,allowed_origins)
    application=server.streamable_http_app(stateless_http=True,json_response=True,
        max_request_body_size=65536,transport_security=security)

    async def guarded(scope, receive, send):
        request=Request(scope,receive)
        own=str(request.base_url).rstrip('/')
        origin=request.headers.get('origin')
        if origin is not None and origin not in (*allowed_origins,own):
            return await JSONResponse({'error':'forbidden'},status_code=403)(scope,receive,send)
        if scope.get('query_string'):
            return await JSONResponse({'error':'unsupported_query'},status_code=400)(scope,receive,send)
        try:
            store,_=await run_in_threadpool(authenticate,request)
            await run_in_threadpool(store.require_ready)
        except (Denied,IdentityError):
            return await JSONResponse({'error':'authentication_required'},status_code=401,
                headers={'WWW-Authenticate':challenge or 'Bearer realm="Vaelius"','Cache-Control':'no-store'})(scope,receive,send)
        except Exception:
            return await JSONResponse({'error':'service_unavailable'},status_code=503)(scope,receive,send)
        async def private_send(message):
            if message['type']=='http.response.start':
                message=dict(message,headers=[*message.get('headers',[]),(b'cache-control',b'no-store')])
            await send(message)
        await application(scope,receive,private_send)

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield
    class Endpoint:
        async def __call__(self,scope,receive,send):
            await guarded(scope,receive,send)
    return Endpoint(),lifespan
