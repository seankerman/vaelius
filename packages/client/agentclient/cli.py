"""Model-free capture, transport and agent integration commands."""
import argparse
import json
import os
from pathlib import Path
import sys
from agentclient.local_state import private_dir


def main(argv=None, *, profile_namespace='agentnetwork'):
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',default=str(Path.home()/'.local/share'/profile_namespace/'client'))
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('status','backend-check','capture','capture-status','pause','resume'):sub.add_parser(command)
    hook=sub.add_parser('hook',help='handle one agent hook event read from stdin')
    hook.add_argument('--host',choices=('codex','claude-code'),default='codex')
    hook.add_argument('--via',choices=('settings','plugin'),default='settings',help=argparse.SUPPRESS)
    headers=sub.add_parser('mcp-headers',help='print MCP authentication headers for a host header helper')
    headers.add_argument('--project');headers.add_argument('--project-dir')
    remove=sub.add_parser('uninstall',help='remove installed hooks and MCP entries (all hosts unless one is named)')
    remove.add_argument('--claude-code',action='store_true',help='remove only Claude Code installations; capture is not paused')
    remove.add_argument('--scope',choices=('user','project'))
    remove.add_argument('--guardrails-only',action='store_true',help='Claude Code: remove only the guardrail profile')
    drain=sub.add_parser('outbox-drain');drain.add_argument('--max-events',type=int,default=32);drain.add_argument('--max-seconds',type=float,default=5)
    install=sub.add_parser('install');install.add_argument('--codex-home',default=str(Path.home()/'.codex'))
    install.add_argument('--claude-code',action='store_true',help='install into Claude Code settings instead of Codex')
    install.add_argument('--scope',choices=('user','project'),default='user',help='Claude Code: all folders, or one project')
    install.add_argument('--project-root',help='Claude Code project scope: enrolled project root (default: current folder)')
    install.add_argument('--claude-config-dir',help='Claude Code configuration folder (default: CLAUDE_CONFIG_DIR or ~/.claude)')
    install.add_argument('--guardrails',choices=('standard','strict','none'),
        help='Claude Code least-privilege profile (default: the one installed, else standard; none removes it)')
    install.add_argument('--guardrails-only',action='store_true',help='Claude Code: write only the guardrail profile (plugin users)')
    guard=sub.add_parser('guardrail-check',help='Claude Code PreToolUse guardrail check; reads one event from stdin')
    guard.add_argument('--profile',choices=('standard','strict'),required=True)
    renewal=sub.add_parser('credential-renew');renewal.add_argument('--force',action='store_true')
    sub.add_parser('credential-status',help='local sign-in status; contacts neither AWS nor the service')
    for command in ('logout','credential-revoke'):
        revoke=sub.add_parser(command,help='revoke this profile\'s token family and delete local tokens')
        revoke.add_argument('--local-only',action='store_true',help='delete local tokens without contacting the service')
    enroll=sub.add_parser('enroll-project');enroll.add_argument('--root',required=True);enroll.add_argument('--name',required=True)
    scope=sub.add_parser('scope-status',help='show the project, capture mode and sharing request for a folder')
    scope.add_argument('--cwd',default=os.getcwd())
    sharing=sub.add_parser('sharing',help='show or change your own sharing setting (the most any of your content is shared)')
    sharing.add_argument('--set',dest='level',choices=('private','team','organization'),
        help='private: only you; team: members of the project; organization: everyone in your organization')
    notice=sub.add_parser('notice',help='show what is captured for a project, who can read it, and how to stop')
    notice.add_argument('--project',required=True);notice.add_argument('--acknowledge',action='store_true')
    args=parser.parse_args(argv)
    if args.command=='guardrail-check':
        # Needs no profile: runs on every file-tool and Bash call, and must not create folders.
        from agentclient.claude_guardrails import check
        try:
            raw=sys.stdin.buffer.read(32*1024*1024+1)
            if len(raw)>32*1024*1024:raise ValueError('hook_payload_bound')
            result=check(json.loads(raw),args.profile)
        except Exception as exc:
            # strict fails closed (exit 2 blocks the call); standard must never stop normal
            # work, so it reports the failure (exit 1, shown to the person) and lets the call run.
            print('Vaelius '+args.profile+' guardrails could not check this call ('+type(exc).__name__+')'
                  +('; blocked.' if args.profile=='strict' else '.'),file=sys.stderr)
            raise SystemExit(2 if args.profile=='strict' else 1) from None
        print(json.dumps(result));return
    home=private_dir(args.home)
    if args.command=='uninstall':
        from agentclient import claude_code
        if args.claude_code or args.guardrails_only:
            print(json.dumps({'removed':claude_code.uninstall(home,scope=args.scope,guardrails_only=args.guardrails_only)},indent=2));return
        from agentclient.install import uninstall
        claude_code.uninstall(home)
        uninstall(home);return
    if args.command=='hook':
        # The handler reads the profile itself, so a missing profile can still be reported.
        from agentclient.hooks import handle
        from agentclient.host_command import upgrade_notice
        event=None
        try:
            raw=sys.stdin.buffer.read(32*1024*1024+1)
            if len(raw)>32*1024*1024:raise ValueError('hook_payload_bound')
            event=json.loads(raw)
            result=handle(home,event,host=args.host,via=args.via)
        except Exception as exc:
            result={'systemMessage':'Vaelius capture unavailable ('+type(exc).__name__+'). Normal work continues.'}
        notice=upgrade_notice(home,args.host,event)
        if notice:result=dict(result,systemMessage='\n'.join(x for x in (result.get('systemMessage'),notice) if x))
        print(json.dumps(result));return
    if args.command=='mcp-headers':
        from agentclient.mcp_headers import headers, failure
        try:result=headers(home,project=args.project,project_dir=args.project_dir or None,
                           url=os.environ.get('CLAUDE_CODE_MCP_SERVER_URL'))
        except Exception as error:
            print(failure(error),file=sys.stderr)
            raise SystemExit(1) from None
        # Only for the host's header-helper pipe; never logged.
        print(json.dumps(result));return
    if args.command=='enroll-project':
        from agentclient.install import enroll_project
        result=enroll_project(home,args.root,args.name)
    else:
        config=json.loads((home/'config.json').read_text())
        from agentclient.transport import require_backend,enterprise_client
        require_backend(config)
        if args.command=='credential-renew':
            from agentclient.credentials import renew,LoginRequired
            try:result=renew(config['knowledge_backend'],force=args.force)
            except LoginRequired as exc:
                # Fail closed with a clear status; capture keeps queuing meanwhile.
                raise SystemExit(json.dumps({'status':'signed_out','reason':str(exc)})) from None
        elif args.command=='credential-status':
            backend=config['knowledge_backend']
            if backend.get('aws_sts'):
                from agentclient.aws_sts import status
                result=status(backend)
            else:result={'status':'unknown','auth':'oauth' if backend.get('oauth') else 'legacy'}
        elif args.command in {'logout','credential-revoke'}:
            from agentclient.credentials import logout
            try:result=logout(config['knowledge_backend'],local_only=args.local_only)
            except Exception as exc:
                # Tokens are kept so the revocation can be retried; never print them.
                raise SystemExit(json.dumps({'error':'credential_revocation_failed',
                    'reason':type(exc).__name__,'local_credentials_deleted':False})) from None
        elif args.command=='scope-status':
            from agentclient.hooks import resolve_scope
            from agentclient.scope_file import enrollments
            value=resolve_scope(config,{'cwd':str(Path(args.cwd).expanduser().resolve())})
            result={'cwd':str(Path(args.cwd).expanduser().resolve()),'scope':value,
                'paused':bool(config.get('paused')),'scope_files':config.get('scope_files',['.vaelius']),
                'scope_enrollments':enrollments(home),
                'note':'sharing and originals are requests; the service applies your own sharing setting, '
                       'project and tenant policy and membership'}
        elif args.command=='sharing':
            # Your own ceiling. Lowering hides already shared content on the next
            # search; raising applies to what is captured from now on.
            from agentclient import capture_notice
            request={} if args.level is None else {'max_sharing':args.level}
            value=enterprise_client(config,timeout=5).request('/enterprise/v3/sharing',request)
            try:capture_notice.remember_personal_sharing(home,value['max_sharing'])
            except (OSError,ValueError):pass
            result={'max_sharing':value['max_sharing'],'changed':value.get('changed',False),
                'tenant_ceiling':value.get('tenant_ceiling'),
                'note':'your setting caps every project and repository request; lowering applies at once, '
                       'raising applies to new captures only'}
        elif args.command=='notice':
            from agentclient import capture_notice
            value=capture_notice.fetch(config,args.project,timeout=5)
            result={'project':args.project,'notice':capture_notice.text(value,args.project),
                'notice_version':value.get('notice_version'),'source':value.get('source'),
                'capture':'paused' if config.get('paused') else ('disabled' if args.project in config.get('capture_disabled_projects',[]) else 'on')}
            if args.acknowledge:result['acknowledgement']=capture_notice.record(home,args.project,value,'cli')
        elif args.command=='install' and (args.claude_code or args.guardrails or args.guardrails_only):
            from agentclient.claude_code import install
            result=install(home,scope=args.scope,claude_config_dir=args.claude_config_dir,project_root=args.project_root,
                           guardrails=args.guardrails or 'default',guardrails_only=args.guardrails_only)
        elif args.command=='install':
            from agentclient.install import install
            result=install(home,args.codex_home,{}, {})
        elif args.command in {'pause','resume'}:
            config['paused']=args.command=='pause'
            temporary=home/'config.json.tmp';temporary.write_text(json.dumps(config,indent=2)+'\n');temporary.chmod(0o600);temporary.replace(home/'config.json')
            result={'paused':config['paused']}
        elif args.command=='capture':
            from agentclient.desktop_capture import poll_enterprise
            result=poll_enterprise(home,config)
        elif args.command in {'capture-status','outbox-drain'}:
            from agentclient.capture_outbox import Outbox
            box=Outbox(home)
            try:
                result=box.status() if args.command=='capture-status' else box.drain(enterprise_client(config,timeout=1),max_events=args.max_events,max_seconds=args.max_seconds)
            finally:box.close()
        else:
            from agentclient.credentials import LoginRequired
            try:result=enterprise_client(config).request('/enterprise/v1/status')
            except LoginRequired as exc:raise SystemExit(json.dumps({'status':'signed_out','reason':str(exc)})) from None
    print(json.dumps(result,indent=2))


def vaelius_main(argv=None):
    """New installations use a separate default; explicit profile paths are stable."""
    return main(argv, profile_namespace='vaelius')


if __name__=='__main__':main()
