"""Capture notice and per-session capture status shown to the person being captured.

The service supplies the organization's own wording and policy link
(`/enterprise/v3/capture-notice`). Without a reachable service the client shows
its built-in notice. Displays and acknowledgements are recorded in the profile's
`notice-acknowledgements.json`, next to `config.json`, so capture never has to
rewrite `config.json` (and can never race a concurrent `pause`).

The notice wording now follows the workspace access model
(`docs/ACCESS_MODEL.md`): a person's own setting is `contributions_default`
(`shared` or `private`) and items inherit the workspace's audience; the per-item
`private`/`team`/`organization` ladder goes away.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

DEFAULT_NOTICE = (
    'This workspace is enrolled in Vaelius shared memory. While capture is on, your prompts, '
    'the assistant\'s replies, and tool calls with their results are sent to your organization\'s '
    'Vaelius service after local secret redaction. Who can read them is decided by the service: '
    'items inherit the workspace\'s audience (the people and groups on its access list) unless you '
    'opt a specific item out as private. Pause with `vaelius-client pause`; withdraw an item you '
    'captured with the withdraw_memory tool.')
CAPTURED = ['user_prompts', 'assistant_replies', 'tool_calls', 'tool_results']
FILE = 'notice-acknowledgements.json'
CONTROLS = 'Pause: vaelius-client pause. Resume: vaelius-client resume.'
SHARING_CONTROL = 'vaelius-client sharing --set shared|private'
# The two states the person's own `contributions_default` can hold under the
# access model. Older services (`max_sharing`) are translated: `private`
# stays private, anything else is treated as `shared`.
AUDIENCE = {'shared': 'the workspace audience (people and groups on its access list)',
            'private': 'only you'}


def normalize_contributions_default(value):
    """Collapse a legacy or new value to the two-state `contributions_default`.

    `private` and `shared` stay as themselves; the legacy `team` and
    `organization` map to `shared` (the person's contributions inherit the
    workspace audience), anything else returns None.
    """
    if value in AUDIENCE:
        return value
    if value in ('team', 'organization'):
        return 'shared'
    return None


def default_notice():
    return {'notice': DEFAULT_NOTICE, 'policy_url': None, 'captured': CAPTURED,
            'notice_version': hashlib.sha256(json.dumps([DEFAULT_NOTICE, None]).encode()).hexdigest()[:16],
            'source': 'client_default'}


def fetch(config, project, *, timeout=0.5):
    """Server wording for this workspace's capture connection, else the built-in notice."""
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
    """Record that the notice for `scope` ('enrollment' or a workspace) was shown/acknowledged."""
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
        personal = normalize_contributions_default(readers.get('personal_sharing'))
        if personal is not None:
            value['personal_sharing'] = {'level': personal, 'at': time.time()}
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
    """The last notice shown for a workspace, as a notice-shaped dict for `status`."""
    entry = read(home).get('projects', {}).get(project) or {}
    if not isinstance(entry.get('readers'), str):
        return {}
    return {'readers': {'description': entry['readers']}}


def personal_sharing(home):
    """The person's own `contributions_default` as last reported by the service, else None."""
    entry = read(home).get('personal_sharing') or {}
    return entry.get('level') if entry.get('level') in AUDIENCE else None


def remember_personal_sharing(home, level):
    """Keep the setting the service just confirmed, for the per-session status line."""
    canonical = normalize_contributions_default(level)
    if canonical is None:
        raise ValueError('invalid_contributions_default')
    home = Path(home)
    with (home / 'notice.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read(home)
        value['personal_sharing'] = {'level': canonical, 'at': time.time()}
        with tempfile.NamedTemporaryFile(mode='w', dir=home, prefix='notice-', suffix='.tmp', delete=False) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        temporary.replace(_path(home))
    return canonical


def text(notice, project=None):
    """Plain message for the person: what is captured, who reads it, how to stop."""
    lines = ['Vaelius capture notice' + (' for workspace ' + project if project else '') + ':', notice['notice']]
    readers = notice.get('readers')
    if isinstance(readers, dict) and isinstance(readers.get('description'), str):
        lines.append('Readers for this workspace: ' + readers['description'] + '.')
    if isinstance(readers, dict):
        personal = normalize_contributions_default(readers.get('personal_sharing'))
        if personal is not None:
            lines.append('Your sharing setting: ' + personal + '. Items you capture inherit ' +
                         AUDIENCE[personal] + ' unless you share them explicitly. '
                         'Change: ' + SHARING_CONTROL + '.')
    if notice.get('policy_url'):
        lines.append('Policy: ' + notice['policy_url'])
    lines.append(CONTROLS)
    return '\n'.join(lines)


def status(state, project, notice=None, *, sharing='private', personal=None):
    """One line per session: is capture on or off for this workspace, and how to change it."""
    if state == 'on':
        readers = (notice or {}).get('readers') or {}
        who = readers.get('description') if isinstance(readers.get('description'), str) else 'the workspace audience'
        # A scope file with the new `workspace` key never sends sharing requests
        # on the wire. A legacy file may still request team/organization; the
        # service applies the person's setting and the workspace access list.
        passages = ''
        if sharing in ('team', 'organization'):
            passages = (' Legacy scope-file sharing request: ' + sharing + '; '
                        'the service applies your setting and the workspace access list.')
        personal_fetched = normalize_contributions_default(readers.get('personal_sharing'))
        if personal_fetched is not None:
            personal = personal_fetched
        mine = (' Your sharing setting: ' + personal + ' (' + SHARING_CONTROL + ').') if personal in AUDIENCE else ''
        return ('Vaelius capture ON for workspace ' + project + ': prompts, assistant replies and tool '
                'calls/results are shared (readers: ' + who + ').' + passages + mine + ' Pause: vaelius-client pause.')
    if state == 'read-only':
        return ('Vaelius capture OFF (read-only scope file) for workspace ' + project +
                ': nothing is captured; shared memory recall stays on.')
    if state == 'paused':
        return 'Vaelius capture OFF (paused) for workspace ' + project + '. Resume: vaelius-client resume.'
    return 'Vaelius capture OFF for workspace ' + project + ' (capture disabled for this workspace).'


def on_enrollment(profile):
    """Shown once at login: nothing is captured until a workspace is enrolled."""
    try:
        config = json.loads((Path(profile) / 'config.json').read_text())
        notice = fetch(config, None, timeout=2)
    except (OSError, ValueError):
        notice = default_notice()
    try:
        record(profile, 'enrollment', notice, 'displayed_at_enrollment')
    except OSError:
        pass
    return text(notice) + '\nNothing is captured until you enroll a workspace (vaelius-client enroll-project).'
