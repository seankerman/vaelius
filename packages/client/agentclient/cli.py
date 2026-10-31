"""Model-free capture, transport and agent integration commands."""
import argparse
import json
import os
from pathlib import Path
import sys
from agentclient.local_state import private_dir


# Sharing levels accepted on the command line. The access model has two states
# (`shared` and `private`); the old `team`/`organization` level names stay
# acceptable so existing scripts and muscle memory keep working. Translation to
# the wire shape (`contributions_default` for the person setting, `sharing` for
# a selection) happens where the request is built.
SHARING_LEVELS = ('shared', 'private', 'team', 'organization')
# Workspace membership is binary (migration 055).
WORKSPACE_ROLES = ('member', 'admin')


def _contributions_default(level):
    """Translate a CLI sharing level to the person-setting wire value."""
    return 'private' if level == 'private' else 'shared'


def _selection_sharing(level):
    """Translate a CLI sharing level to the owner-selection wire value."""
    return 'private' if level == 'private' else 'shared'


def _add_workspace_commands(sub):
    """`vaelius-client workspace` subcommands over /enterprise/v3/workspaces/*."""
    workspace = sub.add_parser('workspace', help='manage workspaces and their access list and linked repositories')
    inner = workspace.add_subparsers(dest='workspace_command', required=True)
    inner.add_parser('list', help='workspaces you can see')
    create = inner.add_parser('create', help='create a workspace you administer')
    create.add_argument('name')
    create.add_argument('--private', action='store_true',
                        help='only you (admin) and whoever an admin adds; the default is org: everyone '
                             'signed in to your organization can read it (when the tenant allows it)')
    create.add_argument('--item-default', choices=('shared', 'private'), default='shared',
                        help='whether new items are workspace-audience (shared) or owner-only (private)')
    visibility = inner.add_parser('set-visibility', help='admins: make a workspace private or org-readable')
    visibility.add_argument('workspace')
    visibility.add_argument('visibility', choices=('private', 'org'))
    show = inner.add_parser('show', help='access list, repositories and audience size')
    show.add_argument('workspace')
    settings = inner.add_parser('settings', help='rename, change item default, archive or unarchive')
    settings.add_argument('workspace')
    settings.add_argument('--name', help='new workspace name')
    settings.add_argument('--item-default', choices=('shared', 'private'))
    settings.add_argument('--active', choices=('true', 'false'), help='false archives')
    set_entry = inner.add_parser('set-entry', help='add a member or admin (a person or a group), or change their role')
    set_entry.add_argument('workspace')
    set_entry.add_argument('--subject-kind', choices=('principal', 'group'), required=True)
    set_entry.add_argument('--subject', required=True, help='principal/group id or name')
    set_entry.add_argument('--role', default='member', help='member (default: read and write) or admin (also '
                           'manages membership and settings); org-wide reading is `set-visibility org`')
    remove_entry = inner.add_parser('remove-entry', help='remove an entry from the access list')
    remove_entry.add_argument('workspace')
    remove_entry.add_argument('--subject-kind', choices=('principal', 'group', 'organization'), required=True)
    remove_entry.add_argument('--subject', help='principal/group id or name (omit for organization)')
    link = inner.add_parser('link', help='link a Git remote to this workspace')
    link.add_argument('workspace');link.add_argument('remote')
    unlink = inner.add_parser('unlink', help='unlink a Git remote from this workspace')
    unlink.add_argument('workspace');unlink.add_argument('remote')


def _add_remember_commands(sub):
    """`vaelius-client remember add|list|remove` and `import-memory` (agentclient.remembered)."""
    remember = sub.add_parser('remember', help='keep something in Vaelius memory, private to you until you share it')
    inner = remember.add_subparsers(dest='remember_command', required=True)
    add = inner.add_parser('add', help='remember text (an argument, or stdin with -); up to 8 KB')
    add.add_argument('text', help='the text, or - to read it from stdin')
    add.add_argument('--title', help='a short title (default: the first line)')
    add.add_argument('--workspace', help='workspace that stores it (default: this folder\'s)')
    add.add_argument('--always-load', action='store_true',
                     help='deliver it at session start and with recall, where its audience allows')
    add.add_argument('--personal', action='store_true', help='deliver it in every workspace, not only its own')
    add.add_argument('--replace', metavar='ID', help='store this as a new version of an item from `remember list`')
    listed = inner.add_parser('list', help='your remembered items (titles and ids)')
    listed.add_argument('--workspace');listed.add_argument('--all', action='store_true', help='include removed items')
    removed = inner.add_parser('remove', help='remove one of your remembered items')
    removed.add_argument('id')
    importer = sub.add_parser('import-memory', help='import a Claude Code auto-memory folder as remembered items '
                              '(dry run unless --apply; prints counts only; never changes the folder)')
    importer.add_argument('--from', dest='folder', help='the memory folder (default: this repository\'s, under '
                          'the Claude Code configuration folder)')
    importer.add_argument('--workspace', help='workspace that stores entries of a folder that maps to no repository')
    importer.add_argument('--claude-config-dir', help='Claude Code configuration folder (default: CLAUDE_CONFIG_DIR or ~/.claude)')
    mode = importer.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', default=True, help='count only (the default)')
    mode.add_argument('--apply', action='store_true', help='write the items')


