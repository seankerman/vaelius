"""Explicit "remember" items: what a person, or their agent at their request, asks
Vaelius to keep (docs/MEMORY_GOVERNANCE.md, "Remembered items").

A remembered item is the same item as an owner-reviewed note
(`EnterpriseStore._install_owner_note`): an owner-private source holding the
person's own words, and one curated document over it whose audience follows the
source. Nothing here adds a permission path:

* writing needs only the person's own credential with `ingest` (the action that
  already creates their content) and the writer role in the workspace it is stored
  in; it never needs `curate`, because the person's explicit command is the review;
* the item starts owner-private and is recalled only where the existing audience
  rules admit it (the owner's private sessions, per the session-label rule); sharing
  it later is the ordinary owner sharing of its source (`vaelius-client share`,
  `change_memory_sharing`);
* a new version is a lifecycle correction of the source (same document, new
  revision) and removal is a lifecycle withdrawal, both audited and journaled.
  They are applied here for the item's owner only, so `ingest` suffices for them;
* "always load" items are delivered with the person's preferences at session start
  and with recall (`always_load_cards`), still through `_document_allowed`, so a
  session whose audience does not admit an item never receives it.

`remembered_items` holds metadata only (agenthub/migrations/051_remembered_items.sql).
"""
import hashlib
import json
import re
import time

MAX_TEXT = 8192
# The document carries the first part; the source (fetch_memory) carries all of it.
EXCERPT = 1200
DELIVERED_EXCERPT = 300
LIST_LIMIT = 200
KEY = re.compile(r'[A-Za-z0-9_-]{1,100}')
NAME = re.compile(r'[A-Za-z0-9_-]{1,120}')
DOCUMENT = re.compile(r'[A-Za-z0-9_:-]{1,128}')
ADD_FIELDS = {'text', 'title', 'workspace', 'always_load', 'personal', 'replace', 'request_key', 'confirm'}


def derive_title(text):
    """The first non-empty line, without Markdown heading marks, at most 120 characters."""
    line = next((item.strip() for item in text.splitlines() if item.strip()), '')
    line = re.sub(r'^#+\s*', '', line) or 'Remembered note'
    return line if len(line) <= 120 else line[:117].rstrip() + '...'


def excerpt(text, size=EXCERPT):
    if len(text) <= size:
        return text
    return text[:size - 40].rstrip() + ' [excerpt; fetch_memory reads the rest]'


def _flag(value, name):
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError('invalid_remember_' + name)
    return value


def _confirm(value):
    confirm = value.get('confirm', False)
    if confirm is not True and confirm is not False and not (isinstance(confirm, str) and len(confirm) == 32):
        raise ValueError('invalid_confirm')
    return confirm


def _owner_action(ctx, action):
    """The lifecycle action for one operation on the caller's own remembered item.

    Only `remember` and `forget_remembered` call this, after checking that the item
    is a remembered item the caller owns; lifecycle re-checks ownership itself."""
    return dict(ctx, actions=set(ctx['actions']) | {action})


