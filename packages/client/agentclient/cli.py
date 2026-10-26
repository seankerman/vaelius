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
    sharing=sub.add_parser('sharing',help='show or change your own sharing setting (the most any of your content is '
        'shared), or with a selection, the stored level of those sources of yours')
    sharing.add_argument('--set',dest='level',choices=('private','team','organization'),
        help='private: only you; team: members of the project; organization: everyone in your organization')
    sharing.add_argument('--originals',choices=('private','team','organization'),
        help='with a selection: level for the unredacted originals (never above --set)')
    mine=sub.add_parser('mine',help='list what Vaelius holds that you created, with each source\'s current sharing and why')
    mine.add_argument('--sharing',choices=('private','team','organization'),help='only sources at this current level')
    mine.add_argument('--sessions',action='store_true',help='one line per capture session instead of per source')
    mine.add_argument('--all',action='store_true',help='include withdrawn and deleted sources')
    mine.add_argument('--limit',type=int,default=50);mine.add_argument('--cursor')
    forget=sub.add_parser('forget',help='delete your own content: a session, sources, a project or a date range (preview first)')
    share=sub.add_parser('share',help='share a session or sources of yours with one person or one project\'s members')
    share.add_argument('--originals',action='store_true',help='also the unredacted originals (reader needs source_read)')
    share.add_argument('--list',action='store_true',help='list the shares you made and the ones made with you')
    unshare=sub.add_parser('unshare',help='revoke a share you made')
    for command in (mine,forget,sharing,share,unshare):
        command.add_argument('--project',help='project (default for --session current: this folder\'s project)')
        command.add_argument('--session',help='"current" (the latest session in the project), a session handle '
            'from `mine --sessions`, or a host conversation ID')
        command.add_argument('--source',dest='sources',action='append',help='a source ID (repeatable)')
        if command in (mine,forget,sharing):
            command.add_argument('--since',help='captured on or after (UTC date or ISO time)')
            command.add_argument('--until',help='captured before (UTC date or ISO time)')
        if command in (share,unshare):
            command.add_argument('--with-principal');command.add_argument('--with-project')
        if command in (forget,sharing,share):
            command.add_argument('--confirm',metavar='TOKEN',help='apply the preview with this confirm_token')
            command.add_argument('--yes',action='store_true',help='preview and apply in one step')
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
        elif args.command in {'mine','forget','share','unshare'} or (args.command=='sharing' and (
                args.sources or args.session or args.project or args.since or args.until)):
            result=_owner_command(config,args)
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


def _selection(config,args):
    """Selectors from the command line; 'current' needs the project, by default this folder's."""
    value={}
    if getattr(args,'sources',None):value['source_ids']=args.sources
    for key in ('session','project','since','until'):
        if getattr(args,key,None):value[key]=getattr(args,key)
    if value.get('session')=='current' and not value.get('project'):
        from agentclient.hooks import resolve_scope
        scope=resolve_scope(config,{'cwd':os.getcwd()})
        if not scope:raise SystemExit(json.dumps({'error':'project_required','reason':'--session current needs --project '
            'or an enrolled project folder'}))
        value['project']=scope['project']
    return value


def _owner_command(config,args):
    """Owner self-service: mine, forget, sharing (with a selection), share and unshare."""
    from agentclient.transport import enterprise_client
    client=enterprise_client(config,timeout=30)
    if args.command=='mine':
        request=dict(_selection(config,args),limit=args.limit,view='sessions' if args.sessions else 'sources',
                     state='all' if args.all else 'active')
        if args.sharing:request['sharing']=args.sharing
        if args.cursor:request['cursor']=args.cursor
        return client.request('/enterprise/v3/my-data',request)
    if args.command=='share' and args.list:
        return client.request('/enterprise/v3/my-data/grants',{})
    if args.command in {'share','unshare'}:
        if bool(args.session)==bool(args.sources) or bool(args.with_principal)==bool(args.with_project):
            raise SystemExit(json.dumps({'error':'one of --session/--source and one of --with-principal/--with-project'}))
        request={'scope':'session' if args.session else 'source',
                 'ids':[args.session] if args.session else args.sources,
                 'grantee_kind':'principal' if args.with_principal else 'project',
                 'grantee':args.with_principal or args.with_project}
        if args.project or args.session=='current':
            request['project']=_selection(config,args).get('project')
        if args.command=='unshare':
            return client.request('/enterprise/v3/my-data/revoke',dict(request,confirm=True))
        path='/enterprise/v3/my-data/grant';request['originals']=args.originals
    else:
        request=_selection(config,args)
        if not request:raise SystemExit(json.dumps({'error':'selection_required','reason':'name --session, --source, '
            '--project, --since or --until'}))
        if args.command=='forget':path='/enterprise/v3/my-data/forget'
        else:
            if not args.level:raise SystemExit(json.dumps({'error':'--set LEVEL required with a selection'}))
            path='/enterprise/v3/my-data/sharing';request['sharing']=args.level
            if args.originals:request['originals']=args.originals
    if args.confirm:return client.request(path,dict(request,confirm=args.confirm))
    preview=client.request(path,dict(request,confirm=False))
    if not args.yes or not preview.get('confirm_token'):
        if preview.get('confirm_token'):
            preview['next']='run again with --confirm '+preview['confirm_token']+' (or --yes) to apply'
        return preview
    return {'preview':preview,'applied':client.request(path,dict(request,confirm=preview['confirm_token']))}


def vaelius_main(argv=None):
    """New installations use a separate default; explicit profile paths are stable."""
    return main(argv, profile_namespace='vaelius')


if __name__=='__main__':main()