def _add_push_commands(sub):
    """`vaelius-client push-session` (agentclient.push_session): one past conversation, by hand."""
    push = sub.add_parser('push-session', help='send one of your own past Claude Code or Codex conversations '
                          'from a scoped folder, owner-private (dry run unless --apply; counts only)')
    push.add_argument('target', nargs='?', help='a session id or the transcript file')
    push.add_argument('--list-recent', nargs='?', type=int, const=20, metavar='N',
                      help='list your N most recent conversations (default 20) and whether each can be pushed')
    push.add_argument('--apply', action='store_true', help='queue the events for delivery (default: counts only)')
    push.add_argument('--claude-config-dir', help='Claude Code configuration folder (default: CLAUDE_CONFIG_DIR or ~/.claude)')
    push.add_argument('--codex-home', help='Codex home folder (default: CODEX_HOME or ~/.codex)')


def _push_command(config, home, args):
    from agentclient import push_session
    claude = push_session.claude_dir(args.claude_config_dir)
    codex = push_session.codex_dir(args.codex_home)
    if args.list_recent is not None:
        if args.target or args.apply:raise SystemExit(json.dumps({'error': 'list_recent_takes_no_session'}))
        return push_session.recent(config, limit=args.list_recent, claude=claude, codex=codex)
    if not args.target:raise SystemExit(json.dumps({'error': 'session_required', 'reason': 'pass a session id, '
                                                    'a transcript path or --list-recent'}))
    try:
        return push_session.push(config, home, args.target, apply=args.apply, claude=claude, codex=codex)
    except push_session.PushRefused as refused:
        raise SystemExit(json.dumps({'error': 'push_refused', 'reason': str(refused)})) from None


def _remember_command(config, home, args):
    from agentclient import remembered
    if args.command == 'import-memory':
        folder = args.folder
        if not folder:
            from agentclient.claude_code import config_dir
            from agentclient.hooks import git_common_dir
            from agentclient.origin import slug
            common = git_common_dir(Path(os.getcwd()))
            top = common.parent if common and common.name == '.git' else Path(os.getcwd()).resolve()
            folder = config_dir(args.claude_config_dir) / 'projects' / slug(str(top)) / 'memory'
        return remembered.import_memory(config, home, folder, workspace=args.workspace, apply=args.apply)
    if args.remember_command == 'list':
        return remembered.listing(config, workspace=args.workspace, include_withdrawn=args.all)
    if args.remember_command == 'remove':
        return remembered.remove(config, args.id)
    text = sys.stdin.read(remembered.MAX_TEXT + 1) if args.text == '-' else args.text
    return remembered.add(config, text, workspace=args.workspace, title=args.title, always_load=args.always_load,
                          personal=args.personal, replace=args.replace)