class RememberMixin:
    def _remembered_row(self, db, ctx, document_id):
        from agenthub.enterprise import Denied
        if not isinstance(document_id, str) or not DOCUMENT.fullmatch(document_id):
            raise ValueError('invalid_remembered_item')
        row = db.execute('''SELECT r.*,s.source_version,s.active source_active FROM remembered_items r
            JOIN enterprise_sources s ON s.id=r.source_id
            WHERE r.document_id=? AND r.tenant=? AND r.owner=? AND r.withdrawn IS NULL''',
                         (document_id, ctx['tenant'], ctx['actor'])).fetchone()
        if not row or not row['source_active']:
            raise Denied()
        return row

    def remember(self, ctx, value):
        """Add a remembered item, or a new version of one (`replace`).

        `confirm`: False previews and returns a `confirm_token`; the token (an MCP
        call after the person agreed) or True (the person's own CLI command) applies."""
        from agenthub.enterprise import Denied, _digest
        from agenthub.owner_controls import confirm_token
        from agentclient.enterprise_contract import VERSION
        self._need(ctx, 'ingest')
        if not isinstance(value, dict) or set(value) - ADD_FIELDS:
            raise ValueError('invalid_remember_request')
        text = value.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError('invalid_remember_text')
        title = value.get('title') or derive_title(text)
        if not isinstance(title, str) or not 1 <= len(title) <= 180:
            raise ValueError('invalid_remember_title')
        key = value.get('request_key')
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError('invalid_request_key')
        always_load = _flag(value.get('always_load'), 'always_load')
        personal = _flag(value.get('personal'), 'personal')
        confirm = _confirm(value)
        replace = value.get('replace')
        workspace = value.get('workspace')
        digest = _digest(json.dumps([ctx['tenant'], ctx['actor'], key]))
        with self.open() as state:
            db = state.db
            self.current_identity(db, ctx)
            done = db.execute('SELECT * FROM remembered_items WHERE tenant=? AND owner=? AND request_digest=?',
                              (ctx['tenant'], ctx['actor'], digest)).fetchone()
            if done:
                return {'disposition': 'duplicate', 'document_id': done['document_id'],
                        'source_id': done['source_id'], 'private_owner': ctx['actor']}
            target = self._remembered_row(db, ctx, replace) if replace is not None else None
            if target is not None:
                workspace = workspace or target['workspace']
                if workspace != target['workspace']:
                    raise ValueError('replace_keeps_the_workspace')
            if not isinstance(workspace, str) or not NAME.fullmatch(workspace):
                raise ValueError('invalid_remember_workspace')
            # Writing needs the writer role there, as capture does.
            if not self._project_member(db, ctx, workspace, 2):
                raise Denied()
            sharing = 'private'
            if target is not None:
                source = db.execute('SELECT private FROM enterprise_sources WHERE id=?', (target['source_id'],)).fetchone()
                sharing = 'private' if source['private'] else 'shared'
        parameters = {'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'title': title,
                      'workspace': workspace, 'always_load': always_load, 'personal': personal,
                      'replace': replace, 'request_key': key}
        token = confirm_token(ctx, 'remember', parameters, [])
        preview = {'dry_run': True, 'operation': 'replace' if replace else 'add', 'title': title,
                   'chars': len(text), 'workspace': workspace, 'personal': personal, 'always_load': always_load,
                   'sharing': sharing, 'replaces': replace, 'confirm_token': token}
        if confirm is not True and confirm != token:
            return dict(preview, status='selection_changed') if confirm else preview
        now = time.time()
        external = 'remember-' + digest[:48]
        event = {'version': VERSION, 'external_id': external, 'session': 'remember:' + workspace,
                 'turn': digest[:64], 'project': workspace, 'kind': 'Stop', 'body': text,
                 'occurred_at': int(now), 'visibility': 'private', 'speaker': 'user'}
        if target is not None:
            result = self.lifecycle(_owner_action(ctx, 'correct'), {
                'version': VERSION, 'target_id': target['source_id'],
                'expected_revision': str(target['source_version']), 'idempotency_key': external,
                'reason': 'explicit owner remember: new version', 'operation': 'correct',
                'replacement': {'source': event, 'title': title, 'lesson': excerpt(text)}})
            source_id = result['replacement_source_id']
            with self.delivery_lock(), self.open() as state, state.db:
                state.db.execute('''UPDATE remembered_items SET source_id=?,always_load=?,personal=?,updated=?
                    WHERE document_id=?''', (source_id, int(always_load), int(personal), now, replace))
                self._audit(state.db, ctx, 'remember', 'corrected', replace)
            return {'disposition': 'replaced', 'document_id': replace, 'source_id': source_id,
                    'private_owner': ctx['actor'], 'sharing': sharing}
        with self.open() as state:
            # A retry after the source was stored but before the note: reuse the source.
            prior = state.db.execute('SELECT id FROM enterprise_sources WHERE tenant=? AND enrollment=? AND external_id=?',
                                     (ctx['tenant'], ctx['enrollment'], external)).fetchone()
        source_id = prior['id'] if prior else self.ingest(ctx, event)['source_id']
        with self.open() as state, state.db:
            # Capture keeps only bodies of 20+ characters as source records; a short
            # remembered sentence ("Prefer pytest.") is still the person's whole item.
            session = _digest(json.dumps([ctx['tenant'], ctx['enrollment'], event['session']]))
            state.db.execute('''INSERT INTO memories(id,session,project,body,kind,created,turn)
                SELECT s.id,?,s.internal_project,?,'Stop',?,? FROM enterprise_sources s WHERE s.id=?
                ON CONFLICT DO NOTHING''', (session, text, now, _digest(json.dumps([session, event['turn']])),
                                            source_id))

        def record(db, result):
            db.execute('''INSERT INTO remembered_items(document_id,tenant,owner,workspace,personal,always_load,
                source_id,request_digest,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       (result['document_id'], ctx['tenant'], ctx['actor'], workspace, int(personal),
                        int(always_load), source_id, digest, now, now))
            self._audit(db, ctx, 'remember', 'accepted', result['document_id'])
        result = self._install_owner_note(ctx, source_id, title, excerpt(text), minimum=1, record=record)
        return {'disposition': 'accepted', 'document_id': result['document_id'], 'source_id': source_id,
                'private_owner': ctx['actor'], 'sharing': 'private'}

    def forget_remembered(self, ctx, value):
        """Withdraw one of the caller's remembered items; two-step like `remember`."""
        from agenthub.owner_controls import confirm_token
        from agentclient.enterprise_contract import VERSION
        self._need(ctx, 'ingest')
        if not isinstance(value, dict) or set(value) - {'document_id', 'confirm'}:
            raise ValueError('invalid_forget_remembered_request')
        confirm = _confirm(value)
        with self.open() as state:
            self.current_identity(state.db, ctx)
            row = self._remembered_row(state.db, ctx, value.get('document_id'))
        token = confirm_token(ctx, 'forget_remembered', {'document_id': row['document_id']},
                              [{'id': row['source_id'], 'source_version': row['source_version']}])
        preview = {'dry_run': True, 'document_id': row['document_id'], 'workspace': row['workspace'],
                   'confirm_token': token}
        if confirm is not True and confirm != token:
            return dict(preview, status='selection_changed') if confirm else preview
        self.lifecycle(_owner_action(ctx, 'withdraw'), {
            'version': VERSION, 'target_id': row['source_id'], 'expected_revision': str(row['source_version']),
            'idempotency_key': 'forget-remembered-' + token, 'reason': 'explicit owner remember: removed',
            'operation': 'withdraw'})
        with self.delivery_lock(), self.open() as state, state.db:
            state.db.execute('UPDATE remembered_items SET withdrawn=? WHERE document_id=? AND withdrawn IS NULL',
                             (time.time(), row['document_id']))
            self._audit(state.db, ctx, 'remember', 'withdrawn', row['document_id'])
        return {'disposition': 'withdrawn', 'document_id': row['document_id']}

    def remembered(self, ctx, value):
        """The caller's own remembered items (metadata and title, never another person's)."""
        self._need(ctx, 'read')
        value = value or {}
        if not isinstance(value, dict) or set(value) - {'workspace', 'include_withdrawn'}:
            raise ValueError('invalid_remembered_request')
        workspace = value.get('workspace')
        if workspace is not None and (not isinstance(workspace, str) or not NAME.fullmatch(workspace)):
            raise ValueError('invalid_remember_workspace')
        with self.open() as state:
            db = state.db
            self.current_identity(db, ctx)
            rows = db.execute('''SELECT r.*,s.private,s.active source_active,kr.claim_json,length(m.body) chars
                FROM remembered_items r JOIN enterprise_sources s ON s.id=r.source_id
                LEFT JOIN memories m ON m.id=r.source_id
                LEFT JOIN knowledge_documents kd ON kd.document_id=r.document_id
                LEFT JOIN knowledge_revisions kr ON kr.revision_id=kd.active_revision_id
                WHERE r.tenant=? AND r.owner=? AND (? OR r.withdrawn IS NULL)
                AND (?::text IS NULL OR r.workspace=?) ORDER BY r.updated DESC LIMIT ?''',
                              (ctx['tenant'], ctx['actor'], bool(value.get('include_withdrawn')), workspace,
                               workspace, LIST_LIMIT)).fetchall()
        items = []
        for row in rows:
            claim = json.loads(row['claim_json']) if row['claim_json'] else {}
            sharing = 'private' if row['private'] else 'shared'
            items.append({'document_id': row['document_id'], 'source_id': row['source_id'],
                          'workspace': row['workspace'], 'personal': bool(row['personal']),
                          'always_load': bool(row['always_load']), 'title': claim.get('title'),
                          'chars': row['chars'], 'sharing': sharing, 'created': row['created'],
                          'updated': row['updated'], 'withdrawn': row['withdrawn'] is not None,
                          # The origin label: an agent that lists these carries them for taint.
                          'origin': {'project': row['workspace'],
                                     'sharing': 'private' if row['private'] else 'team'}})
        return {'items': items, 'private_owner': ctx['actor']}

    def always_load_cards(self, db, ctx, project, *, session=None):
        """"Always load" items for a session in `project`: this workspace's items and the
        caller's personal ones, each only if `_document_allowed` admits it for the
        request's session label. With `session` (automatic delivery), items already
        offered in the session's current context epoch are skipped."""
        rows = db.execute('''SELECT r.document_id FROM remembered_items r
            JOIN enterprise_sources s ON s.id=r.source_id AND s.active=1
            WHERE r.tenant=? AND r.withdrawn IS NULL AND r.always_load=1
            AND ((r.workspace=? AND (r.owner=? OR s.private=0)) OR (r.personal=1 AND r.owner=?))
            ORDER BY r.updated DESC LIMIT 20''', (ctx['tenant'], project, ctx['actor'], ctx['actor'])).fetchall()
        receiver = epoch = None
        if session:
            from agenthub.processing.context_delivery import current_epoch
            receiver = self._receiver(ctx, session)
            epoch = str(current_epoch(db, receiver)['epoch'])
        cards = []
        for row in rows:
            doc = self._document_allowed(db, ctx, row['document_id'])
            if not doc:
                continue
            if receiver and db.execute('''SELECT 1 FROM enterprise_receipts WHERE tenant=? AND principal=?
                    AND session=? AND epoch=? AND document_id=? AND revision=?''',
                                       (ctx['tenant'], ctx['principal'], receiver, epoch, row['document_id'],
                                        doc['active_revision_id'])).fetchone():
                continue
            claim = json.loads(db.execute('SELECT claim_json FROM knowledge_revisions WHERE revision_id=?',
                                          (doc['active_revision_id'],)).fetchone()[0])
            lesson = str(claim.get('lesson', ''))
            card = {'id': row['document_id'], 'revision': doc['active_revision_id'],
                    'title': str(claim.get('title', '')), 'lesson': excerpt(lesson, DELIVERED_EXCERPT),
                    'evidence_status': 'remembered', 'always_load': True}
            if len(lesson) > DELIVERED_EXCERPT:
                card['excerpt_truncated'] = True
            cards.append(card)
        if not cards:
            return []
        from agenthub.provenance import decorate_cards
        return decorate_cards(db, cards, project)
