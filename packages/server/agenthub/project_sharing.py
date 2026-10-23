"""Sharing levels for captured content: one ladder, private < team < organization.

The same three levels apply to conversation passages (the redacted passage tier:
search results, cited evidence and conversation context) and, separately, to
retained originals (the unredacted canonical source). Curated and derived
documents keep the level of the capture connection's visibility.

Who controls each term of the minimum:

* the **person** (the source's author): `enterprise_principals.max_sharing`,
  default `private`, changed only by that person (`set_personal_sharing`);
* the **repository**: a scope file's `sharing` / `originals` request. A request
  is never a grant: anyone who can edit a repository can edit its scope file;
* the **operator**: the project ceiling (`enterprise_projects.max_sharing`, or
  the tenant default `default_max_sharing` for a project without one) and the
  tenant ceiling (`enterprise_organizations.max_sharing`);
* the **connection owner**: the capture connection's visibility;
* the **origin**: what the captured content came from (`agentclient.origin`,
  stored per source as `origin_ceiling`, `origin_reason`, `origin_projects`). The
  origin ceiling caps the level; every other project whose content the source
  carries adds that project's current ceiling to the minimum, and a `team`
  reader must be an active member of the source project AND of every one of
  them. The origin is fixed at ingest: no later policy change widens it.

At ingest the effective level of a source is the minimum of all of them, stored
per source (`enterprise_sources.sharing`, `originals_sharing`) next to the request
(`sharing_requested`, `originals_requested`). Originals never exceed passages.

At query time every ceiling is applied again to the stored level, so lowering
any of them hides content on the next request, and raising one never exposes a
source that was stored lower. A reader other than the author needs the current
level to reach them: `team` = an active member of the source project;
`organization` = any active, non-quarantined principal of the tenant. Every
ordinary source rule still applies on top (tenant, lifecycle, delegation,
connection readers and freshness, visibility, quarantine). Credentials without
`read` read no passages; originals additionally need `source_read`
(agenthub.raw_access), and explicit owner grants keep working.

Operator methods take no request context and audit as `local_operator`.
"""
from contextlib import nullcontext
import re
import time

SHARING = ('private', 'team', 'organization')
RANK = {level: rank for rank, level in enumerate(SHARING)}
OPERATOR = 'local_operator'


def lowest(*levels):
    """The most restrictive of the given levels; anything unknown counts as private."""
    return SHARING[min(RANK.get(level, 0) for level in levels)] if levels else 'private'


def _origin(item):
    """(ceiling, reason, projects) of a v2 event (`origin_label`) or a stored source row."""
    import json
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
    """Ceiling from the content's origin (origin labels and session taint).

    Read from an event's `origin_label` at ingest, or from the stored source
    (`enterprise_sources.origin_ceiling`) at query time. Absent means organization:
    content that only touched its own project is not restricted by its origin.
    Anything unrecognised is private. The SQL mirror reads the policy-definition
    key `origin_ceiling`.
    """
    value = _origin(item)[0]
    return value if value in SHARING else 'private'


def origin_projects(item=None):
    """Other projects whose content an event or source carries (`origin_projects`)."""
    return _origin(item)[2]


def effective_sharing(requested, *ceilings):
    """Minimum of the request and every ceiling (person, project, tenant, connection, origin)."""
    return lowest(requested, *ceilings)


