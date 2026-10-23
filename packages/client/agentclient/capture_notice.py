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
SHARING_CONTROL = 'vaelius-client sharing --set private|team|organization'
AUDIENCE = {'private': 'only you', 'team': 'members of this project',
            'organization': 'everyone in your organization'}


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
        if readers.get('passage_sharing_limit') in AUDIENCE:
            entries[key]['passage_sharing_limit'] = readers['passage_sharing_limit']
        if readers.get('personal_sharing') in AUDIENCE:
            value['personal_sharing'] = {'level': readers['personal_sharing'], 'at': time.time()}
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
    if not isinstance(entry.get('readers'), str):
        return {}
    readers = {'description': entry['readers']}
    if entry.get('passage_sharing_limit') in AUDIENCE:
        readers['passage_sharing_limit'] = entry['passage_sharing_limit']
    return {'readers': readers}


def personal_sharing(home):
    """The person's own sharing setting as last reported by the service, else None."""
    entry = read(home).get('personal_sharing') or {}
    return entry.get('level') if entry.get('level') in AUDIENCE else None


def remember_personal_sharing(home, level):
    """Keep the setting the service just confirmed, for the per-session status line."""
    if level not in AUDIENCE:
        raise ValueError('invalid_max_sharing')
    home = Path(home)
    with (home / 'notice.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read(home)
        value['personal_sharing'] = {'level': level, 'at': time.time()}
        with tempfile.NamedTemporaryFile(mode='w', dir=home, prefix='notice-', suffix='.tmp', delete=False) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        temporary.replace(_path(home))
    return level


def text(notice, project=None):
    """Plain message for the person: what is captured, who reads it, how to stop."""
    lines = ['Vaelius capture notice' + (' for project ' + project if project else '') + ':', notice['notice']]
    readers = notice.get('readers')
    if isinstance(readers, dict) and isinstance(readers.get('description'), str):
        lines.append('Readers for this project: ' + readers['description'] + '.')
    if isinstance(readers, dict) and readers.get('personal_sharing') in AUDIENCE:
        limit = readers.get('passage_sharing_limit')
        lines.append('Your sharing setting: ' + readers['personal_sharing'] + '. Your conversation passages here '
                     'are shared with at most ' + AUDIENCE.get(limit, 'only you') + ', and only as far as the '
                     "repository's scope file asks; unredacted originals stay yours unless it also asks for them. "
                     'Change: ' + SHARING_CONTROL + '.')
    if notice.get('policy_url'):
        lines.append('Policy: ' + notice['policy_url'])
    lines.append(CONTROLS)
    return '\n'.join(lines)


def status(state, project, notice=None, *, sharing='private', personal=None):
    """One line per session: is capture on or off for this project, and how to change it."""
    if state == 'on':
        readers = (notice or {}).get('readers') or {}
        who = readers.get('description') if isinstance(readers.get('description'), str) else 'set by server policy'
        # A scope file's request; the person's setting and operator policy decide what is shared.
        passages = (' Conversation passages: ' + sharing + ' sharing requested by the repository scope file; '
                    'your setting and the service decide.' if sharing in ('team', 'organization') else '')
        # A freshly fetched notice carries the setting; later sessions use the remembered one.
        personal = readers.get('personal_sharing') if readers.get('personal_sharing') in AUDIENCE else personal
        mine = (' Your sharing setting: ' + personal + ' (' + SHARING_CONTROL + ').') if personal in AUDIENCE else ''
        return ('Vaelius capture ON for project ' + project + ': prompts, assistant replies and tool '
                'calls/results are shared (readers: ' + who + ').' + passages + mine + ' Pause: vaelius-client pause.')
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
