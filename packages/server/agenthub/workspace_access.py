"""Workspaces, groups, access lists and the read predicate (docs/ACCESS_MODEL.md).

A workspace (stored in `enterprise_projects`, same ids as the projects it
replaces) has an access list: entries of a subject (`principal`, `group` or the
whole `organization`) and a role (`reader` < `writer` < `admin`). A principal's
role is the highest any entry gives them, directly, through any group they belong
to (`group_closure`, materialized) or through the organization entry. Entries
wider than the tenant's `widest_entry` are suspended at query time.

The audience of an item (a captured source and everything derived from it) is:

    {owner} ∪ ( ( (workspace audience  if not private) ∪ share grantees )
                ∩ ⋂ carried workspace audiences )
    — and nobody but the owner when origin_private

implemented three times and kept in agreement by parity tests:

1. this module (`WorkspaceAccessMixin._in_audience`, `_shared_with`), used by
   `EnterpriseStore._visible_source` and `_document_allowed`;
2. `agenthub.search_permissions` (the indexed SQL filter);
3. `search_reader_in_audience` and `search_document_allowed` (row-level
   security, migration 048).

Effective actions in a workspace are the credential's actions intersected with
those implied by the caller's role there (`ROLE_ACTIONS`). Owners keep every
self-service right over their own items whatever their role; widening (sharing,
clearing `private`) needs the `writer` role.

Nothing about access is cached between requests: every check reads the current
tables. Operator methods take no request context and audit as `local_operator`.
"""
from contextlib import nullcontext
import json
import re
import time

ROLES = ('reader', 'writer', 'admin')
RANK = {'reader': 1, 'writer': 2, 'admin': 3}
READER, WRITER, ADMIN = 1, 2, 3
SUBJECT_KINDS = ('principal', 'group', 'organization')
WIDEST = ('none', 'people_and_groups', 'organization')
CREATION = ('anyone', 'admins')
ITEM_DEFAULTS = ('shared', 'private')
# Chosen when a workspace is created, and changed later only on the server.
VISIBILITIES = ('private', 'org')
DEFAULT_VISIBILITY = 'org'
ORG_NOTICE = 'visible to everyone in the org (use --private to restrict)'
GROUP_ROLES = ('member', 'admin')
NAME = re.compile(r'[A-Za-z0-9_-]{1,120}')
GROUP_NAME = re.compile(r'[A-Za-z0-9_-]{1,128}')
OPERATOR = 'local_operator'
# Actions a role implies inside one workspace (credential actions ∩ these).
ROLE_ACTIONS = {
    READER: frozenset({'read', 'feedback', 'source_read'}),
    WRITER: frozenset({'read', 'feedback', 'source_read', 'ingest', 'correct', 'curate', 'withdraw', 'share'}),
    ADMIN: frozenset({'read', 'feedback', 'source_read', 'ingest', 'correct', 'curate', 'withdraw', 'share',
                      'moderate', 'manage'}),
}
# Legacy sharing ladder, still read from older clients and reports.
SHARING = ('private', 'team', 'organization')


class RepoNotLinked(Exception):
    """A capture's remote is not linked to the named workspace (HTTP 409 repo_not_linked)."""
    code = 'repo_not_linked'


def _origin(item):
    """(ceiling, reason, projects) of a v2 event (`origin_label`) or a stored source row."""
    if not isinstance(item, dict):
        return 'organization', None, ()
    label = item.get('origin_label')
    if isinstance(label, dict):
        return label.get('ceiling'), label.get('reason') or None, tuple(label.get('projects') or ())
    projects = item.get('origin_projects') or ()
    if isinstance(projects, str):
        projects = json.loads(projects)
    return item.get('origin_ceiling') or 'organization', item.get('origin_reason'), tuple(projects)


def origin_ceiling(item=None):
    """'private' (origin_private), 'team' (legacy members-only origin) or 'organization' (no origin limit)."""
    value = _origin(item)[0]
    return value if value in SHARING else 'private'


def origin_projects(item=None):
    """Carried workspaces: other workspaces whose content an event or source carries."""
    return _origin(item)[2]


def lowest(*levels):
    """The most restrictive of the given legacy levels; anything unknown counts as private."""
    rank = {level: index for index, level in enumerate(SHARING)}
    return SHARING[min(rank.get(level, 0) for level in levels)] if levels else 'private'


def role_name(rank):
    return {READER: 'reader', WRITER: 'writer', ADMIN: 'admin'}.get(rank)


def _role_rank(value):
    if value not in RANK:
        raise ValueError('invalid_role')
    return RANK[value]