def _add_group_commands(sub):
    """`vaelius-client group` subcommands over /enterprise/v3/groups/*."""
    group = sub.add_parser('group', help='manage local groups and inspect synced ones')
    inner = group.add_subparsers(dest='group_command', required=True)
    inner.add_parser('list', help='groups you can see; synced ones are read-only')
    create = inner.add_parser('create', help='create a local group you administer')
    create.add_argument('name')
    create.add_argument('--workspace', help='workspace this local group belongs to (its admins also manage it)')
    show = inner.add_parser('show', help='members of a group (synced ones mirror the source)')
    show.add_argument('group')
    add_member = inner.add_parser('add-member', help='add a member to a local group')
    add_member.add_argument('group')
    add_member.add_argument('--member-kind', choices=('principal', 'group'), required=True)
    add_member.add_argument('--member', required=True)
    add_member.add_argument('--role', choices=('member', 'admin'), default='member')
    remove_member = inner.add_parser('remove-member', help='remove a member from a local group')
    remove_member.add_argument('group')
    remove_member.add_argument('--member-kind', choices=('principal', 'group'), required=True)
    remove_member.add_argument('--member', required=True)
    delete = inner.add_parser('delete', help='delete a local group')
    delete.add_argument('group')


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
    scope=sub.add_parser('scope-status',help='show the workspace, capture mode and sharing request for a folder')
    scope.add_argument('--cwd',default=os.getcwd())
    sharing=sub.add_parser('sharing',help='show or change your own sharing setting (contributions_default: shared or '
        'private), or with a selection, the stored level of those sources of yours')
    sharing.add_argument('--set',dest='level',choices=SHARING_LEVELS,
        help='shared: items you capture inherit the workspace audience; private: owner only. '
             '(team/organization are legacy aliases of shared)')
    sharing.add_argument('--originals',choices=SHARING_LEVELS,
        help='with a selection: level for the unredacted originals (never above --set); legacy, originals '
             'are now shared only via `share --originals`')
    sharing.add_argument('--apply-to-existing',action='store_true',
        help='with --set private: also mark every existing item of yours private (preview, then confirm)')
    mine=sub.add_parser('mine',help='list what Vaelius holds that you created, with each source\'s current sharing and why')
    mine.add_argument('--sharing',choices=SHARING_LEVELS,help='filter sources at this current level')
    mine.add_argument('--sessions',action='store_true',help='one line per capture session instead of per source')
    mine.add_argument('--all',action='store_true',help='include withdrawn and deleted sources')
    mine.add_argument('--limit',type=int,default=50);mine.add_argument('--cursor')
    forget=sub.add_parser('forget',help='delete your own content: a session, sources, a project or a date range (preview first)')
    share=sub.add_parser('share',help='share a session or sources of yours with one person or one group')
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
            command.add_argument('--with-principal');command.add_argument('--with-group')
        if command in (forget,sharing,share):
            command.add_argument('--confirm',metavar='TOKEN',help='apply the preview with this confirm_token')
            command.add_argument('--yes',action='store_true',help='preview and apply in one step')
    notice=sub.add_parser('notice',help='show what is captured for a workspace, who can read it, and how to stop')
    notice.add_argument('--project',required=True);notice.add_argument('--acknowledge',action='store_true')
    _add_remember_commands(sub)
    _add_push_commands(sub)
    _add_workspace_commands(sub)
    _add_group_commands(sub)
    _add_session_commands(sub)
    args=parser.parse_args(argv)
    if args.command=='guardrail-check':
        # Needs no profile: runs on every file-tool and Bash call, and must not create folders.
        from agentclient.claude_guardrails import check
        try:
            raw=sys.stdin.buffer.read(32*1024*1024+1)
            if len(raw)>32*1024*1024:raise ValueError('hook_payload_bound')
            # The profile folder is only read (session labels); it is never created here.
            result=check(json.loads(raw),args.profile,home=Path(args.home).expanduser())
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
                'note':'`workspace` chooses the access-model workspace; the service decides who reads items. '
                       'Legacy `sharing`/`originals` are requests, capped by the access list and your own setting.'}
        elif args.command in {'mine','forget','share','unshare'} or (args.command=='sharing' and (
                args.sources or args.session or args.project or args.since or args.until)):
            result=_owner_command(config,args)
        elif args.command=='sharing':
            result=_sharing_command(config,home,args)
        elif args.command=='notice':
            from agentclient import capture_notice
            value=capture_notice.fetch(config,args.project,timeout=5)
            result={'project':args.project,'notice':capture_notice.text(value,args.project),
                'notice_version':value.get('notice_version'),'source':value.get('source'),
                'capture':'paused' if config.get('paused') else ('disabled' if args.project in config.get('capture_disabled_projects',[]) else 'on')}
            if args.acknowledge:result['acknowledgement']=capture_notice.record(home,args.project,value,'cli')
        elif args.command in ('remember','import-memory'):
            result=_remember_command(config,home,args)
        elif args.command=='push-session':
            result=_push_command(config,home,args)
        elif args.command=='workspace':
            result=_workspace_command(config,args)
        elif args.command=='group':
            result=_group_command(config,args)
        elif args.command in ('session','use'):
            result=_session_command(home,config,args)
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


