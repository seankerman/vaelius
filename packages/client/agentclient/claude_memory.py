"""Claude Code's built-in auto memory, switched off in captured repositories.

Auto memory (`~/.claude/projects/<project>/memory/`, whose `MEMORY.md` Claude Code
loads at session start) sits outside the Vaelius access model: text a narrowed or
private session read can be written there and then loaded, unlabelled, into a later
shared session in the same repository and captured for its audience. Vaelius
remembered items (`vaelius-client remember`) replace it.

Claude Code's documented switches (code.claude.com/docs/en/settings-reference and
/env-vars): the settings key `autoMemoryEnabled` (boolean; `false` stops reading and
writing the auto memory folder, CLAUDE.md files are unaffected) in any settings
file, and the environment variable `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, which wins over
the key for a session in either direction (`1` off, `0` on). The docs do not say
whether a managed `false` can be overridden by that variable.

`vaelius-client install --claude-code` writes `autoMemoryEnabled: false` into
`<root>/.claude/settings.local.json` of each enrolled or captured project (per
person, per repository, not committed). The receipt records the value it replaced,
and uninstall puts that value back while the key still holds what the installer
wrote; a value the person set afterwards is theirs and is left alone.
"""
import json
import os
from pathlib import Path

KEY = 'autoMemoryEnabled'
ENV = 'CLAUDE_CODE_DISABLE_AUTO_MEMORY'
NOTICE_KEY = 'auto_memory_notice'
ABSENT = {'absent': True}
TRUE = ('1', 'true', 'yes', 'on')
FALSE = ('0', 'false', 'no', 'off')


def settings_file(root):
    return Path(root) / '.claude' / 'settings.local.json'


def enabled(cwd, env=None):
    """Whether Claude Code's auto memory is on for a session started in `cwd`.

    The environment variable first, then the highest-precedence settings file that
    sets the key (managed, local, project, user); on when nothing sets it."""
    from agentclient.claude_guardrails import _documents
    env = os.environ if env is None else env
    value = str(env.get(ENV, '')).strip().lower()
    if value in TRUE:
        return False
    if value in FALSE:
        return True
    for _, document in _documents(cwd, env):
        if isinstance(document.get(KEY), bool):
            return document[KEY]
    return True


def status_line(cwd, env=None):
    """One line for the person when auto memory is on in a captured repository, else None."""
    try:
        if not enabled(cwd, env):
            return None
    except Exception:
        return None
    return ('vaelius: Claude Code auto memory is on in this captured repository; its notes are outside Vaelius '
            'access rules. `vaelius-client install --claude-code` switches it off here.')


def apply(root, records, backup):
    """Write `autoMemoryEnabled: false` into one project's local settings.

    Returns (record, kept_on). `records` holds this installation's earlier records by
    file. A file it already wrote keeps its first record, so uninstall restores the
    value from before the first install; a value the person set after that install
    (`kept_on`) is theirs and is left as it is."""
    from agentclient.claude_code import _read_json, _write_json
    path = settings_file(root)
    settings = _read_json(path)
    previous = records.get(str(path))
    if previous is not None and settings is not None and settings.get(KEY, ABSENT) is not False:
        return previous, True
    record = previous or {'prior': (settings or {}).get(KEY, ABSENT), 'created_settings': settings is None,
                          'created_folder': not path.parent.exists(), 'project_root': str(root)}
    settings = {} if settings is None else settings
    if settings.get(KEY) is not False:
        backup(path)
        settings[KEY] = False
        _write_json(path, settings)
    return record, False


def restore(path, record):
    """Put back what `apply` replaced, if the key still holds the installer's `false`."""
    from agentclient.claude_code import _read_json, _write_json
    path = Path(path)
    settings = _read_json(path)
    if settings is None or settings.get(KEY, ABSENT) is not False:
        return False
    prior = record.get('prior', ABSENT)
    if prior == ABSENT:
        settings.pop(KEY)
    else:
        settings[KEY] = prior
    if not settings and record.get('created_settings'):
        path.unlink()
        if record.get('created_folder'):
            try:
                path.parent.rmdir()  # only when nothing else was put there since
            except OSError:
                pass
    else:
        _write_json(path, settings)
    return True
