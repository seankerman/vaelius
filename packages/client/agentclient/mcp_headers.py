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
    scope=None
    if project:
        selected=project
        recorded=enrollments(home)
        enrolled|={value.get('project') for value in recorded.values() if isinstance(value,dict)}
        roots=[root for root,name in config.get('projects',{}).items() if name==project]
        roots+=[root for root,value in recorded.items() if isinstance(value,dict) and value.get('project')==project]
        for root in roots:
            try:found=resolve_scope(config,{'cwd':root})
            except Exception:found=None
            if found and found['project']==project:
                scope=found;break
    else:
        # The folder's project as hooks resolve it; a read-only scope file still recalls.
        scope=resolve_scope(config,{'cwd':project_dir or os.getcwd()})
        selected=scope['project'] if scope and scope['capture'] in ('on','read-only') else None
        if scope and scope['origin']=='scope_file':enrolled.add(scope['project'])
    if selected is not None and selected not in enrolled:
        raise ValueError('project_not_enrolled')
    result={'Authorization':'Bearer '+backend.token,'X-AgentNetwork-Session':'mcp-'+uuid.uuid4().hex}
    if selected:result['X-AgentNetwork-Project']=selected
    label=session_label(scope,selected)
    if label:result['X-Vaelius-Session-Label']=json.dumps(label,separators=(',',':'))
    return result


def session_label(scope, selected):
    """The connection's session label: the workspace the folder starts a session in.

    The header helper cannot tell which conversation will use the connection, so it
    sends the label a session starting in that folder gets (agentclient.session_label);
    a later narrowing only widens what such a session may recall, so this is never
    wider than the session's own label. No label (old behavior) outside a workspace
    folder with capture on."""
    from agentclient.session_label import LABELLED_ORIGINS, NAME
    if (not scope or not selected or scope.get('project')!=selected or scope.get('capture')!='on'
            or scope.get('origin') not in LABELLED_ORIGINS or not NAME.fullmatch(str(selected))):
        return None
    label={'workspace':selected}
    if scope.get('sharing') in ('private','team'):label['sharing']=scope['sharing']
    return label


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
