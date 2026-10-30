"""`vaelius-client remember` and `import-memory`: explicit, owner-private memory.

A remembered item is text the person asks Vaelius to keep (agenthub.remember on the
service). It is private to them until they share it, versioned, removable and
audited. "Always load" items arrive at session start and with recall in sessions
whose audience admits them; for a private item, the person's private sessions.

`import-memory` turns a Claude Code auto-memory folder
(`~/.claude/projects/<project>/memory/`) into remembered items: every entry becomes
one item; `user` and `feedback` entries are "always load", `project` and
`reference` entries are not. An entry is stored in the workspace of the repository
the folder belongs to when that maps to an enrolled or scoped folder, otherwise as a
personal item (delivered in every workspace) in the workspace named by
`--workspace`. It is a dry run unless `--apply`: the dry run prints counts only,
never contents. Source files are never changed or deleted.
"""
import hashlib
import json
import os
from pathlib import Path
import re

ALWAYS_LOAD_TYPES = ('user', 'feedback')
TYPES = ('user', 'feedback', 'project', 'reference')
MAX_TEXT = 8192
MAX_FILES = 2000


def _client(config, timeout=15):
    from agentclient.transport import enterprise_client
    return enterprise_client(config, timeout=timeout)


def current_workspace(config, cwd=None):
    """The workspace of the current folder (as the hooks resolve it), or None."""
    from agentclient.hooks import resolve_scope
    try:
        scope = resolve_scope(config, {'cwd': str(Path(cwd or os.getcwd()).resolve())})
    except Exception:
        return None
    return scope['project'] if scope and scope.get('origin') != 'capture_all' else None


def request_key(*parts):
    return 'cli-' + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:40]


def add(config, text, *, workspace=None, title=None, always_load=False, personal=False, replace=None):
    """The person's own command applies directly (confirm=True); it is their review."""
    workspace = workspace or (None if replace else current_workspace(config))
    if not workspace and not replace:
        raise SystemExit(json.dumps({'error': 'workspace_required',
                                     'reason': 'run it in a workspace folder or pass --workspace'}))
    value = {'text': text, 'request_key': request_key('add', text, workspace, title, always_load, personal, replace),
             'always_load': always_load, 'personal': personal, 'confirm': True}
    for key, item in (('workspace', workspace), ('title', title), ('replace', replace)):
        if item:
            value[key] = item
    return _client(config).request('/enterprise/v3/remember', value)


def listing(config, *, workspace=None, include_withdrawn=False):
    value = {'include_withdrawn': include_withdrawn}
    if workspace:
        value['workspace'] = workspace
    return _client(config).request('/enterprise/v3/remember/list', value)


def remove(config, document_id):
    return _client(config).request('/enterprise/v3/remember/forget', {'document_id': document_id, 'confirm': True})


# Claude Code auto memory ---------------------------------------------------------------

FRONTMATTER = re.compile(r'\A---\s*\n(.*?)\n---\s*\n?', re.S)


def parse_entry(text):
    """(type, name, body) of one auto-memory file; type None when it has none."""
    match = FRONTMATTER.match(text)
    fields = {}
    if match:
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(':')
            if sep:
                fields[key.strip().lower()] = value.strip().strip('"\'')
        text = text[match.end():]
    kind = fields.get('type', '').lower()
    return (kind if kind in TYPES else None), fields.get('name') or fields.get('title'), text.strip()


def entries(folder):
    """[(path, type, name, body)] for the Markdown files of one auto-memory folder.

    `MEMORY.md` is the folder's index of the other files, not an entry."""
    folder = Path(folder)
    found = []
    for path in sorted(folder.glob('*.md'))[:MAX_FILES]:
        if path.name == 'MEMORY.md' or not path.is_file() or path.is_symlink():
            continue
        try:
            kind, name, body = parse_entry(path.read_text(errors='replace'))
        except OSError:
            continue
        if body:
            found.append((path, kind, name, body))
    return found


def folder_workspace(config, folder, home):
    """The workspace of the repository an auto-memory folder belongs to, or None.

    Claude Code names the folder after the project path with every non-alphanumeric
    character replaced by '-'; the enrolled and scoped roots are compared that way."""
    from agentclient.hooks import resolve_scope
    from agentclient.origin import slug
    from agentclient.scope_file import enrollments
    project_folder = Path(folder).parent.name if Path(folder).name == 'memory' else Path(folder).name
    roots = set(config.get('projects', {})) | set(enrollments(home))
    for root in sorted(roots):
        if slug(str(Path(root).resolve())) != project_folder and slug(root) != project_folder:
            continue
        try:
            scope = resolve_scope(config, {'cwd': str(Path(root).resolve())})
        except Exception:
            continue
        if scope and scope.get('origin') != 'capture_all':
            return scope['project']
    return None


def plan(config, home, folder, *, workspace=None):
    """The items an import would write, without contents in anything printed."""
    mapped = folder_workspace(config, folder, home)
    items = []
    for path, kind, name, body in entries(folder):
        text = body if len(body) <= MAX_TEXT else body[:MAX_TEXT]
        items.append({'path': path, 'type': kind or 'unknown', 'title': (name or '')[:180] or None, 'text': text,
                      'truncated': len(body) > MAX_TEXT, 'always_load': kind in ALWAYS_LOAD_TYPES,
                      'personal': mapped is None, 'workspace': mapped or workspace})
    return mapped, items


def import_memory(config, home, folder, *, workspace=None, apply=False):
    """Dry run (counts only) or apply; never reads or changes anything but `folder`."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise SystemExit(json.dumps({'error': 'memory_folder_not_found'}))
    mapped, items = plan(config, home, folder, workspace=workspace)
    counts = {'entries': len(items),
              'always_load': sum(1 for item in items if item['always_load']),
              'by_type': {kind: sum(1 for item in items if item['type'] == kind)
                          for kind in (*TYPES, 'unknown') if any(item['type'] == kind for item in items)},
              'truncated': sum(1 for item in items if item['truncated'])}
    result = {'dry_run': not apply, 'workspace': mapped, 'personal': mapped is None, 'counts': counts}
    if mapped is None and items and not workspace:
        result['note'] = ('this folder maps to no enrolled or scoped repository: its items would be personal; '
                          'pass --workspace to choose the workspace that stores them')
    if not apply:
        return result
    if mapped is None and items and not workspace:
        raise SystemExit(json.dumps({'error': 'workspace_required', 'reason': result['note']}))
    written = {'accepted': 0, 'duplicate': 0}
    client = _client(config)
    for item in items:
        value = {'text': item['text'], 'workspace': item['workspace'], 'always_load': item['always_load'],
                 'personal': item['personal'], 'confirm': True,
                 'request_key': request_key('import', str(item['path'].resolve()),
                                            hashlib.sha256(item['text'].encode()).hexdigest())}
        if item['title']:
            value['title'] = item['title']
        disposition = client.request('/enterprise/v3/remember', value).get('disposition')
        written[disposition if disposition in written else 'accepted'] += 1
    return dict(result, written=written)