class ProjectSharingMixin:
    # Ceilings -----------------------------------------------------------------

    @staticmethod
    def _sharing_ceiling(db, tenant, project):
        """A project's operator ceiling: its own setting (else the tenant default) under the tenant ceiling."""
        row = db.execute('''SELECT coalesce(p.max_sharing,o.default_max_sharing) ceiling,o.max_sharing tenant
            FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
            WHERE p.tenant=? AND p.id=? AND p.active=1''', (tenant, project)).fetchone()
        return lowest(row['ceiling'], row['tenant']) if row else 'private'

    @staticmethod
    def _ceilings(db, tenant, project, author):
        """Current person, project and tenant ceilings for one author's project, else None."""
        row = db.execute('''SELECT who.max_sharing person,coalesce(p.max_sharing,o.default_max_sharing) project,
                o.max_sharing tenant
            FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
            JOIN enterprise_principals who ON who.tenant=p.tenant AND who.id=?
            WHERE p.tenant=? AND p.id=? AND p.active=1''', (author, tenant, project)).fetchone()
        return dict(row) if row else None

    def _current_level(self, db, source, key='sharing'):
        """A source's stored level (`sharing` or `originals_sharing`) under every current ceiling."""
        ceilings = self._ceilings(db, source['tenant'], source['external_project'], source['owner'])
        if not ceilings:
            return 'private'
        return effective_sharing(source.get(key) or 'private', ceilings['person'], ceilings['project'],
                                 ceilings['tenant'], source['visibility'], origin_ceiling(source),
                                 *self._origin_ceilings(db, source['tenant'], origin_projects(source)))

    def _origin_ceilings(self, db, tenant, projects):
        """Current ceiling of each other project a source carries; unknown or inactive ones are private."""
        return [self._sharing_ceiling(db, tenant, project) for project in projects]

    # Ingest ------------------------------------------------------------------

    def _ingest_sharing(self, db, ctx, connection, value):
        """(requested, effective, originals requested, originals effective) for one v2 event.

        A request above what the ceilings allow is stored at the lower level and audited.
        """
        requested = value.get('sharing', 'private')
        originals = value.get('originals', 'private')
        if requested not in SHARING or originals not in SHARING:
            raise ValueError('invalid_sharing_request')
        ceilings = self._ceilings(db, ctx['tenant'], value['project'], ctx['actor']) or {
            'person': 'private', 'project': 'private', 'tenant': 'private'}
        effective = effective_sharing(requested, ceilings['person'], ceilings['project'], ceilings['tenant'],
                                      connection['visibility'], origin_ceiling(value),
                                      *self._origin_ceilings(db, ctx['tenant'], origin_projects(value)))
        # Unredacted originals are never shared more widely than their redacted passages.
        originals_effective = effective_sharing(originals, effective)
        return requested, effective, originals, originals_effective

    # Query-time oracle ---------------------------------------------------------

    def _organization_reader(self, db, ctx):
        """The reader is an active, non-quarantined principal of the tenant (the organization audience)."""
        row = db.execute('''SELECT 1 FROM enterprise_principals who WHERE who.tenant=? AND who.id=? AND who.active=1
            AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                WHERE held.tenant=who.tenant AND held.principal=who.id)''', (ctx['tenant'], ctx['actor'])).fetchone()
        return bool(row)

    def _level_reaches(self, db, ctx, project, level, others=()):
        """The level reaches the reader: organization, or team as a member of the project
        and of every other project whose content the source carries."""
        if level == 'organization' and self._organization_reader(db, ctx):
            return True
        return (RANK.get(level, 0) >= RANK['team'] and self._project_member(db, ctx, project)
                and all(db.execute('''SELECT 1 FROM enterprise_memberships m WHERE m.tenant=? AND m.project=?
                    AND m.principal=? AND m.active=1''', (ctx['tenant'], name, ctx['actor'])).fetchone()
                        for name in others))

    def _origin_reaches(self, db, ctx, source):
        """Python mirror of search_permissions.ORIGIN_REACHES for one source row and a non-owner.

        Applies to every document, curated and derived ones included: the stored
        origin ceiling and the current ceiling of every other project the source
        carries (unknown or inactive = private) give a level; organization changes
        nothing, team needs an active member of the source project and of every
        other project, private reaches nobody but the owner."""
        ceiling, _, others = _origin(source)
        if source.get('origin_ceiling') is None and not others:
            return True
        project = db.execute('''SELECT p.id,o.default_max_sharing FROM enterprise_projects p
            JOIN enterprise_organizations o ON o.id=p.tenant
            WHERE p.tenant=? AND p.id=? AND p.active=1''', (source['tenant'], source['external_project'])).fetchone()
        if not project:
            return False
        levels = [origin_ceiling(source)]
        for name in others:
            row = db.execute('''SELECT coalesce(p.max_sharing,o.default_max_sharing) level FROM enterprise_projects p
                JOIN enterprise_organizations o ON o.id=p.tenant WHERE p.tenant=? AND p.id=? AND p.active=1''',
                (source['tenant'], name)).fetchone()
            levels.append(row['level'] if row else 'private')
        level = lowest(*levels)
        if level == 'organization':
            return True
        if level != 'team':
            return False
        return all(db.execute('''SELECT 1 FROM enterprise_memberships m WHERE m.tenant=? AND m.project=?
            AND m.principal=? AND m.active=1''', (source['tenant'], name, ctx['actor'])).fetchone()
                   for name in (source['external_project'], *others))

    def _passage_shared(self, db, ctx, source):
        """Python mirror of search_permissions.SHARE for one source row (reader is not the owner)."""
        if source.get('sharing') not in ('team', 'organization'):
            return False
        return self._level_reaches(db, ctx, source['external_project'], self._current_level(db, source),
                                   origin_projects(source))

    def _originals_shared(self, db, ctx, source):
        """A retained original reaches a non-owner through its own level and every ordinary rule."""
        if source.get('originals_sharing') not in ('team', 'organization'):
            return False
        level = lowest(self._current_level(db, source, 'originals_sharing'), self._current_level(db, source))
        return (self._level_reaches(db, ctx, source['external_project'], level, origin_projects(source))
                and self._visible_source(db, ctx, source))

    # Explicit owner grants (agenthub.owner_controls) ------------------------------

    def _origin_level(self, db, source):
        """The origin term alone (for reports): origin ceiling under each carried project's ceiling."""
        carried = []
        for name in origin_projects(source):
            row = db.execute('''SELECT coalesce(p.max_sharing,o.default_max_sharing) ceiling
                FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
                WHERE p.tenant=? AND p.id=? AND p.active=1''', (source['tenant'], name)).fetchone()
            carried.append(row['ceiling'] if row else 'private')
        return lowest(origin_ceiling(source), *carried)

    def _passage_granted(self, db, ctx, source):
        """Python mirror of search_permissions.SOURCE_GRANTED for one source row.

        An active passage grant from the source's owner (source, its session or its
        connection) to the reader or a project the reader is an active member of;
        never while the operator restricts owner-issued grants (owner_sharing), in
        an inactive project, or to a quarantined reader. Sharing
        levels and their ceilings govern sharing with a whole audience; a grant
        names its readers and is governed by owner_sharing instead.
        The ordinary source rules other than visibility are the caller's.
        """
        if (not source or source['tenant'] != ctx['tenant'] or not source['active']
                or source['owner'] == ctx['actor']):
            return False
        if db.execute('SELECT 1 FROM enterprise_principal_quarantine WHERE tenant=? AND principal=?',
                      (ctx['tenant'], ctx['actor'])).fetchone():
            return False
        row = db.execute('''SELECT o.owner_sharing FROM enterprise_organizations o
            JOIN enterprise_projects p ON p.tenant=o.id AND p.id=? AND p.active=1
            WHERE o.id=?''', (source['external_project'], source['tenant'])).fetchone()
        if not row:
            return False
        scopes = {('source', source['id'])}
        session = db.execute('SELECT session FROM memories WHERE id=?', (source['id'],)).fetchone()
        if session and session['session'] is not None:
            scopes.add(('session', session['session']))
        connection = db.execute('SELECT connection FROM backend_source_revisions WHERE source_id=?',
                                (source['id'],)).fetchone()
        if connection:
            scopes.add(('connection', connection['connection']))
        grants = db.execute('''SELECT scope_kind,scope_id,grantee_kind,grantee,issued_with FROM raw_source_grants
            WHERE tenant=? AND revoked IS NULL AND passages=1 AND granted_by=?''',
            (source['tenant'], source['owner'])).fetchall()
        if not any((grant['scope_kind'], grant['scope_id']) in scopes
                   and (grant['issued_with'] == 'policy' or row['owner_sharing'] == 'allowed')
                   and ((grant['grantee_kind'] == 'principal' and grant['grantee'] == ctx['actor'])
                        or (grant['grantee_kind'] == 'project' and self._active_member(db, ctx, grant['grantee'])))
                   for grant in grants):
            return False
        # The origin (migration 046) is one of the ordinary rules every reader passes
        # (EnterpriseStore._source_rules); a grant never stands in for it.
        return True

    @staticmethod
    def _active_member(db, ctx, project, *, project_active=True):
        """The reader's own active membership; delegation limits are the caller's ordinary rules."""
        return bool(db.execute('''SELECT 1 FROM enterprise_memberships m JOIN enterprise_projects p
            ON p.tenant=m.tenant AND p.id=m.project WHERE m.tenant=? AND m.project=? AND m.principal=?
            AND m.active=1''' + (' AND p.active=1' if project_active else ''),
            (ctx['tenant'], project, ctx['actor'])).fetchone())

    def _passage_readers(self, db, source):
        """Principals whose redacted-passage tier covers this source under current policy.

        A policy report for `explain`, not a credential check: the owner, plus the
        audience of the source's current level when every ordinary source rule
        (visibility, quarantine, connection readers) admits them.
        """
        import json
        if not source or not source['active']:
            return []
        readers = {source['owner']}
        level = self._current_level(db, source) if source.get('sharing') in ('team', 'organization') else 'private'
        if level != 'private' and not source.get('principal_quarantine'):
            members = None
            for name in (source['external_project'], *origin_projects(source)):
                these = {row['principal'] for row in db.execute('''SELECT m.principal FROM enterprise_memberships m
                    JOIN enterprise_projects p ON p.tenant=m.tenant AND p.id=m.project
                    JOIN enterprise_principals who ON who.tenant=m.tenant AND who.id=m.principal
                    WHERE m.tenant=? AND m.project=? AND m.active=1 AND p.active=1 AND who.active=1''',
                    (source['tenant'], name))}
                members = these if members is None else members & these
            if level == 'organization':
                members |= {row['id'] for row in db.execute('''SELECT who.id FROM enterprise_principals who
                    WHERE who.tenant=? AND who.active=1 AND NOT EXISTS(SELECT 1 FROM enterprise_principal_quarantine held
                        WHERE held.tenant=who.tenant AND held.principal=who.id)''', (source['tenant'],))}
            link = db.execute('''SELECT c.owner,c.reader_ids FROM backend_source_revisions r
                JOIN backend_connections c ON c.id=r.connection WHERE r.source_id=?''', (source['id'],)).fetchone()
            listed = json.loads(link['reader_ids']) if link else []
            if listed:
                members &= set(listed) | {link['owner']}
            readers |= members
        return sorted(readers)

    # The person's own setting -------------------------------------------------

    def sharing_setting(self, ctx, value):
        """Self-service: report, or (with `max_sharing`) change, the caller's own ceiling.

        Lowering hides the person's shared passages and originals on the next
        request. Raising applies to new captures only: sources stored lower stay
        lower. A delegated credential can read the setting but never change it.
        """
        from agenthub.enterprise import Denied
        if not isinstance(value, dict) or set(value) - {'max_sharing'}:
            raise ValueError('invalid_sharing_request')
        level = value.get('max_sharing')
        if 'max_sharing' in value and level not in SHARING:
            raise ValueError('invalid_max_sharing')
        if level is not None and ctx.get('acting_for'):
            raise Denied()
        # A change is a policy commit: it serializes with delivery like any other.
        with (self.delivery_lock() if level is not None else nullcontext()), self.open() as state, state.db:
            db = state.db
            if hasattr(self, 'current_identity'):
                self.current_identity(db, ctx)
            row = db.execute('''SELECT p.max_sharing,o.max_sharing tenant_ceiling FROM enterprise_principals p
                JOIN enterprise_organizations o ON o.id=p.tenant WHERE p.tenant=? AND p.id=? AND p.active=1''',
                (ctx['tenant'], ctx['actor'])).fetchone()
            if not row:
                raise Denied()
            changed = level is not None and level != row['max_sharing']
            if changed:
                db.execute('UPDATE enterprise_principals SET max_sharing=? WHERE tenant=? AND id=?',
                           (level, ctx['tenant'], ctx['actor']))
                self._audit(db, ctx, 'personal_sharing', level, ctx['actor'])
        return {'principal': ctx['actor'], 'max_sharing': level if level is not None else row['max_sharing'],
                'tenant_ceiling': row['tenant_ceiling'], 'changed': changed}

    # Operator policy -----------------------------------------------------------

    def _sharing_audit(self, db, operation, disposition, target):
        db.execute('''INSERT INTO enterprise_audit(tenant,actor,operation,disposition,target_id,created)
            VALUES(?,?,?,?,?,?)''', (self.tenant_id, OPERATOR, operation, disposition, target, time.time()))

    def set_project_sharing(self, project, max_sharing):
        """Set one project's ceiling ('private', 'team', 'organization') or None to inherit the tenant default."""
        if max_sharing is not None and max_sharing not in SHARING:
            raise ValueError('invalid_max_sharing')
        if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', project):
            raise ValueError('invalid_project')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            prior = db.execute('SELECT max_sharing FROM enterprise_projects WHERE tenant=? AND id=?',
                               (self.tenant_id, project)).fetchone()
            if not prior:
                raise ValueError('unknown_project')
            db.execute('UPDATE enterprise_projects SET max_sharing=? WHERE tenant=? AND id=?',
                       (max_sharing, self.tenant_id, project))
            self._sharing_audit(db, 'project_sharing', max_sharing or 'inherit', project)
            ceiling = self._sharing_ceiling(db, self.tenant_id, project)
        return {'project': project, 'max_sharing': max_sharing, 'effective_ceiling': ceiling,
                'previous': prior['max_sharing']}

    def set_tenant_sharing_default(self, max_sharing):
        """Ceiling for every project without its own setting."""
        if max_sharing not in SHARING:
            raise ValueError('invalid_max_sharing')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            changed = db.execute('UPDATE enterprise_organizations SET default_max_sharing=? WHERE id=?',
                                 (max_sharing, self.tenant_id)).rowcount
            if not changed:
                raise ValueError('unknown_tenant')
            self._sharing_audit(db, 'tenant_sharing_default', max_sharing, self.tenant_id)
        return {'tenant': self.tenant_id, 'default_max_sharing': max_sharing}

    def set_tenant_sharing_ceiling(self, max_sharing):
        """Hard ceiling for the whole tenant, below which every project and person stays."""
        if max_sharing not in SHARING:
            raise ValueError('invalid_max_sharing')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            changed = db.execute('UPDATE enterprise_organizations SET max_sharing=? WHERE id=?',
                                 (max_sharing, self.tenant_id)).rowcount
            if not changed:
                raise ValueError('unknown_tenant')
            self._sharing_audit(db, 'tenant_sharing_ceiling', max_sharing, self.tenant_id)
        return {'tenant': self.tenant_id, 'max_sharing': max_sharing}

    def sharing_policy_status(self):
        """Ceilings, person-setting counts and per-project source counts by stored level; no content or IDs."""
        with self.open() as state:
            db = state.db
            tenant = db.execute('SELECT default_max_sharing,max_sharing FROM enterprise_organizations WHERE id=?',
                                (self.tenant_id,)).fetchone()
            projects = db.execute('''SELECT p.id,p.max_sharing,p.active,
                coalesce(p.max_sharing,o.default_max_sharing) ceiling
                FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
                WHERE p.tenant=? ORDER BY p.id''', (self.tenant_id,)).fetchall()
            counts = db.execute('''SELECT external_project,sharing_requested,sharing,originals_requested,
                originals_sharing,count(*) n FROM enterprise_sources WHERE tenant=? AND active=1
                GROUP BY external_project,sharing_requested,sharing,originals_requested,originals_sharing''',
                (self.tenant_id,)).fetchall()
            people = db.execute('''SELECT max_sharing,count(*) n FROM enterprise_principals
                WHERE tenant=? AND active=1 GROUP BY max_sharing''', (self.tenant_id,)).fetchall()

        def empty():
            return {'private': 0, 'team': 0, 'organization': 0, 'requested_capped': 0,
                    'originals': {'private': 0, 'team': 0, 'organization': 0}}
        sources = {}
        for row in counts:
            entry = sources.setdefault(row['external_project'], empty())
            entry[row['sharing']] += row['n']
            entry['originals'][row['originals_sharing']] += row['n']
            if (RANK[row['sharing_requested']] > RANK[row['sharing']]
                    or RANK[row['originals_requested']] > RANK[row['originals_sharing']]):
                entry['requested_capped'] += row['n']
        return {'tenant': self.tenant_id,
                'default_max_sharing': tenant['default_max_sharing'] if tenant else None,
                'max_sharing': tenant['max_sharing'] if tenant else None,
                'people': {level: next((row['n'] for row in people if row['max_sharing'] == level), 0)
                           for level in SHARING},
                'projects': [{'project': row['id'], 'max_sharing': row['max_sharing'],
                              'effective_ceiling': lowest(row['ceiling'], tenant['max_sharing']) if tenant else 'private',
                              'active': bool(row['active']),
                              'active_sources': sources.get(row['id'], empty())}
                             for row in projects]}
