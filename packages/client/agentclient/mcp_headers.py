"""Local credential/context helper for a direct backend HTTP MCP connection.

Hosts run it as their header helper: Codex `http_headers_helper`, Claude Code
`headersHelper`. Its stdout is one JSON object of headers for that pipe only.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid


def headers(home, *, project=None, project_dir=None, url=None):
    """Bearer and project headers, or an exception; never a partial result.

    `url` is the MCP URL the host is about to call (Claude Code passes it as
    CLAUDE_CODE_MCP_SERVER_URL). The token is only released for this profile's
    own service, so a mistyped or substituted URL cannot collect it.
    """
    from agentclient.transport import enterprise_client, require_backend, backend_endpoint
    from agentclient.hooks import resolve_scope
    from agentclient.scope_file import enrollments
    config=json.loads((Path(home)/'config.json').read_text())
    if url is not None and url.rstrip('/')!=backend_endpoint(require_backend(config))+'/mcp':
        raise ValueError('mcp_url_not_this_profile_service')
    backend=enterprise_client(config)
    enrolled=set(config.get('projects',{}).values())|set(config.get('sessions',{}).values())
    if project:
        selected=project
        enrolled|={value.get('project') for value in enrollments(home).values() if isinstance(value,dict)}
    else:
        # The folder's project as hooks resolve it; a read-only scope file still recalls.
        scope=resolve_scope(config,{'cwd':project_dir or os.getcwd()})
        selected=scope['project'] if scope and scope['capture'] in ('on','read-only') else None
        if scope and scope['origin']=='scope_file':enrolled.add(scope['project'])
    if selected is not None and selected not in enrolled:
        raise ValueError('project_not_enrolled')
    result={'Authorization':'Bearer '+backend.token,'X-AgentNetwork-Session':'mcp-'+uuid.uuid4().hex}
    if selected:result['X-AgentNetwork-Project']=selected
    return result


def failure(error):
    """A content-free reason for the host's MCP error display (stderr only)."""
    from agentclient.hooks import service_problem
    if isinstance(error,ValueError) and str(error) in {'mcp_url_not_this_profile_service','project_not_enrolled'}:
        return 'Vaelius MCP refused: '+str(error)
    if isinstance(error,FileNotFoundError):
        return 'Vaelius MCP unavailable: no client profile on this computer'
    problem=service_problem(error)
    if problem=='signed_out':
        return 'Vaelius signed out: sign in again to use memory tools (capture keeps queuing locally)'
    return 'Vaelius credential/context unavailable ('+problem+')'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True);parser.add_argument('--project')
    parser.add_argument('--project-dir',help='folder whose enrolled project the connection is bound to')
    args=parser.parse_args()
    try:result=headers(args.home,project=args.project,project_dir=args.project_dir or None,
                       url=os.environ.get('CLAUDE_CODE_MCP_SERVER_URL'))
    except Exception as error:
        print(failure(error),file=sys.stderr)
        raise SystemExit(1) from None
    # Intended only for the host's credential helper pipe, never a log or MCP result.
    print(json.dumps(result))


if __name__=='__main__':main()