def _sharing_command(config, home, args):
    """Person setting (`contributions_default`), with --apply-to-existing preview/confirm."""
    from agentclient.transport import enterprise_client
    from agentclient import capture_notice
    client = enterprise_client(config, timeout=5)
    if args.level is None:
        value = client.request('/enterprise/v3/sharing', {})
    else:
        request = {'contributions_default': _contributions_default(args.level)}
        if args.apply_to_existing:
            if request['contributions_default'] != 'private':
                raise SystemExit(json.dumps({'error': '--apply-to-existing requires --set private'}))
            request['apply_to_existing'] = True
            if args.confirm:
                request['confirm'] = args.confirm
                value = client.request('/enterprise/v3/sharing', request)
            else:
                preview = client.request('/enterprise/v3/sharing', request)
                token = (preview.get('existing') or {}).get('confirm_token')
                if not args.yes or not token:
                    if token:
                        preview['next'] = ('run again with --confirm ' + token +
                                           ' (or --yes) to mark existing items private')
                    return preview
                applied = client.request('/enterprise/v3/sharing',
                                         dict(request, confirm=token))
                return {'preview': preview, 'applied': applied}
        else:
            value = client.request('/enterprise/v3/sharing', request)
    # Keep the status line in sync with what the service confirms.
    for key in ('contributions_default', 'max_sharing'):
        if value.get(key) is not None:
            try: capture_notice.remember_personal_sharing(home, value[key])
            except (OSError, ValueError): pass
            break
    note = ('your setting caps and shapes every workspace capture; `shared` means items inherit the '
            'workspace audience, `private` means owner only')
    return {'principal': value.get('principal'),
            'contributions_default': value.get('contributions_default'),
            'max_sharing': value.get('max_sharing'),
            'tenant_ceiling': value.get('tenant_ceiling'),
            'widest_entry': value.get('widest_entry'),
            'existing': value.get('existing'),
            'changed': value.get('changed', False),
            'note': note}


def _add_session_commands(sub):
    """`vaelius-client session use|status` (and the `use` shorthand): fixed session labels."""
    session = sub.add_parser('session', help='show or narrow the fixed label of a running agent session')
    inner = session.add_subparsers(dest='session_command', required=True)
    for parser in (inner.add_parser('use', help='narrow the session to also use a workspace (or make it private); '
                                                 'capture is then limited to people who can read both'),
                   sub.add_parser('use', help='shorthand for `session use`')):
        parser.add_argument('target', help='a workspace name, a folder of one, a linked remote, or "private"')
        parser.add_argument('--session', help='host session ID (default: the session active in this folder)')
    status = inner.add_parser('status', help='the label of the session active in this folder')
    status.add_argument('--session', help='host session ID (default: the session active in this folder)')
    readonly = inner.add_parser('read-only', help='save nothing more from this chat (one-way until /clear or a '
                                'new chat); reads keep working')
    readonly.add_argument('--session', help='host session ID (default: the session active in this folder)')


def _session_command(home, config, args):
    from urllib.error import HTTPError
    from agentclient import session_label
    from agentclient.scope_file import scope_names
    command = 'use' if args.command == 'use' else args.session_command
    try:
        if command == 'use':
            return session_label.use(home, config, args.target, session=args.session)
        if command == 'read-only':
            return session_label.read_only(home, config, session=args.session)
        _, entry = session_label.resolve_session(home, os.getcwd(), args.session, scope_names(config))
    except session_label.UseRefused as refused:
        raise SystemExit(json.dumps({'error': 'session_use_refused', 'reason': str(refused)})) from None
    except HTTPError as error:
        if error.code not in (400, 403, 404):raise
        raise SystemExit(json.dumps({'error': 'session_use_refused', 'reason': 'the service refused: you cannot '
                                     'read that workspace, or it does not exist'})) from None
    descriptor = entry.get('descriptor') or {}
    return {'session': entry.get('short'), 'label': session_label.display(entry),
            'narrowed': entry.get('narrowed', []), 'private': bool(entry.get('private')),
            'status': session_label.status_line(entry) or ('vaelius: session label ' + session_label.display(entry)),
            'readable_here': sorted(session_label.allowed(entry)[0]),
            'descriptor': {key: descriptor.get(key) for key in ('epoch', 'role', 'fetched_at', 'refresh_after',
                                                                 'expires_at')} if descriptor else None}


