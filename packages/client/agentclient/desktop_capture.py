"""Verified transcript metadata helpers for structured model-free capture."""
import json
from pathlib import Path
def checked_path(path, session, allowed_root=None):
    p = Path(path).expanduser().resolve()
    root = Path(allowed_root).resolve() if allowed_root else (Path.home()/'.codex/sessions').resolve()
    if root not in p.parents or p.suffix!='.jsonl' or not p.name.endswith(session+'.jsonl'):
        raise ValueError('transcript_scope')
    with p.open('rb') as f:
        first = json.loads(f.readline(65536))
    if not isinstance(first,dict) or not isinstance(first.get('payload'),dict) or first.get('type')!='session_meta' or first['payload'].get('id')!=session:
        raise ValueError('transcript_identity')
    return p


def text_content(content):
    if not isinstance(content,list):return ''
    return '\n'.join(c['text'] for c in content if isinstance(c,dict) and c.get('type') in ('text','input_text','output_text') and isinstance(c.get('text'),str))



def poll_enterprise(home, config, allowed_root=None):
    from agentclient.transport import require_backend
    from agentclient.enterprise_desktop import poll
    require_backend(config)
    return poll(home,config,allowed_root=allowed_root)
