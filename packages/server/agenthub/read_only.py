"""Read-only chats, enforced by the service (docs/ACCESS_MODEL.md section 7).

A chat is read-only when the person switched it (`vaelius-client session read-only`,
or the MCP tool `read_only_session` when they ask), or when it runs in a folder whose
scope file says `capture = "read-only"`. Nothing from it is written any more, not even
private items. The client stops capturing; the service refuses every write route
(captures, boundaries, receipts, remember, feedback, corrections, withdrawals, shares,
workspace administration) for:

* a request whose `X-Vaelius-Read-Only` header is `1`, or whose
  `X-Vaelius-Session-Label` says `read_only` (the MCP connection of such a chat);
* a request that names a session id registered here (`POST
  /enterprise/v3/sessions/read-only`): the MCP connection id (`X-AgentNetwork-Session`),
  a capture event's `conversation`, a boundary's `session_id` or a receipt's `session`.

Reads keep working. Registration is one-way: there is no route that removes a row, so
only a new chat (or `/clear`, which starts a new session id) writes again.
"""
import hashlib
import json
import time

MESSAGE = ('vaelius: this chat is read-only — nothing from it is saved, so Vaelius refused this write. '
           'Start a new chat (or /clear) to save again.')
# What the person sees when a chat switches to read-only.
STATUS = 'vaelius: read-only — nothing from this chat is saved'
# Credential actions that only read; a route whose rule can be met with these alone is not a write.
READ_ACTIONS = frozenset({'read', 'source_read', 'audit'})
REGISTRATION = '/enterprise/v3/sessions/read-only'
MAX_SESSIONS = 8
RETENTION = 30 * 86400


class ReadOnlySession(Exception):
    """A write from a read-only chat (HTTP 403 `read_only_session`)."""
    code = 'read_only_session'
    message = MESSAGE


def is_write(rule, path):
    """True for a route that changes stored state; registration itself only narrows."""
    if path == REGISTRATION or rule is None:
        return False
    return not any(set(group) <= READ_ACTIONS for group in rule)


def _digest(tenant, principal, session):
    return hashlib.sha256(json.dumps([tenant, principal, str(session)], separators=(',', ':')).encode()).hexdigest()


def header_read_only(headers):
    """The request says it comes from a read-only chat (header or label)."""
    if (headers.get('x-vaelius-read-only') or '').strip() == '1':
        return True
    label = headers.get('x-vaelius-session-label')
    if label and len(label) <= 4096:
        try:
            value = json.loads(label)
        except ValueError:
            return False
        return isinstance(value, dict) and value.get('read_only') is True
    return False


def named_sessions(path, data, headers):
    """Session ids a write names: the MCP connection, a capture, a boundary or a receipt."""
    found = [headers.get('x-agentnetwork-session')]
    if isinstance(data, dict):
        if path in ('/enterprise/v1/receipts', '/enterprise/v1/sources'):
            found.append(data.get('session'))
        if path == '/enterprise/v1/boundary' and isinstance(data.get('event'), dict):
            found.append(data['event'].get('session_id'))
    return [s for s in found if isinstance(s, str) and 0 < len(s) <= 256]


class ReadOnlySessionsMixin:
    """Registration and lookup of read-only chats for one tenant store."""

    def mark_read_only(self, ctx, value):
        """Register the caller's chat session ids as read-only (one-way; needs `read`)."""
        if (not isinstance(value, dict) or set(value) != {'sessions'} or not isinstance(value['sessions'], list)
                or not 1 <= len(value['sessions']) <= MAX_SESSIONS
                or any(not isinstance(s, str) or not 0 < len(s) <= 256 for s in value['sessions'])):
            raise ValueError('invalid_request')
        self._need(ctx, 'read')
        now = time.time()
        with self.open() as state, state.db:
            db = state.db
            db.execute('DELETE FROM read_only_sessions WHERE created<?', (now - RETENTION,))
            for session in dict.fromkeys(value['sessions']):
                db.execute('''INSERT INTO read_only_sessions(tenant,principal,session,created) VALUES(?,?,?,?)
                    ON CONFLICT(tenant,principal,session) DO NOTHING''',
                           (ctx['tenant'], ctx['principal'], _digest(ctx['tenant'], ctx['principal'], session), now))
            self._audit(db, ctx, 'session_read_only', 'registered', None, len(value['sessions']))
        return {'read_only': True, 'sessions': len(set(value['sessions'])), 'message': MESSAGE}

    def read_only_registered(self, ctx, sessions, db=None):
        """True when any of these session ids was registered read-only by the caller."""
        digests = sorted({_digest(ctx['tenant'], ctx['principal'], s) for s in sessions if s})
        if not digests:
            return False
        def query(handle):
            return bool(handle.execute('''SELECT 1 FROM read_only_sessions WHERE tenant=? AND principal=?
                AND session=ANY(?::text[]) LIMIT 1''', (ctx['tenant'], ctx['principal'], digests)).fetchone())
        if db is not None:
            return query(db)
        with self.open() as state:
            return query(state.db)

    def refuse_read_only(self, ctx, sessions, db=None):
        if self.read_only_registered(ctx, sessions, db):
            raise ReadOnlySession()
