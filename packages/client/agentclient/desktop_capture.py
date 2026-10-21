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


def session_cwd(path):
    """Working directory recorded in a verified transcript's session_meta, else None."""
    try:
        with Path(path).open('rb') as f:
            first = json.loads(f.readline(65536))
        cwd = first['payload'].get('cwd')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return cwd if isinstance(cwd, str) and cwd else None


def text_content(content):
    if not isinstance(content,list):return ''
    return '\n'.join(c['text'] for c in content if isinstance(c,dict) and c.get('type') in ('text','input_text','output_text') and isinstance(c.get('text'),str))



def poll_enterprise(home, config, allowed_root=None):
    from agentclient.transport import require_backend
    from agentclient.enterprise_desktop import poll
    require_backend(config)
    return poll(home,config,allowed_root=allowed_root)