class WorkspaceAccessMixin:
    # Roles ------------------------------------------------------------------------

    _ROLES_SQL = '''SELECT p.id,
            CASE o.widest_entry WHEN 'none' THEN CASE WHEN a.principal_role=3 THEN 3 ELSE 0 END
                ELSE greatest(a.principal_role,a.group_role) END AS direct,0 AS organization
        FROM workspace_access a JOIN enterprise_projects p ON p.tenant=a.tenant AND p.id=a.workspace AND p.active=1
        JOIN enterprise_organizations o ON o.id=a.tenant
        WHERE a.tenant=? AND a.principal=?{one}
        UNION ALL
        SELECT p.id,0,p.org_role FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
        WHERE p.tenant=? AND p.active=1 AND p.org_role>0 AND o.widest_entry='organization' AND ?
        AND EXISTS(SELECT 1 FROM enterprise_principals reader WHERE reader.tenant=p.tenant AND reader.id=?
            AND reader.active=1 AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                WHERE held.tenant=reader.tenant AND held.principal=reader.id)){one}'''

    def _roles(self, db, ctx, workspace=None):
        """{workspace: (role, direct)} for the reader: one indexed statement per request.

        `direct` comes from principal and group entries; `role` also counts the
        organization entry (active, non-quarantined readers, credentials that are
        not delegated). A delegated credential has no role outside its delegated
        workspaces. Mirrors search_workspace_role (migration 048)."""
        one = ' AND p.id=?' if workspace is not None else ''
        delegated = ctx.get('delegated_projects')
        args = [ctx['tenant'], ctx['actor']] + ([workspace] if one else []) + [
            ctx['tenant'], delegated is None, ctx['actor']] + ([workspace] if one else [])
        roles = {}
        for row in db.execute(self._ROLES_SQL.format(one=one), tuple(args)).fetchall():
            if delegated is not None and row['id'] not in delegated:
                continue
            role, direct = roles.get(row['id'], (0, 0))
            direct = max(direct, int(row['direct'] or 0))
            roles[row['id']] = (max(role, direct, int(row['organization'] or 0)), direct)
        return {name: value for name, value in roles.items() if value[0] > 0}

    def _workspace_role(self, db, ctx, workspace):
        """(role, direct) of the reader in one workspace; (0, 0) when none."""
        return self._roles(db, ctx, workspace).get(workspace, (0, 0))

    def _reader_workspaces(self, db, ctx):
        """(R, D): workspaces where the reader's role (direct role) is at least reader, sorted."""
        roles = self._roles(db, ctx)
        return (sorted(name for name, (role, _) in roles.items() if role >= READER),
                sorted(name for name, (_, direct) in roles.items() if direct >= READER))

    def _project_member(self, db, ctx, project, minimum=READER):
        """The reader holds at least `minimum` (default reader) in an active workspace."""
        return self._workspace_role(db, ctx, project)[0] >= minimum

    def _need_role(self, db, ctx, workspace, minimum, action=None):
        """Credential action ∩ role: raise Denied unless both allow it."""
        from agenthub.enterprise import Denied
        if action is not None and action not in ctx['actions']:
            raise Denied()
        role = self._workspace_role(db, ctx, workspace)[0]
        if role < minimum or (action is not None and action not in ROLE_ACTIONS[role]):
            raise Denied()
        return role

    def _effective_actions(self, db, ctx, workspace):
        """Credential actions ∩ the actions implied by the role in `workspace`."""
        role = self._workspace_role(db, ctx, workspace)[0]
        return set(ctx['actions']) & ROLE_ACTIONS.get(role, frozenset())

    # The predicate ----------------------------------------------------------------

    def _origin_admits(self, db, ctx, source, roles=None, *, via_share=False):
        """The carried-workspace and origin terms (and, unless via_share, the workspace
        term) of the audience, for a reader who is not the owner."""
        ceiling, _, carried = _origin(source)
        if ceiling == 'private' or (ceiling not in SHARING):
            return False
        legacy = ceiling == 'team'
        if via_share and not legacy and not carried:
            return True  # nothing to check: no statement
        roles = self._roles(db, ctx) if roles is None else roles
        role, direct = roles.get(source['external_project'], (0, 0))
        if not via_share and role < READER:
            return False
        if legacy and direct < READER:
            return False
        for name in carried:
            role, direct = roles.get(name, (0, 0))
            if role < READER or (legacy and direct < READER):
                return False
        return True

    def _in_audience(self, db, ctx, source, roles=None):
        """Owner, or the inherited workspace audience (not private) with every carried
        workspace and origin rule. Mirrors search_reader_in_audience(..., false)."""
        if source['owner'] == ctx['actor']:
            return True
        if int(source.get('private') or 0):
            return False
        return self._origin_admits(db, ctx, source, roles)

    def _shared_with(self, db, ctx, source, roles=None):
        """An explicit share of this source's passages to the reader or a group of
        theirs, plus the carried-workspace and origin rules. Mirrors
        search_permissions.SOURCE_GRANTED with search_reader_in_audience(..., true).
        The ordinary source rules are the caller's."""
        if (not source or source['tenant'] != ctx['tenant'] or not source['active']
                or source['owner'] == ctx['actor']):
            return False
        if not self._share_rows(db, ctx, source, 'passages'):
            return False
        return self._origin_admits(db, ctx, source, roles, via_share=True)

    def _share_rows(self, db, ctx, source, tier):
        """Active grants of `tier` ('passages'/'originals') from the owner that cover this
        source (source, its session or its connection) and name the reader or a group of
        theirs; none in an archived workspace, while owner sharing is restricted, or
        to a quarantined reader. One statement."""
        return db.execute('''SELECT g.grantee_kind,g.grantee FROM raw_source_grants g
            JOIN enterprise_organizations o ON o.id=g.tenant
            JOIN enterprise_projects p ON p.tenant=g.tenant AND p.id=? AND p.active=1
            WHERE g.tenant=? AND g.revoked IS NULL AND g.''' + ('passages' if tier == 'passages' else 'originals') + '''=1
            AND g.granted_by=? AND (g.issued_with='policy' OR o.owner_sharing='allowed')
            AND ((g.scope_kind='source' AND g.scope_id=?)
                OR (g.scope_kind='session' AND g.scope_id=(SELECT session FROM memories WHERE id=?))
                OR (g.scope_kind='connection' AND g.scope_id=(SELECT connection FROM backend_source_revisions
                    WHERE source_id=?)))
            AND ((g.grantee_kind='principal' AND g.grantee=?)
                OR (g.grantee_kind='group' AND EXISTS(SELECT 1 FROM group_closure c WHERE c.tenant=g.tenant
                    AND c.group_id=g.grantee AND c.principal=?)))
            AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held WHERE held.tenant=g.tenant
                AND held.principal=?)''',
            (source['external_project'], source['tenant'], source['owner'], source['id'], source['id'],
             source['id'], ctx['actor'], ctx['actor'], ctx['actor'])).fetchall()

    def _audience_is_organization(self, db, source):
        """The item's audience is the whole organization (labels and reach only)."""
        if int(source.get('private') or 0) or source.get('principal_quarantine'):
            return False
        ceiling, _, carried = _origin(source)
        if ceiling != 'organization':
            return False
        row = db.execute('''SELECT o.widest_entry,count(p.id) n FROM enterprise_organizations o
            LEFT JOIN enterprise_projects p ON p.tenant=o.id AND p.id=ANY(?::text[]) AND p.active=1 AND p.org_role>0
            WHERE o.id=? GROUP BY o.widest_entry''', (sorted({source['external_project'], *carried}),
                                                      source['tenant'])).fetchone()
        return bool(row and row['widest_entry'] == 'organization'
                    and row['n'] == len({source['external_project'], *carried}))

    def _passage_readers(self, db, source):
        """Principals in the audience of this source's passages under current policy.

        A policy report for `explain`, not a credential check: the owner plus every
        active principal in the inherited audience whom the ordinary source rules
        (quarantine, connection readers) admit. Explicit shares are listed separately.
        """
        if not source or not source['active']:
            return []
        readers = {source['owner']}
        if not source.get('principal_quarantine') and not int(source.get('private') or 0):
            definition = db.execute('''SELECT p.definition FROM search_source_policies m
                JOIN search_policies p ON p.id=m.policy_id WHERE m.source_id=?''', (source['id'],)).fetchone()
            if definition:
                readers |= {row['id'] for row in db.execute('''SELECT who.id FROM enterprise_principals who
                    WHERE who.tenant=? AND who.active=1
                    AND search_reader_in_audience(?::jsonb,who.tenant,who.id,NULL,false)''',
                    (source['tenant'], json.dumps(definition['definition']))).fetchall()}
            link = db.execute('''SELECT c.owner,c.reader_ids FROM backend_source_revisions r
                JOIN backend_connections c ON c.id=r.connection WHERE r.source_id=?''', (source['id'],)).fetchone()
            listed = json.loads(link['reader_ids']) if link else []
            if listed:
                readers = {r for r in readers if r in set(listed) | {link['owner'], source['owner']}}
        return sorted(readers)

    # Ingest ------------------------------------------------------------------------

    def _resolve_workspace(self, db, tenant, ref, *, active_only=False):
        """A workspace row by name, else by id (names and ids share one namespace)."""
        if not isinstance(ref, str) or not NAME.fullmatch(ref):
            return None
        row = (db.execute('SELECT * FROM enterprise_projects WHERE tenant=? AND name=?', (tenant, ref)).fetchone()
               or db.execute('SELECT * FROM enterprise_projects WHERE tenant=? AND id=?', (tenant, ref)).fetchone())
        if row and active_only and not row['active']:
            return None
        return row

    def _ingest_privacy(self, db, ctx, connection_visibility, value):
        """(private, requested, reasons) for one capture in its workspace.

        New items inherit the workspace audience unless something makes them
        private: no sharing request (an older client's default), a legacy `team`
        request in a workspace with an organization entry, a private capture
        connection, the person's contributions_default, or the workspace's
        item_default. A request never widens anything."""
        requested = value.get('sharing', 'private')
        if requested not in SHARING + ('shared',):
            raise ValueError('invalid_sharing_request')
        row = db.execute('''SELECT who.contributions_default,p.item_default,p.org_role,o.widest_entry
            FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
            JOIN enterprise_principals who ON who.tenant=p.tenant AND who.id=?
            WHERE p.tenant=? AND p.id=?''', (ctx['actor'], ctx['tenant'], value['project'])).fetchone()
        reasons = []
        if requested == 'private':
            reasons.append('request')
        if requested == 'team' and row and row['org_role'] > 0:
            reasons.append('legacy_team_request')
        if connection_visibility == 'private':
            reasons.append('connection')
        if not row or row['contributions_default'] == 'private':
            reasons.append('person')
        if not row or row['item_default'] == 'private':
            reasons.append('workspace')
        return bool(reasons), requested, reasons

    def _check_remote(self, db, ctx, workspace, remote):
        """None, or a notice when an admin's first capture links an unlinked remote.

        A remote linked to another workspace, or an unlinked one captured by anyone
        but a workspace admin holding `manage`, raises RepoNotLinked."""
        if remote is None:
            return None
        row = db.execute('SELECT workspace FROM workspace_repos WHERE tenant=? AND remote=?',
                         (ctx['tenant'], remote)).fetchone()
        if row:
            if row['workspace'] != workspace:
                raise RepoNotLinked()
            return None
        role = self._workspace_role(db, ctx, workspace)[0]
        if role < ADMIN or 'manage' not in ctx['actions'] or ctx.get('acting_for'):
            raise RepoNotLinked()
        db.execute('''INSERT INTO workspace_repos(tenant,remote,workspace,linked_by,created) VALUES(?,?,?,?,?)''',
                   (ctx['tenant'], remote, workspace, ctx['actor'], time.time()))
        self._bump_epoch(db, ctx['tenant'], [workspace])
        self._audit(db, ctx, 'repo_link', 'automatic', json.dumps([workspace, remote]))
        name = db.execute('SELECT name FROM enterprise_projects WHERE tenant=? AND id=?',
                          (ctx['tenant'], workspace)).fetchone()['name']
        return f'vaelius: linked {remote} to workspace "{name}" (first capture by a workspace admin)'

    # The person's own setting -------------------------------------------------------

    def sharing_setting(self, ctx, value):
        """Self-service: report, or change, the caller's contributions_default.

        `private` makes every new item of the person private; with
        `apply_to_existing` it also marks their existing items private (a preview
        with a confirm token, then the same request with `confirm`). It never widens
        an existing item. Legacy `max_sharing` requests are mapped (private ->
        private, anything else -> shared). Delegated credentials only read it.
        """
        from agenthub.enterprise import Denied
        from agenthub.owner_controls import confirm_token
        if not isinstance(value, dict) or set(value) - {'max_sharing', 'contributions_default',
                                                       'apply_to_existing', 'confirm'}:
            raise ValueError('invalid_sharing_request')
        setting = value.get('contributions_default')
        if 'max_sharing' in value:
            if value['max_sharing'] not in SHARING or setting is not None:
                raise ValueError('invalid_max_sharing')
            setting = 'private' if value['max_sharing'] == 'private' else 'shared'
        if setting is not None and setting not in ITEM_DEFAULTS:
            raise ValueError('invalid_contributions_default')
        existing = value.get('apply_to_existing', False)
        confirm = value.get('confirm')
        if type(existing) is not bool or (existing and setting != 'private') \
                or (confirm is not None and not isinstance(confirm, str)):
            raise ValueError('invalid_sharing_request')
        if setting is not None and ctx.get('acting_for'):
            raise Denied()
        with (self.delivery_lock() if setting is not None else nullcontext()), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row = db.execute('''SELECT p.contributions_default,o.widest_entry FROM enterprise_principals p
                JOIN enterprise_organizations o ON o.id=p.tenant WHERE p.tenant=? AND p.id=? AND p.active=1''',
                (ctx['tenant'], ctx['actor'])).fetchone()
            if not row:
                raise Denied()
            changed = setting is not None and setting != row['contributions_default']
            if changed:
                db.execute('UPDATE enterprise_principals SET contributions_default=? WHERE tenant=? AND id=?',
                           (setting, ctx['tenant'], ctx['actor']))
                self._audit(db, ctx, 'contributions_default', setting, ctx['actor'])
            result = {'principal': ctx['actor'], 'contributions_default': setting or row['contributions_default'],
                      'changed': changed, 'widest_entry': row['widest_entry']}
            if existing:
                rows = db.execute('''SELECT id,source_version FROM enterprise_sources WHERE tenant=? AND owner=?
                    AND active=1 AND private=0 ORDER BY id''', (ctx['tenant'], ctx['actor'])).fetchall()
                token = confirm_token(ctx, 'contributions_private', {}, rows)
                summary = {'sources': len(rows), 'confirm_token': token if rows else None}
                if confirm and confirm == token and rows:
                    self._make_private(db, ctx, [r['id'] for r in rows])
                    summary = {'sources': len(rows), 'confirm_token': None, 'applied': len(rows)}
                result['existing'] = summary
        level = result['contributions_default']
        result['max_sharing'] = 'private' if level == 'private' else 'organization'
        result['tenant_ceiling'] = {'none': 'private', 'people_and_groups': 'team'}.get(result['widest_entry'],
                                                                                        'organization')
        return result

    def _make_private(self, db, ctx, ids):
        """Mark some of the caller's sources private (narrowing only), with history."""
        now = time.time()
        for ident in ids:
            changed = db.execute('''UPDATE enterprise_sources SET private=1,policy_version=policy_version+1
                WHERE id=? AND tenant=? AND owner=? AND private=0''', (ident, ctx['tenant'], ctx['actor'])).rowcount
            if not changed:
                continue
            db.execute('''UPDATE enterprise_documents SET raw_shared=0 WHERE representation='raw'
                AND id IN (SELECT document_id FROM enterprise_dependencies WHERE source_id=?)''', (ident,))
            db.execute('''INSERT INTO enterprise_sharing_changes
                (tenant,actor,source_id,tier,from_level,to_level,requested,capped_by,request_id,created)
                VALUES(?,?,?,'passages','shared','private','private','',?,?)''',
                (ctx['tenant'], ctx['actor'], ident, ctx.get('request_id'), now))
            self._audit(db, ctx, 'source_sharing', 'shared>private', ident)

    # Maintenance ---------------------------------------------------------------------

    @staticmethod
    def _bump_epoch(db, tenant, workspaces):
        db.execute('UPDATE enterprise_projects SET epoch=epoch+1 WHERE tenant=? AND id=ANY(?::text[])',
                   (tenant, sorted(workspaces)))

    @staticmethod
    def _refresh_groups(db, tenant, groups):
        db.execute('SELECT access_refresh_groups(?,?::text[])', (tenant, sorted(set(groups))))

    @staticmethod
    def _refresh_workspaces(db, tenant, workspaces):
        db.execute('SELECT access_refresh_workspaces(?,?::text[])', (tenant, sorted(set(workspaces))))

    @staticmethod
    def _audience_size(db, tenant, workspace):
        return db.execute('''SELECT count(*) FROM enterprise_principals who WHERE who.tenant=? AND who.active=1
            AND (search_workspace_role(who.tenant,who.id,?,NULL)).role>=1''', (tenant, workspace)).fetchone()[0]

    @staticmethod
    def _counting_admins(db, tenant, workspace):
        """Admin entries that keep a workspace managed: a principal entry for an active
        principal, or a local group entry with an active member. Synced groups never count."""
        return db.execute('''SELECT count(*) FROM workspace_entries e WHERE e.tenant=? AND e.workspace=?
            AND e.role='admin' AND (
                (e.subject_kind='principal' AND EXISTS(SELECT 1 FROM enterprise_principals who
                    WHERE who.tenant=e.tenant AND who.id=e.subject AND who.active=1))
                OR (e.subject_kind='group' AND EXISTS(SELECT 1 FROM groups g JOIN group_closure c
                    ON c.tenant=g.tenant AND c.group_id=g.id JOIN enterprise_principals who
                    ON who.tenant=c.tenant AND who.id=c.principal AND who.active=1
                    WHERE g.tenant=e.tenant AND g.id=e.subject AND g.source='local')))''',
            (tenant, workspace)).fetchone()[0]

    def _guard_last_admins(self, db, tenant, before):
        """Refuse a change that removed the last counting admin of any workspace in `before`."""
        for workspace, count in before.items():
            if count and not self._counting_admins(db, tenant, workspace):
                raise ValueError('last_admin')

    def _group_workspaces(self, db, tenant, group):
        """Workspaces whose entries name this group or a group that contains it."""
        return [row[0] for row in db.execute('''WITH RECURSIVE up(id) AS (SELECT ?::text UNION
                SELECT e.group_id FROM group_edges e JOIN up ON e.tenant=? AND e.member_kind='group' AND e.member=up.id)
            SELECT DISTINCT w.workspace FROM workspace_entries w JOIN up ON w.subject_kind='group' AND w.subject=up.id
            WHERE w.tenant=?''', (group, tenant, tenant)).fetchall()]

    def _local_groups_of(self, db, tenant, principal):
        """Local groups whose admins include this principal through an admin edge."""
        return [row[0] for row in db.execute('''SELECT DISTINCT e.group_id FROM group_edges e JOIN groups g
            ON g.tenant=e.tenant AND g.id=e.group_id WHERE e.tenant=? AND e.member_kind='principal'
            AND e.member=? AND g.source='local' ''', (tenant, principal)).fetchall()]

    # Containment changes ---------------------------------------------------------------
    #
    # Sessions of workspace S may read workspace T while audience(T) ⊇ audience(S)
    # (section 7). An access-list or group change can end that for some pairs; the
    # admin is told which (`newly_unreadable`), and group sync logs it. The rule itself
    # is unchanged: this only reports what a change did to it.

    def _contained_pairs(self, db, tenant, scope, focus):
        """{(S, T)}: S != T, both in `scope`, one of them in `focus`, audience(T) ⊇ audience(S)
        (current materialized state, the same superset query as the session filter).
        None when the scope is too large to check within one request."""
        scope = sorted(set(scope))
        if len(scope) > CONTAINMENT_SCOPE:
            return None
        focus = set(focus)
        pairs = set()
        for source in scope:
            candidates = [w for w in scope if w != source and (source in focus or w in focus)]
            if not candidates:
                continue
            rows = db.execute(self._SUPERSET_SQL.format(audience=self._AUDIENCE_SQL), (
                tenant, source, source, tenant, tenant, tenant, candidates, tenant, [], tenant, candidates,
                tenant, [], '')).fetchall()
            if not any(r['kind'] == 'self' and r['total'] for r in rows):
                continue  # nobody can start a session in a workspace with an empty audience
            pairs |= {(source, r['id']) for r in rows if r['kind'] == 'workspace' and r['contains']}
        return pairs

    def _newly_unreadable(self, db, tenant, before, after):
        """`newly_unreadable` (target names) and a one-line explanation, from two
        `_contained_pairs` results."""
        if before is None or after is None:
            return {'newly_unreadable': [], 'newly_unreadable_note': 'not checked: too many workspaces'}
        lost = sorted(before - after)
        if not lost:
            return {'newly_unreadable': []}
        ids = sorted({w for pair in lost for w in pair})
        names = {r['id']: r['name'] for r in db.execute(
            'SELECT id,name FROM enterprise_projects WHERE tenant=? AND id=ANY(?::text[])', (tenant, ids))}
        name = lambda ident: names.get(ident, ident)
        by_source = {}
        for source, target in lost:
            by_source.setdefault(name(source), []).append(name(target))
        clauses = ['sessions in ' + source + ' can no longer read ' + ', '.join(sorted(targets))
                   for source, targets in sorted(by_source.items())]
        return {'newly_unreadable': sorted({name(t) for _, t in lost}),
                'newly_unreadable_note': '; '.join(clauses) + ' (those workspaces no longer include everyone in '
                                         'the sessions\' audience). Add the same people as readers there, or use '
                                         "'vaelius-client session use <workspace>' per session."}

    def _containment_scope(self, db, ctx, focus):
        """The caller's readable workspaces (reported pairs never name others) and the
        focus workspaces among them."""
        readable = {w for w, (role, _) in self._roles(db, ctx).items() if role >= READER}
        return readable, readable & set(focus)

    def log_newly_unreadable(self, db, tenant, before, after, cause):
        """Group sync: report lost containment in the server log only."""
        found = self._newly_unreadable(db, tenant, before, after)
        if found['newly_unreadable']:
            import logging
            logging.getLogger('agenthub.workspace_access').warning(json.dumps(
                {'event': 'newly_unreadable', 'tenant': tenant, 'cause': cause, **found}, sort_keys=True))
        return found

    def sync_containment(self, db, tenant, groups):
        """`_contained_pairs` for the workspaces a group sync can change (all active ones)."""
        focus = set()
        for group in groups:
            focus.update(self._group_workspaces(db, tenant, group))
        if not focus:
            return set()
        scope = [r[0] for r in db.execute('SELECT id FROM enterprise_projects WHERE tenant=? AND active=1', (tenant,))]
        return self._contained_pairs(db, tenant, scope, focus)

    # Authority -----------------------------------------------------------------------

    def _settings_admin(self, db, ctx):
        if 'settings' not in ctx['actions'] or ctx.get('acting_for'):
            return False
        row = db.execute('SELECT settings_admin FROM enterprise_principals WHERE tenant=? AND id=? AND active=1',
                         (ctx['tenant'], ctx['principal'])).fetchone()
        return bool(row and row['settings_admin'])

    def _manage_workspace(self, db, ctx, ref, *, allow_inactive=True):
        """The workspace row an admin (role admin, credential `manage`) may manage, or a
        tenant settings administrator's audited break-glass override."""
        from agenthub.enterprise import Denied
        row = self._resolve_workspace(db, ctx['tenant'], ref)
        if not row or (not allow_inactive and not row['active']):
            raise Denied()
        if 'manage' in ctx['actions'] and not ctx.get('acting_for'):
            role = self._roles(db, dict(ctx, delegated_projects=None), row['id']).get(row['id'], (0, 0))[0]
            if role >= ADMIN or (not row['active'] and self._admin_entry(db, ctx, row['id'])):
                return row, False
        if self._settings_admin(db, ctx):
            return row, True
        raise Denied()

    @staticmethod
    def _admin_entry(db, ctx, workspace):
        return bool(db.execute('''SELECT 1 FROM workspace_access WHERE tenant=? AND principal=? AND workspace=?
            AND greatest(principal_role,group_role)=3''', (ctx['tenant'], ctx['actor'], workspace)).fetchone())

    def _audit_workspace(self, db, ctx, override, operation, disposition, target):
        self._audit(db, ctx, operation, disposition, target)
        if override:
            self._audit(db, ctx, 'workspace_admin_override', operation, target)

    # Workspace API -------------------------------------------------------------------

    @staticmethod
    def _workspace_view(row):
        # Visibility is decided on the server only (never in `.vaelius`): `org` while the
        # workspace has an organization entry, else `private` (admins and whoever they add).
        return {'id': row['id'], 'name': row['name'], 'item_default': row['item_default'],
                'active': bool(row['active']), 'visibility': 'org' if row['org_role'] else 'private'}

    def list_workspaces(self, ctx, value=None):
        """Workspaces the caller can read, with their role and the organization entry."""
        if value not in (None, {}):
            raise ValueError('invalid_request')
        self._need(ctx, 'read')
        with self.open() as state:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            roles = self._roles(db, ctx)
            rows = db.execute('SELECT * FROM enterprise_projects WHERE tenant=? AND id=ANY(?::text[]) ORDER BY name',
                              (ctx['tenant'], sorted(roles))).fetchall() if roles else []
        return {'workspaces': [dict(self._workspace_view(row), role=role_name(roles[row['id']][0]),
                                    organization_entry=role_name(row['org_role'])) for row in rows]}

    def create_workspace(self, ctx, value):
        """Self-service: any principal whose credential holds `manage` (and, when the
        tenant restricts creation, holds the settings_admin flag) creates a workspace
        and becomes its first admin.

        Visibility is chosen here, on the server, and defaults to `org`: an
        organization reader entry, so everyone signed in to the tenant reads it (there
        is no internet-public tier). `private` is only the creator, as admin, plus
        whoever the admins add. The tenant's `widest_entry` still caps `org`: an
        explicit `org` is refused, and the default falls back to `private`, saying so."""
        from agenthub.enterprise import Denied, Conflict
        if not isinstance(value, dict) or set(value) - {'name', 'item_default', 'visibility'}:
            raise ValueError('invalid_request')
        name, item_default = value.get('name'), value.get('item_default', 'shared')
        visibility = value.get('visibility', DEFAULT_VISIBILITY)
        if visibility not in VISIBILITIES:
            raise ValueError('invalid_visibility')
        if not isinstance(name, str) or not NAME.fullmatch(name) or item_default not in ITEM_DEFAULTS:
            raise ValueError('invalid_workspace')
        if 'manage' not in ctx['actions'] or ctx.get('acting_for'):
            raise Denied()
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            tenant = db.execute('SELECT workspace_creation FROM enterprise_organizations WHERE id=?',
                                (ctx['tenant'],)).fetchone()
            if not tenant:
                raise Denied()
            if tenant['workspace_creation'] == 'admins':
                row = db.execute('SELECT settings_admin FROM enterprise_principals WHERE tenant=? AND id=?',
                                 (ctx['tenant'], ctx['principal'])).fetchone()
                if not row or not row['settings_admin']:
                    raise Denied()
            if self._resolve_workspace(db, ctx['tenant'], name):
                raise Conflict()
            widest = db.execute('SELECT widest_entry FROM enterprise_organizations WHERE id=?',
                                (ctx['tenant'],)).fetchone()['widest_entry']
            if visibility == 'org' and self._suspended(widest, 'organization', 'reader'):
                if 'visibility' in value:
                    raise ValueError('entry_wider_than_tenant_allows')
                visibility = 'private'  # the default, capped by the tenant
            now = time.time()
            db.execute('''INSERT INTO enterprise_projects(tenant,id,name,created_by,created,item_default)
                VALUES(?,?,?,?,?,?)''', (ctx['tenant'], name, name, ctx['actor'], now, item_default))
            db.execute('''INSERT INTO workspace_entries(tenant,workspace,subject_kind,subject,role,granted_by,created)
                VALUES(?,?,'principal',?,'admin',?,?)''', (ctx['tenant'], name, ctx['actor'], ctx['actor'], now))
            self._refresh_workspaces(db, ctx['tenant'], [name])
            self._audit(db, ctx, 'workspace_create', 'created:' + visibility, name)
            if visibility == 'org':
                self._write_entry(db, ctx, name, 'organization', '', 'reader')
            row = db.execute('SELECT * FROM enterprise_projects WHERE tenant=? AND id=?', (ctx['tenant'], name)).fetchone()
        notice = (ORG_NOTICE if visibility == 'org' else
                  'private: only you and whoever an admin adds' + (
                      ' (this organization does not allow org-wide workspaces)' if 'visibility' not in value else ''))
        return {'workspace': self._workspace_view(row), 'role': 'admin', 'notice': notice}

    def show_workspace(self, ctx, value):
        """Settings, access list, linked repositories and audience size (readers and admins)."""
        from agenthub.enterprise import Denied
        if not isinstance(value, dict) or set(value) != {'workspace'}:
            raise ValueError('invalid_request')
        self._need(ctx, 'read')
        with self.open() as state:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row = self._resolve_workspace(db, ctx['tenant'], value['workspace'])
            if not row:
                raise Denied()
            role = self._workspace_role(db, ctx, row['id'])[0]
            if role < READER and not self._settings_admin(db, ctx):
                raise Denied()
            widest = db.execute('SELECT widest_entry FROM enterprise_organizations WHERE id=?',
                                (ctx['tenant'],)).fetchone()['widest_entry']
            entries = db.execute('''SELECT subject_kind,subject,role,granted_by,created FROM workspace_entries
                WHERE tenant=? AND workspace=? ORDER BY subject_kind,subject''', (ctx['tenant'], row['id'])).fetchall()
            repos = [r[0] for r in db.execute('SELECT remote FROM workspace_repos WHERE tenant=? AND workspace=? ORDER BY remote',
                                              (ctx['tenant'], row['id']))]
            size = self._audience_size(db, ctx['tenant'], row['id'])
        return {'workspace': self._workspace_view(row), 'role': role_name(role),
                'entries': [{'subject_kind': e['subject_kind'], 'subject': e['subject'] or None, 'role': e['role'],
                             'suspended': self._suspended(widest, e['subject_kind'], e['role']),
                             'granted_by': e['granted_by'], 'created': e['created']} for e in entries],
                'repos': repos, 'audience_size': size}

    def set_visibility(self, ctx, value):
        """Change a workspace's visibility (admins; settings override): `org` adds an
        organization reader entry when there is none, `private` removes the organization
        entry. Principal and group entries are untouched. Returns `newly_unreadable` like
        any entry change."""
        if not isinstance(value, dict) or set(value) != {'workspace', 'visibility'}:
            raise ValueError('invalid_request')
        if value['visibility'] not in VISIBILITIES:
            raise ValueError('visibility_required')
        with self.open() as state:
            row = self._resolve_workspace(state.db, ctx['tenant'], value['workspace'])
            entry = state.db.execute('''SELECT role FROM workspace_entries WHERE tenant=? AND workspace=?
                AND subject_kind='organization' ''', (ctx['tenant'], row['id'])).fetchone() if row else None
        target = {'workspace': value['workspace'], 'subject_kind': 'organization'}
        if value['visibility'] == 'org':
            if not entry and row:
                # What opening it exposes: its shared items with no narrower origin.
                with self.open() as state:
                    self._manage_workspace(state.db, ctx, value['workspace'])
                    exposed = state.db.execute('''SELECT count(*) FROM enterprise_sources WHERE tenant=?
                        AND external_project=? AND active=1 AND private=0
                        AND coalesce(origin_ceiling,'')!='private' AND coalesce(origin_projects,'[]')='[]' ''',
                        (ctx['tenant'], row['id'])).fetchone()[0]
            if entry:
                # Check the caller manages it before reporting anything about it.
                with self.open() as state:
                    self._manage_workspace(state.db, ctx, value['workspace'])
                result = {'workspace': row['id'], 'operation': 'unchanged',
                          'entry': {'subject_kind': 'organization', 'subject': None, 'role': entry['role']}}
            else:
                result = dict(self.set_entry(ctx, dict(target, role='reader')),
                              items_now_visible_to_org=int(exposed), notice=ORG_NOTICE)
        elif entry:
            result = self.remove_entry(ctx, target)
        else:
            with self.open() as state:
                self._manage_workspace(state.db, ctx, value['workspace'])
            result = {'workspace': row['id'] if row else value['workspace'], 'operation': 'unchanged'}
        return dict(result, visibility=value['visibility'])

    @staticmethod
    def _suspended(widest, kind, role):
        if widest == 'organization':
            return False
        if widest == 'people_and_groups':
            return kind == 'organization'
        return not (kind == 'principal' and role == 'admin')

    def workspace_settings(self, ctx, value):
        """Rename, change item_default or archive/restore (admins; settings override)."""
        from agenthub.enterprise import Conflict
        if not isinstance(value, dict) or 'workspace' not in value \
                or set(value) - {'workspace', 'name', 'item_default', 'active'}:
            raise ValueError('invalid_request')
        name, item_default, active = value.get('name'), value.get('item_default'), value.get('active')
        if (name is not None and (not isinstance(name, str) or not NAME.fullmatch(name))) \
                or (item_default is not None and item_default not in ITEM_DEFAULTS) \
                or (active is not None and type(active) is not bool):
            raise ValueError('invalid_workspace_settings')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row, override = self._manage_workspace(db, ctx, value['workspace'])
            changes = {}
            if name is not None and name != row['name']:
                clash = self._resolve_workspace(db, ctx['tenant'], name)
                if clash and clash['id'] != row['id']:
                    raise Conflict()
                changes['name'] = name
            if item_default is not None and item_default != row['item_default']:
                changes['item_default'] = item_default
            if active is not None and int(active) != row['active']:
                changes['active'] = int(active)
            for key, item in changes.items():
                db.execute('UPDATE enterprise_projects SET ' + key + '=?,epoch=epoch+1 WHERE tenant=? AND id=?',
                           (item, ctx['tenant'], row['id']))
            if 'active' in changes:
                # A workspace's own groups contribute members only while it is active.
                groups = [r[0] for r in db.execute('SELECT id FROM groups WHERE tenant=? AND workspace=?',
                                                   (ctx['tenant'], row['id']))]
                if groups:
                    self._refresh_groups(db, ctx['tenant'], groups)
            if changes:
                self._audit_workspace(db, ctx, override, 'workspace_settings', ','.join(sorted(changes)), row['id'])
            row = db.execute('SELECT * FROM enterprise_projects WHERE tenant=? AND id=?', (ctx['tenant'], row['id'])).fetchone()
        return {'workspace': self._workspace_view(row), 'changed': bool(changes)}

    def _subject(self, db, ctx, value):
        from agenthub.enterprise import Denied
        kind, subject = value.get('subject_kind'), value.get('subject')
        if kind not in SUBJECT_KINDS:
            raise ValueError('invalid_subject')
        if kind == 'organization':
            if subject not in (None, ''):
                raise ValueError('invalid_subject')
            return kind, ''
        if not isinstance(subject, str) or not GROUP_NAME.fullmatch(subject):
            raise ValueError('invalid_subject')
        if kind == 'principal':
            if not db.execute('SELECT 1 FROM enterprise_principals WHERE tenant=? AND id=? AND active=1',
                              (ctx['tenant'], subject)).fetchone():
                raise ValueError('unknown_subject')
            return kind, subject
        group = self._resolve_group(db, ctx['tenant'], subject)
        if not group:
            raise ValueError('unknown_subject')
        if not group['active']:
            raise Denied()
        return kind, group['id']

    def set_entry(self, ctx, value):
        """Add an entry or change its role (admins; settings override). Audited with the
        audience size before and after."""
        if not isinstance(value, dict) or set(value) - {'workspace', 'subject_kind', 'subject', 'role'} \
                or 'workspace' not in value:
            raise ValueError('invalid_request')
        role = value.get('role')
        _role_rank(role)
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row, override = self._manage_workspace(db, ctx, value['workspace'])
            kind, subject = self._subject(db, ctx, value)
            widest = db.execute('SELECT widest_entry FROM enterprise_organizations WHERE id=?',
                                (ctx['tenant'],)).fetchone()['widest_entry']
            if self._suspended(widest, kind, role):
                raise ValueError('entry_wider_than_tenant_allows')
            scope, focus = self._containment_scope(db, ctx, [row['id']])
            before = self._contained_pairs(db, ctx['tenant'], scope, focus)
            result = self._write_entry(db, ctx, row['id'], kind, subject, role, override=override)
            after = self._contained_pairs(db, ctx['tenant'], scope, focus)
            result.update(self._newly_unreadable(db, ctx['tenant'], before, after))
        return result

    def _write_entry(self, db, ctx, workspace, kind, subject, role, *, override=False):
        prior = db.execute('''SELECT role FROM workspace_entries WHERE tenant=? AND workspace=? AND subject_kind=?
            AND subject=?''', (ctx['tenant'], workspace, kind, subject)).fetchone()
        before = self._audience_size(db, ctx['tenant'], workspace)
        admins = {workspace: self._counting_admins(db, ctx['tenant'], workspace)}
        if prior and prior['role'] == role:
            operation = 'unchanged'
        else:
            db.execute('''INSERT INTO workspace_entries(tenant,workspace,subject_kind,subject,role,granted_by,created)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant,workspace,subject_kind,subject) DO UPDATE SET
                role=excluded.role,granted_by=excluded.granted_by,created=excluded.created''',
                (ctx['tenant'], workspace, kind, subject, role, ctx['actor'], time.time()))
            self._refresh_workspaces(db, ctx['tenant'], [workspace])
            self._guard_last_admins(db, ctx['tenant'], admins)
            operation = 'changed' if prior else 'added'
        after = self._audience_size(db, ctx['tenant'], workspace)
        if operation != 'unchanged':
            self._audit_workspace(db, ctx, override, 'workspace_entry_role' if prior else 'workspace_entry_add',
                                  f'{role}:{before}>{after}', json.dumps([workspace, kind, subject]))
        return {'workspace': workspace, 'entry': {'subject_kind': kind, 'subject': subject or None, 'role': role},
                'operation': operation, 'audience_before': before, 'audience_after': after}

    def remove_entry(self, ctx, value):
        """Remove an entry (admins; settings override). The last counting admin stays."""
        if not isinstance(value, dict) or set(value) - {'workspace', 'subject_kind', 'subject'} \
                or 'workspace' not in value:
            raise ValueError('invalid_request')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row, override = self._manage_workspace(db, ctx, value['workspace'])
            kind, subject = value.get('subject_kind'), value.get('subject')
            if kind not in SUBJECT_KINDS:
                raise ValueError('invalid_subject')
            if kind == 'group' and isinstance(subject, str):
                group = self._resolve_group(db, ctx['tenant'], subject)
                subject = group['id'] if group else subject
            subject = '' if kind == 'organization' else subject
            if not isinstance(subject, str):
                raise ValueError('invalid_subject')
            scope, focus = self._containment_scope(db, ctx, [row['id']])
            before = self._contained_pairs(db, ctx['tenant'], scope, focus)
            result = self._delete_entry(db, ctx, row['id'], kind, subject, override=override)
            after = self._contained_pairs(db, ctx['tenant'], scope, focus)
            result.update(self._newly_unreadable(db, ctx['tenant'], before, after))
        return result

    def _delete_entry(self, db, ctx, workspace, kind, subject, *, override=False):
        before = self._audience_size(db, ctx['tenant'], workspace)
        admins = {workspace: self._counting_admins(db, ctx['tenant'], workspace)}
        removed = db.execute('''DELETE FROM workspace_entries WHERE tenant=? AND workspace=? AND subject_kind=?
            AND subject=?''', (ctx['tenant'], workspace, kind, subject)).rowcount
        if removed:
            self._refresh_workspaces(db, ctx['tenant'], [workspace])
            self._guard_last_admins(db, ctx['tenant'], admins)
        after = self._audience_size(db, ctx['tenant'], workspace)
        if removed:
            self._audit_workspace(db, ctx, override, 'workspace_entry_remove', f'{before}>{after}',
                                  json.dumps([workspace, kind, subject]))
        return {'workspace': workspace, 'removed': bool(removed), 'audience_before': before, 'audience_after': after}

    def link_repo(self, ctx, value, *, link=True):
        """Link (or unlink) a repository by normalized remote; one remote, one workspace."""
        from agenthub.enterprise import Conflict
        from agentclient.general_contract import normalize_remote, REMOTE
        if not isinstance(value, dict) or set(value) != {'workspace', 'remote'}:
            raise ValueError('invalid_request')
        remote = value['remote']
        if isinstance(remote, str) and not REMOTE.fullmatch(remote):
            remote = normalize_remote(remote)
        if not isinstance(remote, str) or not REMOTE.fullmatch(remote):
            raise ValueError('invalid_remote')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row, override = self._manage_workspace(db, ctx, value['workspace'])
            current = db.execute('SELECT workspace FROM workspace_repos WHERE tenant=? AND remote=?',
                                 (ctx['tenant'], remote)).fetchone()
            changed = False
            if link:
                if current and current['workspace'] != row['id']:
                    raise Conflict()
                if not current:
                    db.execute('''INSERT INTO workspace_repos(tenant,remote,workspace,linked_by,created)
                        VALUES(?,?,?,?,?)''', (ctx['tenant'], remote, row['id'], ctx['actor'], time.time()))
                    changed = True
            elif current and current['workspace'] == row['id']:
                db.execute('DELETE FROM workspace_repos WHERE tenant=? AND remote=?', (ctx['tenant'], remote))
                changed = True
            if changed:
                self._bump_epoch(db, ctx['tenant'], [row['id']])
                self._audit_workspace(db, ctx, override, 'repo_link' if link else 'repo_unlink', 'applied',
                                      json.dumps([row['id'], remote]))
        return {'workspace': row['id'], 'remote': remote, 'linked': link, 'changed': changed}

    def unlink_repo(self, ctx, value):
        return self.link_repo(ctx, value, link=False)

    # Groups -----------------------------------------------------------------------------

    @staticmethod
    def _resolve_group(db, tenant, ref):
        if not isinstance(ref, str) or not GROUP_NAME.fullmatch(ref):
            return None
        return (db.execute('SELECT * FROM groups WHERE tenant=? AND name=?', (tenant, ref)).fetchone()
                or db.execute('SELECT * FROM groups WHERE tenant=? AND id=?', (tenant, ref)).fetchone())

    @staticmethod
    def _group_view(row):
        return {'id': row['id'], 'name': row['name'], 'source': row['source'], 'workspace': row['workspace'],
                'sync_state': row['sync_state'], 'active': bool(row['active'])}

    def _manage_group(self, db, ctx, ref):
        """A local group the caller manages: its own admins, or (for a workspace's group)
        that workspace's admins; settings override audited. Synced groups are read-only."""
        from agenthub.enterprise import Denied
        group = self._resolve_group(db, ctx['tenant'], ref)
        if not group:
            raise Denied()
        if group['source'] != 'local':
            raise ValueError('synced_group_is_read_only')
        if 'manage' in ctx['actions'] and not ctx.get('acting_for'):
            if group['workspace'] is not None:
                try:
                    self._manage_workspace(db, dict(ctx, actions=set(ctx['actions']) - {'settings'}), group['workspace'])
                    return group, False
                except Denied:
                    pass
            elif db.execute('''SELECT 1 FROM group_edges WHERE tenant=? AND group_id=? AND member_kind='principal'
                    AND member=? AND role='admin' ''', (ctx['tenant'], group['id'], ctx['actor'])).fetchone():
                return group, False
        if self._settings_admin(db, ctx):
            return group, True
        raise Denied()

    def _group_admins(self, db, tenant, group):
        return db.execute('''SELECT count(*) FROM group_edges e JOIN enterprise_principals who
            ON who.tenant=e.tenant AND who.id=e.member AND who.active=1 WHERE e.tenant=? AND e.group_id=?
            AND e.member_kind='principal' AND e.role='admin' ''', (tenant, group)).fetchone()[0]

    def list_groups(self, ctx, value=None):
        """Groups the caller belongs to or administers, and groups of workspaces they administer."""
        if value not in (None, {}):
            raise ValueError('invalid_request')
        self._need(ctx, 'read')
        with self.open() as state:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            admin_workspaces = [w for w, (role, _) in self._roles(db, ctx).items() if role >= ADMIN]
            rows = db.execute('''SELECT g.*,EXISTS(SELECT 1 FROM group_closure c WHERE c.tenant=g.tenant
                    AND c.group_id=g.id AND c.principal=?) member,
                EXISTS(SELECT 1 FROM group_edges e WHERE e.tenant=g.tenant AND e.group_id=g.id
                    AND e.member_kind='principal' AND e.member=? AND e.role='admin') group_admin
                FROM groups g WHERE g.tenant=? AND (g.workspace=ANY(?::text[]) OR EXISTS(SELECT 1 FROM group_closure c
                    WHERE c.tenant=g.tenant AND c.group_id=g.id AND c.principal=?) OR EXISTS(SELECT 1 FROM group_edges e
                    WHERE e.tenant=g.tenant AND e.group_id=g.id AND e.member_kind='principal' AND e.member=?))
                ORDER BY g.name''', (ctx['actor'], ctx['actor'], ctx['tenant'], admin_workspaces, ctx['actor'],
                                     ctx['actor'])).fetchall()
        return {'groups': [dict(self._group_view(r), member=bool(r['member']),
                                admin=bool(r['group_admin'] or r['workspace'] in admin_workspaces)) for r in rows]}

    def create_group(self, ctx, value):
        """A local group; the creator is its admin, or it belongs to a workspace the caller administers."""
        from agenthub.enterprise import Denied, Conflict
        if not isinstance(value, dict) or set(value) - {'name', 'workspace'} or 'name' not in value:
            raise ValueError('invalid_request')
        name = value['name']
        if not isinstance(name, str) or not GROUP_NAME.fullmatch(name):
            raise ValueError('invalid_group')
        if name.lower().startswith(('gh-', 'gh_')):
            # Ids and names of the removed GitHub-synced groups (migration 054) are never
            # reused, so nothing that once named one can attach to a new group.
            raise ValueError('reserved_group_name')
        if 'manage' not in ctx['actions'] or ctx.get('acting_for'):
            raise Denied()
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            workspace = None
            if value.get('workspace') is not None:
                workspace, _ = self._manage_workspace(db, ctx, value['workspace'], allow_inactive=False)
                workspace = workspace['id']
            if self._resolve_group(db, ctx['tenant'], name):
                raise Conflict()
            now = time.time()
            db.execute('''INSERT INTO groups(tenant,id,name,source,workspace,created_by,created)
                VALUES(?,?,?,'local',?,?,?)''', (ctx['tenant'], name, name, workspace, ctx['actor'], now))
            if workspace is None:
                db.execute('''INSERT INTO group_edges(tenant,group_id,member_kind,member,role,added_by,created)
                    VALUES(?,?,'principal',?,'admin',?,?)''', (ctx['tenant'], name, ctx['actor'], ctx['actor'], now))
            self._refresh_groups(db, ctx['tenant'], [name])
            self._audit(db, ctx, 'group_create', 'created', name)
            row = db.execute('SELECT * FROM groups WHERE tenant=? AND id=?', (ctx['tenant'], name)).fetchone()
        return {'group': self._group_view(row)}

    def show_group(self, ctx, value):
        from agenthub.enterprise import Denied
        if not isinstance(value, dict) or set(value) != {'group'}:
            raise ValueError('invalid_request')
        self._need(ctx, 'read')
        with self.open() as state:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            group = self._resolve_group(db, ctx['tenant'], value['group'])
            if not group:
                raise Denied()
            visible = db.execute('''SELECT 1 WHERE EXISTS(SELECT 1 FROM group_closure WHERE tenant=? AND group_id=?
                    AND principal=?) OR EXISTS(SELECT 1 FROM group_edges WHERE tenant=? AND group_id=?
                    AND member_kind='principal' AND member=?)''',
                (ctx['tenant'], group['id'], ctx['actor'], ctx['tenant'], group['id'], ctx['actor'])).fetchone()
            if not visible and not (group['workspace'] and self._workspace_role(db, ctx, group['workspace'])[0] >= ADMIN) \
                    and not self._settings_admin(db, ctx):
                raise Denied()
            members = db.execute('''SELECT member_kind,member,role FROM group_edges WHERE tenant=? AND group_id=?
                ORDER BY member_kind,member''', (ctx['tenant'], group['id'])).fetchall()
        return {'group': self._group_view(group),
                'members': [{'kind': m['member_kind'], 'id': m['member'], 'role': m['role']} for m in members]}

    def add_group_member(self, ctx, value):
        """Add a principal or a group to a local group; cycles are refused."""
        if not isinstance(value, dict) or set(value) - {'group', 'member_kind', 'member', 'role'} \
                or not {'group', 'member_kind', 'member'} <= set(value):
            raise ValueError('invalid_request')
        role = value.get('role', 'member')
        if role not in GROUP_ROLES or value['member_kind'] not in ('principal', 'group'):
            raise ValueError('invalid_group_member')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            group, override = self._manage_group(db, ctx, value['group'])
            kind, member = self._subject(db, ctx, {'subject_kind': value['member_kind'], 'subject': value['member']})
            if kind == 'group':
                if role != 'member':
                    raise ValueError('invalid_group_member')
                self._refuse_cycle(db, ctx['tenant'], group['id'], member)
            scope, focus = self._containment_scope(db, ctx, self._group_workspaces(db, ctx['tenant'], group['id']))
            before = self._contained_pairs(db, ctx['tenant'], scope, focus)
            result = self._write_member(db, ctx, group, kind, member, role, override=override)
            after = self._contained_pairs(db, ctx['tenant'], scope, focus)
            result.update(self._newly_unreadable(db, ctx['tenant'], before, after))
        return result

    @staticmethod
    def _refuse_cycle(db, tenant, group, member):
        if member == group or db.execute('''WITH RECURSIVE down(id) AS (SELECT ?::text UNION
                SELECT e.member FROM group_edges e JOIN down ON e.tenant=? AND e.group_id=down.id AND e.member_kind='group')
            SELECT 1 FROM down WHERE id=?''', (member, tenant, group)).fetchone():
            raise ValueError('group_cycle')

    def _write_member(self, db, ctx, group, kind, member, role, *, override=False):
        prior = db.execute('''SELECT role FROM group_edges WHERE tenant=? AND group_id=? AND member_kind=? AND member=?''',
                           (ctx['tenant'], group['id'], kind, member)).fetchone()
        changed = not prior or prior['role'] != role
        if changed:
            workspaces = self._group_workspaces(db, ctx['tenant'], group['id'])
            admins = {w: self._counting_admins(db, ctx['tenant'], w) for w in workspaces}
            group_admins = self._group_admins(db, ctx['tenant'], group['id'])
            db.execute('''INSERT INTO group_edges(tenant,group_id,member_kind,member,role,added_by,created)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant,group_id,member_kind,member) DO UPDATE SET role=excluded.role''',
                (ctx['tenant'], group['id'], kind, member, role, ctx['actor'], time.time()))
            self._refresh_groups(db, ctx['tenant'], [group['id']])
            self._guard_last_admins(db, ctx['tenant'], admins)
            if group['workspace'] is None and group_admins and not self._group_admins(db, ctx['tenant'], group['id']):
                raise ValueError('last_admin')
            self._audit_workspace(db, ctx, override, 'group_membership', 'added:' + role,
                                  json.dumps([group['id'], kind, member]))
        return {'group': group['id'], 'member_kind': kind, 'member': member, 'role': role, 'changed': changed}

    def remove_group_member(self, ctx, value):
        if not isinstance(value, dict) or set(value) != {'group', 'member_kind', 'member'} \
                or value['member_kind'] not in ('principal', 'group') or not isinstance(value['member'], str):
            raise ValueError('invalid_request')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            group, override = self._manage_group(db, ctx, value['group'])
            member = value['member']
            if value['member_kind'] == 'group':
                found = self._resolve_group(db, ctx['tenant'], member)
                member = found['id'] if found else member
            scope, focus = self._containment_scope(db, ctx, self._group_workspaces(db, ctx['tenant'], group['id']))
            before = self._contained_pairs(db, ctx['tenant'], scope, focus)
            removed = self._delete_member(db, ctx, group, value['member_kind'], member, override=override)
            after = self._contained_pairs(db, ctx['tenant'], scope, focus)
            changes = self._newly_unreadable(db, ctx['tenant'], before, after)
        return {'group': group['id'], 'member_kind': value['member_kind'], 'member': member, 'removed': removed,
                **changes}

    def _delete_member(self, db, ctx, group, kind, member, *, override=False):
        workspaces = self._group_workspaces(db, ctx['tenant'], group['id'])
        admins = {w: self._counting_admins(db, ctx['tenant'], w) for w in workspaces}
        group_admins = self._group_admins(db, ctx['tenant'], group['id'])
        removed = db.execute('''DELETE FROM group_edges WHERE tenant=? AND group_id=? AND member_kind=? AND member=?''',
                             (ctx['tenant'], group['id'], kind, member)).rowcount
        if removed:
            self._refresh_groups(db, ctx['tenant'], [group['id']])
            self._guard_last_admins(db, ctx['tenant'], admins)
            if group['workspace'] is None and group_admins and not self._group_admins(db, ctx['tenant'], group['id']):
                raise ValueError('last_admin')
            self._audit_workspace(db, ctx, override, 'group_membership', 'removed',
                                  json.dumps([group['id'], kind, member]))
        return bool(removed)

    def delete_group(self, ctx, value):
        """Delete a local group: its edges, its entries and any shares to it go with it."""
        if not isinstance(value, dict) or set(value) != {'group'}:
            raise ValueError('invalid_request')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            group, override = self._manage_group(db, ctx, value['group'])
            workspaces = self._group_workspaces(db, ctx['tenant'], group['id'])
            admins = {w: self._counting_admins(db, ctx['tenant'], w) for w in workspaces}
            parents = [r[0] for r in db.execute('''SELECT group_id FROM group_edges WHERE tenant=? AND member_kind='group'
                AND member=?''', (ctx['tenant'], group['id']))]
            db.execute("DELETE FROM group_edges WHERE tenant=? AND member_kind='group' AND member=?",
                       (ctx['tenant'], group['id']))
            db.execute("DELETE FROM workspace_entries WHERE tenant=? AND subject_kind='group' AND subject=?",
                       (ctx['tenant'], group['id']))
            db.execute('''UPDATE raw_source_grants SET revoked=?,revoked_by=? WHERE tenant=? AND grantee_kind='group'
                AND grantee=? AND revoked IS NULL''', (time.time(), ctx['actor'], ctx['tenant'], group['id']))
            db.execute('DELETE FROM group_closure WHERE tenant=? AND group_id=?', (ctx['tenant'], group['id']))
            db.execute('DELETE FROM groups WHERE tenant=? AND id=?', (ctx['tenant'], group['id']))
            if parents:
                self._refresh_groups(db, ctx['tenant'], parents)
            self._refresh_workspaces(db, ctx['tenant'], workspaces)
            self._guard_last_admins(db, ctx['tenant'], admins)
            self._audit_workspace(db, ctx, override, 'group_delete', 'deleted', group['id'])
        return {'group': group['id'], 'deleted': True}

    # Tenant policy (settings administrators and operators) ---------------------------------

    def tenant_policy(self, ctx=None, value=None):
        """Report or change workspace_creation, widest_entry and owner_sharing.

        With a request context it needs the settings_admin flag and the `settings`
        action; without one it is an operator command. Lowering widest_entry
        suspends wider entries at once; raising restores them."""
        from agenthub.enterprise import Denied
        value = dict(value or {})
        if set(value) - {'workspace_creation', 'widest_entry', 'owner_sharing'}:
            raise ValueError('invalid_request')
        if value.get('workspace_creation', 'anyone') not in CREATION or value.get('widest_entry', 'none') not in WIDEST \
                or value.get('owner_sharing', 'allowed') not in ('allowed', 'lower_only'):
            raise ValueError('invalid_tenant_policy')
        tenant = ctx['tenant'] if ctx else self.tenant_id
        actor = dict(ctx) if ctx else {'tenant': tenant, 'actor': OPERATOR, 'principal': OPERATOR}
        with (self.delivery_lock() if value else nullcontext()), self.open() as state, state.db:
            db = state.db
            if ctx is not None:
                if hasattr(self, 'current_identity'):
                    self.current_identity(db, ctx)
                if not self._settings_admin(db, ctx):
                    raise Denied()
            row = db.execute('''SELECT workspace_creation,widest_entry,owner_sharing FROM enterprise_organizations
                WHERE id=?''', (tenant,)).fetchone()
            if not row:
                raise ValueError('unknown_tenant')
            changed = {key: item for key, item in value.items() if row[key] != item}
            for key, item in changed.items():
                db.execute('UPDATE enterprise_organizations SET ' + key + '=? WHERE id=?', (item, tenant))
                self._audit(db, actor, 'tenant_policy', key + ':' + item, tenant)
            if 'widest_entry' in changed:
                db.execute('UPDATE enterprise_projects SET epoch=epoch+1 WHERE tenant=?', (tenant,))
            result = dict(row)
            result.update(changed)
        return {'tenant': tenant, **result, 'changed': sorted(changed)}

    # Operator commands (vaelius workspace ...) ------------------------------------------

    def _operator_ctx(self):
        return {'tenant': self.tenant_id, 'actor': OPERATOR, 'principal': OPERATOR, 'actions': set(),
                'delegated_projects': None}

    def operator_workspace(self, command, workspace, **options):
        """Operator workspace management on this tenant (no request context, audited as local_operator).

        Commands: show, set-entry (subject_kind, subject, role), remove-entry,
        settings (name, item_default, active), link/unlink (remote)."""
        ctx = self._operator_ctx()
        if command == 'show':
            with self.open() as state:
                db = state.db
                row = self._resolve_workspace(db, self.tenant_id, workspace)
                if not row:
                    raise ValueError('unknown_workspace')
                entries = [dict(e) for e in db.execute('''SELECT subject_kind,subject,role,granted_by,created
                    FROM workspace_entries WHERE tenant=? AND workspace=? ORDER BY subject_kind,subject''',
                    (self.tenant_id, row['id']))]
                repos = [r[0] for r in db.execute('SELECT remote FROM workspace_repos WHERE tenant=? AND workspace=?',
                                                  (self.tenant_id, row['id']))]
                return {'workspace': self._workspace_view(row), 'entries': entries, 'repos': repos,
                        'counting_admins': self._counting_admins(db, self.tenant_id, row['id']),
                        'audience_size': self._audience_size(db, self.tenant_id, row['id'])}
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            row = self._resolve_workspace(db, self.tenant_id, workspace)
            if not row:
                raise ValueError('unknown_workspace')
            if command == 'set-entry':
                _role_rank(options.get('role'))
                kind, subject = self._subject(db, ctx, options)
                return self._write_entry(db, ctx, row['id'], kind, subject, options['role'])
            if command == 'remove-entry':
                kind = options.get('subject_kind')
                if kind not in SUBJECT_KINDS:
                    raise ValueError('invalid_subject')
                subject = '' if kind == 'organization' else options.get('subject')
                if kind == 'group':
                    group = self._resolve_group(db, self.tenant_id, subject)
                    subject = group['id'] if group else subject
                return self._delete_entry(db, ctx, row['id'], kind, subject)
        if command in ('link', 'unlink'):
            from agentclient.general_contract import normalize_remote, REMOTE
            remote = options.get('remote')
            remote = remote if isinstance(remote, str) and REMOTE.fullmatch(remote) else normalize_remote(remote)
            if not remote:
                raise ValueError('invalid_remote')
            with self.delivery_lock(), self.open() as state, state.db:
                db = state.db
                row = self._resolve_workspace(db, self.tenant_id, workspace)
                current = db.execute('SELECT workspace FROM workspace_repos WHERE tenant=? AND remote=?',
                                     (self.tenant_id, remote)).fetchone()
                if command == 'link':
                    if current and current['workspace'] != row['id']:
                        raise ValueError('remote_linked_to_another_workspace')
                    if not current:
                        db.execute('INSERT INTO workspace_repos VALUES(?,?,?,?,?)',
                                   (self.tenant_id, remote, row['id'], OPERATOR, time.time()))
                elif current and current['workspace'] == row['id']:
                    db.execute('DELETE FROM workspace_repos WHERE tenant=? AND remote=?', (self.tenant_id, remote))
                self._bump_epoch(db, self.tenant_id, [row['id']])
                self._audit(db, ctx, 'repo_' + command, 'applied', json.dumps([row['id'], remote]))
            return {'workspace': row['id'], 'remote': remote, 'linked': command == 'link'}
        if command == 'settings':
            with self.delivery_lock(), self.open() as state, state.db:
                db = state.db
                row = self._resolve_workspace(db, self.tenant_id, workspace)
                for key in ('name', 'item_default', 'active'):
                    if options.get(key) is None:
                        continue
                    item = options[key]
                    if (key == 'name' and not NAME.fullmatch(str(item))) or \
                            (key == 'item_default' and item not in ITEM_DEFAULTS):
                        raise ValueError('invalid_workspace_settings')
                    db.execute('UPDATE enterprise_projects SET ' + key + '=?,epoch=epoch+1 WHERE tenant=? AND id=?',
                               (int(item) if key == 'active' else item, self.tenant_id, row['id']))
                    self._audit(db, ctx, 'workspace_settings', key, row['id'])
                if options.get('active') is not None:
                    groups = [r[0] for r in db.execute('SELECT id FROM groups WHERE tenant=? AND workspace=?',
                                                       (self.tenant_id, row['id']))]
                    if groups:
                        self._refresh_groups(db, self.tenant_id, groups)
                row = db.execute('SELECT * FROM enterprise_projects WHERE tenant=? AND id=?',
                                 (self.tenant_id, row['id'])).fetchone()
            return {'workspace': self._workspace_view(row)}
        raise ValueError('unknown_workspace_command')

    def workspace_access_report(self):
        """Operator summary: workspaces with entry counts, counting admins and item counts; no content."""
        with self.open() as state:
            db = state.db
            rows = db.execute('''SELECT p.id,p.name,p.active,p.org_role,p.item_default,
                (SELECT count(*) FROM workspace_entries e WHERE e.tenant=p.tenant AND e.workspace=p.id) entries,
                (SELECT count(*) FROM enterprise_sources s WHERE s.tenant=p.tenant AND s.external_project=p.id
                    AND s.active=1) items,
                (SELECT count(*) FROM enterprise_sources s WHERE s.tenant=p.tenant AND s.external_project=p.id
                    AND s.active=1 AND s.private=1) private_items
                FROM enterprise_projects p WHERE p.tenant=? ORDER BY p.id''', (self.tenant_id,)).fetchall()
            tenant = db.execute('SELECT workspace_creation,widest_entry,owner_sharing FROM enterprise_organizations WHERE id=?',
                                (self.tenant_id,)).fetchone()
            result = [dict(dict(row), organization_entry=role_name(row['org_role']), active=bool(row['active']),
                           counting_admins=self._counting_admins(db, self.tenant_id, row['id'])) for row in rows]
        for item in result:
            item.pop('org_role', None)
        return {'tenant': self.tenant_id, 'policy': dict(tenant) if tenant else None, 'workspaces': result,
                'without_admin': [item['id'] for item in result if not item['counting_admins']]}

    # Session labels (PR B) -----------------------------------------------------------

    # Principals in the audience of one workspace (bound: tenant, workspace, workspace,
    # tenant): every active principal with a role >= reader through a principal or group
    # entry (subject to widest_entry), plus, through the organization entry, every active
    # principal who is not under an operator quarantine. Mirrors search_workspace_role
    # for a non-delegated credential.
    _AUDIENCE_SQL = '''SELECT a.principal FROM workspace_access a
            JOIN enterprise_projects p ON p.tenant=a.tenant AND p.id=a.workspace AND p.active=1
            JOIN enterprise_organizations o ON o.id=a.tenant
            JOIN enterprise_principals who ON who.tenant=a.tenant AND who.id=a.principal AND who.active=1
            WHERE a.tenant=? AND a.workspace=? AND CASE o.widest_entry WHEN 'none' THEN a.principal_role=3
                ELSE greatest(a.principal_role,a.group_role)>=1 END
        UNION
        SELECT who.id FROM enterprise_principals who
            JOIN enterprise_projects p ON p.tenant=who.tenant AND p.id=? AND p.active=1 AND p.org_role>0
            JOIN enterprise_organizations o ON o.id=who.tenant AND o.widest_entry='organization'
            WHERE who.tenant=? AND who.active=1 AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                WHERE held.tenant=who.tenant AND held.principal=who.id)'''

    # Which candidate workspaces (and groups) contain the session audience `sa`
    # (extensional: current materialized membership, never entry lists). For each
    # workspace: an open one (an organization entry not suspended by widest_entry)
    # reaches every non-quarantined principal, so it only needs the quarantined members
    # of the session audience as direct members; any other needs all of them. `direct`
    # (for a legacy 'team' origin) ignores the organization entry. Set difference by
    # counting, over the (tenant, principal, workspace) primary key.
    _SUPERSET_SQL = '''WITH sa AS MATERIALIZED ({audience}),
        member AS MATERIALIZED (SELECT sa.principal,EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                WHERE held.tenant=? AND held.principal=sa.principal) held FROM sa),
        total AS (SELECT count(*) n,count(*) FILTER (WHERE member.held) nq FROM member),
        covered AS (SELECT a.workspace,count(*) n,count(*) FILTER (WHERE member.held) nq FROM member
            JOIN workspace_access a ON a.tenant=? AND a.principal=member.principal AND a.workspace=ANY(?::text[])
            JOIN enterprise_organizations o ON o.id=a.tenant
            WHERE CASE o.widest_entry WHEN 'none' THEN a.principal_role=3
                ELSE greatest(a.principal_role,a.group_role)>=1 END
            GROUP BY a.workspace),
        grouped AS (SELECT c.group_id,count(*) n FROM member JOIN group_closure c ON c.tenant=? AND c.principal=member.principal
            AND c.group_id=ANY(?::text[]) GROUP BY c.group_id)
        SELECT 'workspace' kind,p.id,
            CASE WHEN p.org_role>0 AND o.widest_entry='organization' THEN coalesce(c.nq,0)=t.nq
                ELSE coalesce(c.n,0)=t.n END AS contains,
            coalesce(c.n,0)=t.n AS direct,t.n total,
            CASE WHEN p.org_role>0 AND o.widest_entry='organization' THEN t.nq-coalesce(c.nq,0)
                ELSE t.n-coalesce(c.n,0) END AS missing
        FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant CROSS JOIN total t
        LEFT JOIN covered c ON c.workspace=p.id
        WHERE p.tenant=? AND p.id=ANY(?::text[]) AND p.active=1
        UNION ALL
        SELECT 'group',g.id,coalesce(x.n,0)=t.n,false,t.n,0 FROM groups g CROSS JOIN total t
        LEFT JOIN grouped x ON x.group_id=g.id WHERE g.tenant=? AND g.id=ANY(?::text[])
        UNION ALL
        SELECT 'self',max(member.principal),count(*)=1 AND bool_and(member.principal=?),false,count(*),0 FROM member'''

    def _session_label(self, db, ctx, label):
        """The recall filter for one session label (docs/ACCESS_MODEL.md section 7).

        `label` is the client's {workspace, narrowed, private, sharing}. The session
        audience is the intersection of the audiences of the workspace and every
        narrowed one, or only the person (a private session: `contributions_default`
        or the workspace's `item_default` is private, the label or its legacy sharing
        request says so, or the intersection is the person alone). Returns
        {'private': True, ...} or {'private': False, 'workspaces': S, 'direct': SD,
        'groups': G, 'principals': P}: S the workspaces the reader can read whose
        audience contains the session audience, SD those whose direct (non-organization)
        audience does, G the reader's groups whose members contain it and P the
        principal a share must name to contain it (only ever the reader alone, which
        makes the session private). Decided on current state at every request."""
        from agenthub.enterprise import Denied
        workspace = self._resolve_workspace(db, ctx['tenant'], label['workspace'], active_only=True)
        if not workspace:
            raise Denied()
        names = [workspace['id']]
        for ref in label.get('narrowed') or ():
            row = self._resolve_workspace(db, ctx['tenant'], ref, active_only=True)
            if not row:
                raise Denied()
            names.append(row['id'])
        names = list(dict.fromkeys(names))
        roles = self._roles(db, ctx)
        readable = sorted(name for name, (role, _) in roles.items() if role >= READER)
        person = db.execute('SELECT contributions_default FROM enterprise_principals WHERE tenant=? AND id=?',
                            (ctx['tenant'], ctx['actor'])).fetchone()
        requested = label.get('sharing')
        # A read-only chat writes nothing, so it cannot leak what it recalls: it gets the
        # person's full readable set, like a private session.
        private = bool(label.get('private') or label.get('read_only') or requested == 'private'
                       or not person or person['contributions_default'] == 'private'
                       or workspace['item_default'] == 'private'
                       or (requested == 'team' and workspace['org_role'] > 0))
        epoch = db.execute('SELECT coalesce(sum(epoch),0) FROM enterprise_projects WHERE tenant=?',
                           (ctx['tenant'],)).fetchone()[0]
        result = {'workspace': workspace['id'], 'narrowed': names[1:], 'readable': readable, 'epoch': int(epoch),
                  'private': private, 'workspaces': readable, 'direct': readable, 'groups': [], 'principals': []}
        if private:
            return result
        audience = ' INTERSECT '.join('(' + self._AUDIENCE_SQL + ')' for _ in names)
        args = [value for name in names for value in (ctx['tenant'], name, name, ctx['tenant'])]
        groups = [r[0] for r in db.execute('SELECT group_id FROM group_closure WHERE tenant=? AND principal=?',
                                           (ctx['tenant'], ctx['actor'])).fetchall()]
        rows = db.execute(self._SUPERSET_SQL.format(audience=audience), (
            *args, ctx['tenant'], ctx['tenant'], readable, ctx['tenant'], groups, ctx['tenant'], readable,
            ctx['tenant'], groups, ctx['actor'])).fetchall()
        alone = any(r['kind'] == 'self' and r['contains'] for r in rows)
        if alone:
            return dict(result, private=True)
        result.update(workspaces=sorted(r['id'] for r in rows if r['kind'] == 'workspace' and r['contains']),
                      direct=sorted(r['id'] for r in rows if r['kind'] == 'workspace' and r['direct']),
                      groups=sorted(r['id'] for r in rows if r['kind'] == 'group' and r['contains']),
                      missing={r['id']: int(r['missing']) for r in rows if r['kind'] == 'workspace' and not r['contains']})
        return result

    # Who of a session's audience a narrower workspace leaves out, named only through
    # the access list of the session's own workspace (what any of its readers sees in
    # `workspace show`): a principal entry by id, else the group entry that brings the
    # person in. People who come in only through the organization entry are counted,
    # never named. Only people in the session audience who are NOT readers of the
    # target are considered, so nothing about the target's own members is revealed.
    _MISSING_SQL = '''WITH sa AS MATERIALIZED ({audience}),
        named AS MATERIALIZED (SELECT sa.principal,
                min(CASE e.subject_kind WHEN 'principal' THEN '0:'||e.subject ELSE '1:'||g.name END) label
            FROM sa JOIN workspace_entries e ON e.tenant=? AND e.workspace=? AND e.subject_kind IN ('principal','group')
            LEFT JOIN groups g ON g.tenant=e.tenant AND g.id=e.subject AND e.subject_kind='group'
            WHERE (e.subject_kind='principal' AND e.subject=sa.principal)
                OR (g.id IS NOT NULL AND EXISTS(SELECT 1 FROM group_closure c WHERE c.tenant=e.tenant
                    AND c.group_id=e.subject AND c.principal=sa.principal))
            GROUP BY sa.principal)
        SELECT t.w,named.label,count(*) n FROM unnest(?::text[]) t(w) CROSS JOIN named
        WHERE coalesce((search_workspace_role(?,named.principal,t.w,NULL)).role,0)<1
        GROUP BY t.w,named.label'''

    def _missing_readers(self, db, ctx, session, targets):
        """{workspace: {'count', 'names', 'more'}} for narrower workspaces of a shared session.

        `count` is how many people of the session audience cannot read the target;
        `names` at most MISSING_NAMES labels (principal ids, then group names) drawn
        from the session workspace's own access list; `more` the people not covered
        by those labels."""
        targets = sorted(targets)[:MISSING_WORKSPACES]
        if not targets:
            return {}
        names = [session['workspace'], *session['narrowed']]
        audience = ' INTERSECT '.join('(' + self._AUDIENCE_SQL + ')' for _ in names)
        args = [value for name in names for value in (ctx['tenant'], name, name, ctx['tenant'])]
        labels = {}
        for row in db.execute(self._MISSING_SQL.format(audience=audience),
                              (*args, ctx['tenant'], session['workspace'], targets, ctx['tenant'])).fetchall():
            labels.setdefault(row['w'], []).append((row['label'], int(row['n'])))
        found = {}
        for workspace in targets:
            count = int(session.get('missing', {}).get(workspace, 0))
            shown = sorted(labels.get(workspace, ()))[:MISSING_NAMES]
            covered = sum(n for _, n in shown)
            found[workspace] = {'count': count,
                                'names': [label[2:] if label[0] == '0' else label[2:] + ' (group)'
                                          for label, _ in shown],
                                'more': max(count - covered, 0)}
        return found

    def _label(self, db, ctx):
        """This request's session-label recall filter, computed once, on first use, inside
        a transaction the request already holds (`session_label_request` from the API;
        an explicit `session_label`, including None, wins)."""
        if 'session_label' in ctx or ctx.get('session_label_request') is None:
            return ctx.get('session_label')
        holder = ctx.setdefault('session_label_cache', {})
        if 'filter' not in holder:
            holder['filter'] = self._session_label(db, ctx, validate_session_label(ctx['session_label_request']))
        ctx['session_label'] = holder['filter']
        return holder['filter']

    def session_filter(self, ctx, label):
        """ctx with its `session_label` recall filter (None keeps today's behavior)."""
        if label is None:
            return ctx
        label = validate_session_label(label)
        with self.open() as state:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            return dict(ctx, session_label=self._session_label(db, ctx, label))

    def _session_admits(self, db, ctx, source, *, granted=False):
        """The session-label term of the recall filter for one source (Python oracle).

        Mirrors search_permissions.SESSION and search_session_admits (migration 049):
        never origin_private; the item's workspace in S when it is not private, or
        `granted` (the document reaches the reader through shares whose subjects
        contain the session audience); every carried workspace in S; a legacy 'team'
        origin needs the workspace and every carried one in SD. A private session, or
        a request without a label, admits everything the reader can read."""
        session = ctx.get('session_label')
        if not session or session.get('private'):
            return True
        ceiling, _, carried = _origin(source)
        if ceiling == 'private' or ceiling not in SHARING:
            return False
        inside = set(session['workspaces'])
        if not ((not int(source.get('private') or 0) and source['external_project'] in inside) or granted):
            return False
        if any(name not in inside for name in carried):
            return False
        if ceiling == 'team':
            direct = set(session['direct'])
            if source['external_project'] not in direct or any(name not in direct for name in carried):
                return False
        return True

    def _session_granted(self, db, ctx, source):
        """A share of this source to the reader names a subject whose members contain the
        session audience (a group in G, or a principal in P)."""
        session = ctx.get('session_label') or {}
        return any((row['grantee_kind'] == 'principal' and row['grantee'] in session.get('principals', ()))
                   or (row['grantee_kind'] == 'group' and row['grantee'] in session.get('groups', ()))
                   for row in self._share_rows(db, ctx, source, 'passages'))

    def session_descriptor(self, ctx, value):
        """The client's advisory audience descriptor for a session label (section 5).

        `value` is {workspace, narrowed?, private?, sharing?}; a narrowed entry may be
        a workspace name or id, or a normalized repository remote linked to one. The
        reader needs `read` and a role >= reader in every named workspace; anything
        else is refused (403), so `vaelius-client session use` and the MCP tool
        `use_workspace` cannot name a workspace the person cannot read."""
        from agenthub.enterprise import Denied
        self._need(ctx, 'read')
        value = dict(value) if isinstance(value, dict) else value
        if isinstance(value, dict) and isinstance(value.get('narrowed'), list):
            with self.open() as state:
                value['narrowed'] = [self._narrow_target(state.db, ctx, ref) for ref in value['narrowed']]
        label = validate_session_label(value)
        now = time.time()
        with self.open() as state:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            roles = self._roles(db, ctx)
            for ref in [label['workspace'], *label.get('narrowed', ())]:
                row = self._resolve_workspace(db, ctx['tenant'], ref, active_only=True)
                if not row or roles.get(row['id'], (0, 0))[0] < READER:
                    raise Denied()
            session = self._session_label(db, ctx, label)
            workspace = self._resolve_workspace(db, ctx['tenant'], label['workspace'], active_only=True)
            names = {r['id']: r['name'] for r in db.execute(
                'SELECT id,name FROM enterprise_projects WHERE tenant=? AND id=ANY(?::text[])',
                (ctx['tenant'], session['readable'])).fetchall()}
            person = db.execute('SELECT contributions_default FROM enterprise_principals WHERE tenant=? AND id=?',
                                (ctx['tenant'], ctx['actor'])).fetchone()
            repos = [r[0] for r in db.execute('SELECT remote FROM workspace_repos WHERE tenant=? AND workspace=? '
                                              'ORDER BY remote', (ctx['tenant'], workspace['id']))]
            epoch = session['epoch']
            narrower = []
            if not session['private']:
                outside = sorted(set(session['readable']) - set(session['workspaces']))
                missing = self._missing_readers(db, ctx, session, outside)
                hidden = {r[0]: int(r[1]) for r in db.execute('''SELECT external_project,count(*) FROM enterprise_sources
                    WHERE tenant=? AND external_project=ANY(?::text[]) AND active=1 AND private=0
                    GROUP BY external_project''', (ctx['tenant'], sorted(missing)))} if missing else {}
                narrower = [{'id': ident, 'name': names.get(ident, ident), 'missing_readers': missing[ident],
                             'hidden_items': hidden.get(ident, 0)} for ident in sorted(missing)]
        listed = lambda ids: [{'id': ident, 'name': names.get(ident, ident)} for ident in ids]
        return {'workspace': {'id': workspace['id'], 'name': workspace['name']}, 'epoch': int(epoch),
                'role': role_name(roles.get(workspace['id'], (0, 0))[0]), 'item_default': workspace['item_default'],
                'visibility': 'org' if workspace['org_role'] else 'private',
                'contributions_default': person['contributions_default'] if person else 'private',
                'session': {'workspace': workspace['id'], 'narrowed': session['narrowed'],
                            'private': session['private']},
                'contains_session_audience': listed(session['workspaces']), 'readable': listed(session['readable']),
                'narrower': narrower, 'repos': repos, 'fetched_at': int(now), 'refresh_after': int(now + DESCRIPTOR_REFRESH),
                'expires_at': int(now + DESCRIPTOR_EXPIRY)}

    def _narrow_target(self, db, ctx, ref):
        """A workspace id for `use`: a workspace name or id, else a linked remote."""
        if not isinstance(ref, str):
            raise ValueError('invalid_session_label')
        row = self._resolve_workspace(db, ctx['tenant'], ref)
        if row:
            return row['id']
        from agentclient.general_contract import normalize_remote, REMOTE
        remote = ref if REMOTE.fullmatch(ref) else (normalize_remote(ref) if ('/' in ref or ':' in ref) else None)
        linked = db.execute('SELECT workspace FROM workspace_repos WHERE tenant=? AND remote=?',
                            (ctx['tenant'], remote)).fetchone() if remote else None
        if not linked:
            from agenthub.enterprise import Denied
            raise Denied()
        return linked['workspace']


