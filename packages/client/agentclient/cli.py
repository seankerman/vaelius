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
    for command in ('hook','status','backend-check','capture','capture-status','pause','resume','uninstall'):sub.add_parser(command)
    drain=sub.add_parser('outbox-drain');drain.add_argument('--max-events',type=int,default=32);drain.add_argument('--max-seconds',type=float,default=5)
    install=sub.add_parser('install');install.add_argument('--codex-home',default=str(Path.home()/'.codex'))
    renewal=sub.add_parser('credential-renew');renewal.add_argument('--force',action='store_true')
    for command in ('logout','credential-revoke'):
        revoke=sub.add_parser(command,help='revoke this profile\'s token family and delete local tokens')
        revoke.add_argument('--local-only',action='store_true',help='delete local tokens without contacting the service')
    enroll=sub.add_parser('enroll-project');enroll.add_argument('--root',required=True);enroll.add_argument('--name',required=True)
    args=parser.parse_args(argv);home=private_dir(args.home)
    if args.command=='uninstall':
        from agentclient.install import uninstall
        uninstall(home);return
    if args.command=='enroll-project':
        from agentclient.install import enroll_project
        result=enroll_project(home,args.root,args.name)
    else:
        config=json.loads((home/'config.json').read_text())
        from agentclient.transport import require_backend,enterprise_client
        require_backend(config)
        if args.command=='credential-renew':
            from agentclient.credentials import renew
            result=renew(config['knowledge_backend'],force=args.force)
        elif args.command in {'logout','credential-revoke'}:
            from agentclient.credentials import logout
            try:result=logout(config['knowledge_backend'],local_only=args.local_only)
            except Exception as exc:
                # Tokens are kept so the revocation can be retried; never print them.
                raise SystemExit(json.dumps({'error':'credential_revocation_failed',
                    'reason':type(exc).__name__,'local_credentials_deleted':False})) from None
        elif args.command=='hook':
            from agentclient.hooks import handle
            try:
                raw=sys.stdin.buffer.read(32*1024*1024+1)
                if len(raw)>32*1024*1024:raise ValueError('hook_payload_bound')
                result=handle(home,json.loads(raw))
            except Exception as exc:
                result={'systemMessage':'Vaelius capture unavailable ('+type(exc).__name__+'). Normal work continues.'}
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
        else:result=enterprise_client(config).request('/enterprise/v1/status')
    print(json.dumps(result,indent=2))


def vaelius_main(argv=None):
    """New installations use a separate default; explicit profile paths are stable."""
    return main(argv, profile_namespace='vaelius')


if __name__=='__main__':main()
