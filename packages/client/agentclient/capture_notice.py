"""Capture notice and per-session capture status shown to the person being captured.

The service supplies the organization's own wording and policy link
(`/enterprise/v3/capture-notice`). Without a reachable service the client shows
its built-in notice. Displays and acknowledgements are recorded in the profile's
`notice-acknowledgements.json`, next to `config.json`, so capture never has to
rewrite `config.json` (and can never race a concurrent `pause`).
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

DEFAULT_NOTICE = (
    'This project is enrolled in Vaelius shared memory. While capture is on, your prompts, '
    'the assistant\'s replies, and tool calls with their results are sent to your organization\'s '
    'Vaelius service after local secret redaction. Who can read them is decided by the service: '
    'private sources only by you, team sources by members of this project, organization sources '
    'by everyone in your organization. Pause with `vaelius-client pause`; withdraw an item you '
    'captured with the withdraw_memory tool.')
CAPTURED = ['user_prompts', 'assistant_replies', 'tool_calls', 'tool_results']
FILE = 'notice-acknowledgements.json'
CONTROLS = 'Pause: vaelius-client pause. Resume: vaelius-client resume.'


def default_notice():
    return {'notice': DEFAULT_NOTICE, 'policy_url': None, 'captured': CAPTURED,
            'notice_version': hashlib.sha256(json.dumps([DEFAULT_NOTICE, None]).encode()).hexdigest()[:16],
            'source': 'client_default'}


def fetch(config, project, *, timeout=0.5):
    """Server wording for this project's capture connection, else the built-in notice."""
    try:
        from agentclient.transport import enterprise_client, capture_connection
        request = {}
        if project:
            request['project'] = project
            try:
                request['connection'] = capture_connection(config, project)
            except ValueError:
                pass
        value = enterprise_client(config, timeout=timeout).request('/enterprise/v3/capture-notice', request)
        if isinstance(value, dict) and isinstance(value.get('notice'), str):
            return value
    except Exception:
        pass
    return default_notice()


def _path(home):
    return Path(home) / FILE


def read(home):
    try:
        value = json.loads(_path(home).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record(home, scope, notice, how):
    """Record that the notice for `scope` ('enrollment' or a project) was shown/acknowledged."""
    home = Path(home)
    with (home / 'notice.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read(home)
        entries = value.setdefault('projects', {}) if scope != 'enrollment' else value
        key = scope if scope != 'enrollment' else 'enrollment'
        readers = notice.get('readers') if isinstance(notice.get('readers'), dict) else {}
        entries[key] = {'notice_version': notice.get('notice_version'), 'source': notice.get('source'),
                        'policy_url': notice.get('policy_url'), 'acknowledged': how, 'at': time.time(),
                        'readers': readers.get('description') if isinstance(readers.get('description'), str) else None}
        with tempfile.NamedTemporaryFile(mode='w', dir=home, prefix='notice-', suffix='.tmp', delete=False) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        temporary.replace(_path(home))
    return entries[key]


def seen(home, project):
    return project in read(home).get('projects', {})


def recorded(home, project):
    """The last notice shown for a project, as a notice-shaped dict for `status`."""
    entry = read(home).get('projects', {}).get(project) or {}
    return {'readers': {'description': entry['readers']}} if isinstance(entry.get('readers'), str) else {}


def text(notice, project=None):
    """Plain message for the person: what is captured, who reads it, how to stop."""
    lines = ['Vaelius capture notice' + (' for project ' + project if project else '') + ':', notice['notice']]
    readers = notice.get('readers')
    if isinstance(readers, dict) and isinstance(readers.get('description'), str):
        lines.append('Readers for this project: ' + readers['description'] + '.')
    if notice.get('policy_url'):
        lines.append('Policy: ' + notice['policy_url'])
    lines.append(CONTROLS)
    return '\n'.join(lines)


def status(state, project, notice=None, *, sharing='private'):
    """One line per session: is capture on or off for this project, and how to change it."""
    if state == 'on':
        readers = (notice or {}).get('readers') or {}
        who = readers.get('description') if isinstance(readers.get('description'), str) else 'set by server policy'
        # A scope file's request; the service's project policy decides what is shared.
        passages = (' Conversation passages: team sharing requested by the repository scope file; '
                    'the service decides.' if sharing == 'team' else '')
        return ('Vaelius capture ON for project ' + project + ': prompts, assistant replies and tool '
                'calls/results are shared (readers: ' + who + ').' + passages + ' Pause: vaelius-client pause.')
    if state == 'read-only':
        return ('Vaelius capture OFF (read-only scope file) for project ' + project +
                ': nothing is captured; shared memory recall stays on.')
    if state == 'paused':
        return 'Vaelius capture OFF (paused) for project ' + project + '. Resume: vaelius-client resume.'
    return 'Vaelius capture OFF for project ' + project + ' (capture disabled for this project).'


def on_enrollment(profile):
    """Shown once at login: nothing is captured until a project is enrolled."""
    try:
        config = json.loads((Path(profile) / 'config.json').read_text())
        notice = fetch(config, None, timeout=2)
    except (OSError, ValueError):
        notice = default_notice()
    try:
        record(profile, 'enrollment', notice, 'displayed_at_enrollment')
    except OSError:
        pass
    return text(notice) + '\nNothing is captured until you enroll a project (vaelius-client enroll-project).'