# Recall routes that honor a session label (`session_label` in the body; the MCP
# endpoint passes its connection's label the same way). The label only narrows.
LABELLED_ROUTES = ('/enterprise/v3/search', '/enterprise/v3/document-evidence', '/enterprise/v3/document-context',
                   '/enterprise/v3/timeline', '/enterprise/v3/project-history', '/enterprise/v3/project-episode',
                   '/enterprise/v3/preferences')
# The client refreshes its descriptor after 15 minutes and stops trusting it after 24 hours.
DESCRIPTOR_REFRESH = 900
DESCRIPTOR_EXPIRY = 86400
# A narrower workspace's "missing readers" summary names at most this many people or
# groups; the descriptor summarizes at most this many narrower workspaces.
MISSING_NAMES = 3
MISSING_WORKSPACES = 64
# `newly_unreadable` is computed when the caller reads at most this many workspaces.
CONTAINMENT_SCOPE = 200
SESSION_LABEL_KEYS = {'workspace', 'narrowed', 'private', 'sharing', 'read_only'}
MAX_NARROWED = 16


def validate_session_label(label):
    """{workspace, narrowed, private, sharing} from a client; raises ValueError."""
    if (not isinstance(label, dict) or set(label) - SESSION_LABEL_KEYS
            or not isinstance(label.get('workspace'), str) or not NAME.fullmatch(label['workspace'])):
        raise ValueError('invalid_session_label')
    narrowed = label.get('narrowed', [])
    if (not isinstance(narrowed, list) or len(narrowed) > MAX_NARROWED
            or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in narrowed)):
        raise ValueError('invalid_session_label')
    if type(label.get('private', False)) is not bool or label.get('sharing', 'organization') not in SHARING:
        raise ValueError('invalid_session_label')
    if type(label.get('read_only', False)) is not bool:
        raise ValueError('invalid_session_label')
    return {'workspace': label['workspace'], 'narrowed': list(dict.fromkeys(narrowed)),
            'private': label.get('private', False), 'sharing': label.get('sharing', 'organization'),
            'read_only': label.get('read_only', False)}
