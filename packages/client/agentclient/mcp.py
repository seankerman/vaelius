"""Optional stdio transport bridge; tools and their behavior live in AgentHub.

New installations connect directly over HTTP. This bridge supports existing hosts
and diagnostic runners that require stdio without carrying a second tool catalogue.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request
import uuid


class MemoryTools:
    def __init__(self,home,project):
        self.home=Path(home);self.project=project;self.session='mcp-'+uuid.uuid4().hex

    def config(self):
        from agentclient.transport import require_backend
        value=json.loads((self.home/'config.json').read_text());require_backend(value)
        if self.project not in set(value.get('projects',{}).values())|set(value.get('sessions',{}).values()):
            raise ValueError('enterprise_project_not_enrolled')
        return value

    def rpc(self,value):
        from agentclient.transport import enterprise_client
        backend=enterprise_client(self.config(),timeout=60)
        request=urllib.request.Request(backend.url+'/mcp',data=json.dumps(value).encode(),headers={
            'Authorization':'Bearer '+backend.token,'Content-Type':'application/json',
            'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25',
            'X-AgentNetwork-Project':self.project,'X-AgentNetwork-Session':self.session})
        with backend.opener.open(request,timeout=60) as response:
            raw=response.read(262145)
        if len(raw)>262144:raise ValueError('mcp_response_bound')
        return json.loads(raw) if raw else None

    def call(self,name,args):
        value=self.rpc({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
        result=value.get('result',{})
        if value.get('error') or result.get('isError'):raise ValueError('memory_request_unavailable')
        return json.loads(result['content'][0]['text'])


def serve(memory,incoming=sys.stdin.buffer,outgoing=sys.stdout):
    for raw in iter(lambda:incoming.readline(65537),b''):
        if len(raw)>65536:return
        ident=None
        try:
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError('invalid_request')
            ident=value.get('id');result=memory.rpc(value)
        except Exception:
            result={'jsonrpc':'2.0','id':ident,'error':{'code':-32603,'message':'Memory service unavailable or invalid request'}}
        if ident is not None and result is not None:
            outgoing.write(json.dumps(result)+'\n');outgoing.flush()


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--home',required=True);parser.add_argument('--project')
    args=parser.parse_args()
    if args.project is None:
        from agentclient.hooks import binding
        cfg=json.loads((Path(args.home)/'config.json').read_text())
        args.project=binding(cfg,{'cwd':os.getcwd()})
        if not args.project:raise ValueError('MCP project must be explicitly enrolled')
    serve(MemoryTools(args.home,args.project))


if __name__=='__main__':main()