def _workspace_payload(args):
    """Request body for a `vaelius-client workspace` subcommand."""
    command = args.workspace_command
    if command == 'list': return '/enterprise/v3/workspaces', {}
    if command == 'create':
        body = {'name': args.name}
        if args.private: body['visibility'] = 'private'
        if args.item_default: body['item_default'] = args.item_default
        return '/enterprise/v3/workspaces/create', body
    if command == 'set-visibility':
        return '/enterprise/v3/workspaces/visibility', {'workspace': args.workspace, 'visibility': args.visibility}
    if command == 'show':
        return '/enterprise/v3/workspaces/show', {'workspace': args.workspace}
    if command == 'settings':
        body = {'workspace': args.workspace}
        if args.name is not None: body['name'] = args.name
        if args.item_default is not None: body['item_default'] = args.item_default
        if args.active is not None: body['active'] = (args.active == 'true')
        return '/enterprise/v3/workspaces/settings', body
    if command == 'set-entry':
        if args.role not in WORKSPACE_ROLES:
            # reader and writer were merged into member (migration 055); refused, not
            # mapped, so a script meant to grant read only never grants write.
            raise SystemExit(json.dumps({'error': 'role_must_be_member_or_admin', 'reason':
                'workspace roles are member (read and write) and admin; reader and writer were merged into '
                'member. Everyone in the org reads an org-visible workspace (`workspace set-visibility`).'}))
        body = {'workspace': args.workspace, 'subject_kind': args.subject_kind,
                'subject': args.subject, 'role': args.role}
        return '/enterprise/v3/workspaces/entries/set', body
    if command == 'remove-entry':
        if args.subject_kind == 'organization':
            body = {'workspace': args.workspace, 'subject_kind': 'organization'}
        else:
            if not args.subject:
                raise SystemExit(json.dumps({'error': '--subject is required for principal and group entries'}))
            body = {'workspace': args.workspace, 'subject_kind': args.subject_kind, 'subject': args.subject}
        return '/enterprise/v3/workspaces/entries/remove', body
    if command == 'link':
        from agentclient.general_contract import normalize_remote
        remote = normalize_remote(args.remote) or args.remote
        return '/enterprise/v3/workspaces/repos/link', {'workspace': args.workspace, 'remote': remote}
    if command == 'unlink':
        from agentclient.general_contract import normalize_remote
        remote = normalize_remote(args.remote) or args.remote
        return '/enterprise/v3/workspaces/repos/unlink', {'workspace': args.workspace, 'remote': remote}
    raise SystemExit(json.dumps({'error': 'unknown_workspace_command'}))


def _workspace_command(config, args):
    from agentclient.transport import enterprise_client
    path, body = _workspace_payload(args)
    result = enterprise_client(config, timeout=15).request(path, body)
    if args.workspace_command in ('create', 'set-visibility') and isinstance(result, dict) and result.get('notice'):
        name = args.name if args.workspace_command == 'create' else args.workspace
        sys.stderr.write('vaelius: ' + name + ' ' + result['notice'] + '\n')
    return result


def _group_payload(args):
    command = args.group_command
    if command == 'list': return '/enterprise/v3/groups', {}
    if command == 'create':
        body = {'name': args.name}
        if args.workspace: body['workspace'] = args.workspace
        return '/enterprise/v3/groups/create', body
    if command == 'show':
        return '/enterprise/v3/groups/show', {'group': args.group}
    if command == 'add-member':
        return ('/enterprise/v3/groups/members/add',
                {'group': args.group, 'member_kind': args.member_kind, 'member': args.member, 'role': args.role})
    if command == 'remove-member':
        return ('/enterprise/v3/groups/members/remove',
                {'group': args.group, 'member_kind': args.member_kind, 'member': args.member})
    if command == 'delete':
        return '/enterprise/v3/groups/delete', {'group': args.group}
    raise SystemExit(json.dumps({'error': 'unknown_group_command'}))


def _group_command(config, args):
    from agentclient.transport import enterprise_client
    path, body = _group_payload(args)
    return enterprise_client(config, timeout=15).request(path, body)


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
        if args.sharing:request['sharing']=_selection_sharing(args.sharing)
        if args.cursor:request['cursor']=args.cursor
        return client.request('/enterprise/v3/my-data',request)
    if args.command=='share' and args.list:
        return client.request('/enterprise/v3/my-data/grants',{})
    if args.command in {'share','unshare'}:
        if bool(args.session)==bool(args.sources) or bool(args.with_principal)==bool(args.with_group):
            raise SystemExit(json.dumps({'error':'one of --session/--source and one of --with-principal/--with-group'}))
        request={'scope':'session' if args.session else 'source',
                 'ids':[args.session] if args.session else args.sources,
                 'grantee_kind':'principal' if args.with_principal else 'group',
                 'grantee':args.with_principal or args.with_group}
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
            path='/enterprise/v3/my-data/sharing'
            request['sharing']=_selection_sharing(args.level)
            if args.originals:request['originals']=_selection_sharing(args.originals)
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
