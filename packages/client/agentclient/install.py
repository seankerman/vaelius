import json
import fcntl
from pathlib import Path
import shlex
import sys
import tempfile
import tomllib

from agentclient.local_state import private_dir


EVENTS=("SessionStart","UserPromptSubmit","PostToolUse","Stop","Interrupt","PostCompact")


def enroll_project(home, root, name):
    """Add one local project without changing hooks, history, or publication."""
    home=private_dir(home)
    root=Path(root).expanduser().resolve(strict=True)
    if not root.is_dir() or not name or not isinstance(name,str):
        raise ValueError('invalid_project')
    path=home/'config.json'
    with (home/'config.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        config=json.loads(path.read_text()) if path.exists() else {}
        projects=config.setdefault('projects',{})
        old=projects.get(str(root))
        if old is not None and old!=name:raise ValueError('project_root_already_bound')
        projects[str(root)]=name
        with tempfile.NamedTemporaryFile(mode='w',dir=home,prefix='config-',suffix='.tmp',delete=False) as stream:
            json.dump(config,stream,indent=2);stream.write('\n');tmp=Path(stream.name)
        tmp.chmod(0o600)
        tmp.replace(path)
    return {'root':str(root),'project':name,'status':'enrolled_forward_only'}


def install(home, codex_home, projects, sessions):
    home=private_dir(home);codex_home=Path(codex_home).expanduser()
    config_path=home/"config.json"
    config=json.loads(config_path.read_text()) if config_path.exists() else {}
    from agentclient.transport import require_backend
    require_backend(config)
    mcp_path=codex_home/"config.toml"
    previous=mcp_path.read_text() if mcp_path.exists() else ""
    registration=prepare_mcp(previous,home)
    config.setdefault("projects",{}).update(projects)
    config.setdefault("sessions",{}).update(sessions)
    config.setdefault("paused",False)
    config["remote_publication"]=False
    config_path.write_text(json.dumps(config,indent=2));config_path.chmod(0o600)
    command=" ".join(shlex.quote(x) for x in (sys.executable,"-m","agentclient.cli","--home",str(home),"hook"))
    path=codex_home/"hooks.json";codex_home.mkdir(parents=True,exist_ok=True)
    doc=json.loads(path.read_text()) if path.exists() else {"hooks":{}}
    # An interpreter upgrade changes the command. Remove only the old command
    # bound by this profile's installation receipt, never a prefix/name match.
    receipt=home/'installation.json'
    owned=json.loads(receipt.read_text()) if receipt.exists() else {}
    old=owned.get('command','')
    try:parts=shlex.split(old)
    except ValueError:parts=[]
    if (old and Path(owned.get('hooks_file','')).resolve()==path.resolve()
            and len(parts)==6 and parts[1:]==['-m','agentclient.cli','--home',str(home),'hook']):
        for event,groups in doc.get('hooks',{}).items():
            for group in groups:
                group['hooks']=[h for h in group.get('hooks',[]) if h.get('command')!=old]
            doc['hooks'][event]=[g for g in groups if g.get('hooks')]
    # Retain all unrelated handlers; manage only our exact installed command.
    for event in EVENTS:
        entries=doc.setdefault("hooks",{}).setdefault(event,[])
        handler={"type":"command","command":command,"timeout":2,"statusMessage":"Vaelius memory"}
        if event in ("SessionStart","UserPromptSubmit","PostToolUse"):handler["additionalContextLimit"]=2000
        found=False
        for group in entries:
            for i,h in enumerate(group.get("hooks",[])):
                if h.get("command")==command:group["hooks"][i]=handler;found=True
        if not found:entries.append({"hooks":[handler]})
    if path.exists() and not (home/"hooks-before-install.json").exists():
        (home/"hooks-before-install.json").write_bytes(path.read_bytes())
    tmp=path.with_suffix(".agentnetwork.tmp");tmp.write_text(json.dumps(doc,indent=2)+"\n");tmp.chmod(0o600);tmp.replace(path)
    atomic_text(mcp_path,registration)
    (home/"installation.json").write_text(json.dumps({"hooks_file":str(path),"command":command,"mcp_file":str(mcp_path),"mcp_block":mcp_block(home)},indent=2))
    return {"hooks_file":str(path),"mcp_file":str(mcp_path),"events":list(EVENTS),"status":"installed_pending_host_trust"}


def uninstall(home):
    home=Path(home);p=home/"installation.json"
    if not p.exists():return
    installed=json.loads(p.read_text());path=Path(installed["hooks_file"])
    if path.exists():
        doc=json.loads(path.read_text())
        for event,groups in list(doc.get("hooks",{}).items()):
            kept=[]
            for group in groups:
                group=dict(group);group["hooks"]=[h for h in group.get("hooks",[]) if h.get("command")!=installed["command"]]
                if group["hooks"]:kept.append(group)
            if kept:doc["hooks"][event]=kept
            else:doc["hooks"].pop(event,None)
        path.write_text(json.dumps(doc,indent=2)+"\n")
    if installed.get('mcp_file'):
        mcp_path=Path(installed['mcp_file'])
        if mcp_path.exists():
            current=mcp_path.read_text();block=installed['mcp_block']
            if block in current:atomic_text(mcp_path,current.replace(block,'',1))
    config_path=home/"config.json";c=json.loads(config_path.read_text());c["paused"]=True;config_path.write_text(json.dumps(c,indent=2))


_BEGIN='# BEGIN AgentNetwork managed MCP\n'
_END='# END AgentNetwork managed MCP\n'

def atomic_text(path,text):
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as stream:
        stream.write(text);temporary=Path(stream.name)
    temporary.chmod(0o600);temporary.replace(path)

def mcp_block(home):
    from agentclient.transport import require_backend, backend_endpoint
    config=json.loads((Path(home)/'config.json').read_text())
    backend=require_backend(config)
    endpoint=backend_endpoint(backend)+'/mcp'
    helper=' '.join(shlex.quote(x) for x in (sys.executable,'-m','agentclient.mcp_headers','--home',str(home)))
    return ('\n'+_BEGIN+'[mcp_servers.agentnetwork_memory]\nurl = '+json.dumps(endpoint)+
        '\nhttp_headers_helper = '+json.dumps(helper)+
        '\nstartup_timeout_sec = 10\ntool_timeout_sec = 60\n'+_END)


def prepare_mcp(previous,home):
    parsed=tomllib.loads(previous)
    if 'agentnetwork_memory' in parsed.get('mcp_servers',{}):
        block=mcp_block(home)
        if block in previous:return previous
        receipt=Path(home)/'installation.json'
        owned=json.loads(receipt.read_text()).get('mcp_block') if receipt.exists() else None
        if not owned or previous.count(owned)!=1:raise ValueError('existing_MCP_registration_not_owned_by_this_installation')
        result=previous.replace(owned,block,1);tomllib.loads(result)
        return result
    if _BEGIN in previous or _END in previous:raise ValueError('incomplete_managed_MCP_configuration')
    result=previous+mcp_block(home);tomllib.loads(result)
    return result
