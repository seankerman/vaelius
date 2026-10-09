"""Native, tool-enabled evaluation receiver using the existing Codex login.

Separate from the tool-disabled curator. The caller owns the MCP process and
shared/service accounting; this module retains installation accounting. No global
configuration edits, API keys, shell tools or automatic source capture.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from agenthub.processing.harness import resolve_executable
from agenthub.processing.harness_errors import HarnessError
from agenthub.processing.usage import Ledger

DISABLED=('hooks','shell_tool','unified_exec','multi_agent','plugins','apps',
          'view_image','image_generation','computer_use',
          'browser_use','in_app_browser','workspace_dependencies')


def allowed_discovery(item):
    """Codex's built-in empty registry lookup is not another data provider."""
    names={'list_mcp_resources':'resources','list_mcp_resource_templates':'resourceTemplates'}
    if item.get('server')!='codex' or item.get('tool') not in names:return False
    content=(item.get('result') or {}).get('content',[])
    try:
        return len(content)==1 and json.loads(content[0]['text'])=={names[item['tool']]:[]}
    except (ValueError,KeyError,TypeError):return False


def command(config,root,mcp_command):
    settings=config['observer']; executable=resolve_executable(settings.get('executable','codex'))
    args=[executable,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
          '--sandbox','read-only','--cd',str(root),'--model',settings['model'],'--json',
          '--color','never','--output-last-message',str(root/'answer.txt'),
          '-c','forced_login_method="chatgpt"','-c','approval_policy="never"',
          '-c','model_reasoning_effort='+json.dumps(settings.get('reasoning','low')),
          '-c','web_search="disabled"','-c','project_doc_max_bytes=0',
          '-c','mcp_servers.replay_memory.command='+json.dumps(mcp_command[0]),
          '-c','mcp_servers.replay_memory.args='+json.dumps(mcp_command[1:]),
          '-c','mcp_servers.replay_memory.required=true']
    args.extend(['--enable','code_mode_host'])
    for feature in DISABLED:args.extend(['--disable',feature])
    return args+['-']


def run_receiver(home,config,prompt,mcp_command):
    root=Path(home).resolve(); root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (root/'events.jsonl').exists():raise HarnessError('receiver_output_exists')
    args=command(config,root,mcp_command)
    env={k:v for k,v in os.environ.items() if k in {'HOME','PATH','USER','LOGNAME','TMPDIR','LANG','CODEX_HOME'}}
    env['AGENTNETWORK_OBSERVER']='1'
    login=subprocess.run([args[0],'login','status'],env=env,capture_output=True,text=True,timeout=15)
    if login.returncode or 'ChatGPT' not in login.stdout+login.stderr:raise HarnessError('chatgpt_login_required')
    ledger=Ledger(config);ident=None;usage={};started=time.monotonic()
    try:
        ident=ledger.reserve(config.get('_purpose','agent_replay'),config['observer']['model'],config.get('_call_context'))
        with (root/'events.jsonl').open('x') as events,(root/'errors.txt').open('x') as errors:
            proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=events,stderr=errors,
                cwd=root,env=env,text=True,start_new_session=True,umask=0o077)
            try:proc.communicate(prompt,timeout=config['observer'].get('timeout_seconds',180))
            except BaseException:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                proc.wait();raise
        unexpected=[];calls=[];errors=[]
        for line in (root/'events.jsonl').read_text().splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('type')=='turn.completed':usage=event.get('usage',{})
            item=event.get('item',{});kind=item.get('type')
            if kind=='error':errors.append(item.get('message','receiver_tool_error'))
            if kind in {'command_execution','web_search','file_change'}:unexpected.append(kind)
            if kind=='mcp_tool_call' and event.get('type')=='item.completed':
                if item.get('server')!='replay_memory' and not allowed_discovery(item):unexpected.append('foreign_mcp')
                calls.append(item)
        if unexpected:raise HarnessError('unexpected_receiver_tool')
        if errors:raise HarnessError('receiver_reported_tool_error')
        if proc.returncode or not (root/'answer.txt').exists():raise HarnessError('receiver_failed')
        result={'answer':(root/'answer.txt').read_text(),'usage':usage,'native_tool_calls':calls,
                'seconds':time.monotonic()-started,'installation_call_id':ident}
        ledger.finish(ident,'done',usage);return result
    except BaseException as exc:
        if ident is not None:ledger.finish(ident,'failed',usage)
        exc.usage=usage
        raise
    finally:ledger.close()
