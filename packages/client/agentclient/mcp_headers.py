"""Local credential/context helper for a direct backend HTTP MCP connection."""
import argparse
import json
import os
from pathlib import Path
import uuid


def headers(home, *, project=None):
    from agentclient.transport import enterprise_client
    from agentclient.hooks import binding
    config=json.loads((Path(home)/'config.json').read_text())
    backend=enterprise_client(config)
    selected=project or binding(config,{'cwd':os.getcwd()})
    if selected is not None and selected not in set(config.get('projects',{}).values())|set(config.get('sessions',{}).values()):
        raise ValueError('project_not_enrolled')
    result={'Authorization':'Bearer '+backend.token,'X-AgentNetwork-Session':'mcp-'+uuid.uuid4().hex}
    if selected:result['X-AgentNetwork-Project']=selected
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True);parser.add_argument('--project')
    args=parser.parse_args()
    try:result=headers(args.home,project=args.project)
    except Exception:raise SystemExit('AgentNetwork credential/context unavailable') from None
    # Intended only for Codex's credential helper pipe, never a log or MCP result.
    print(json.dumps(result))


if __name__=='__main__':main()
