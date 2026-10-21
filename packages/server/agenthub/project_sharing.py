"""Operator sharing policy for captured conversation passages.

A repository scope file lets a client REQUEST `sharing = "team"` for what it
captures. The request is never a grant: anyone who can edit a repository can edit
its scope file. The service decides:

* **At ingest** the effective sharing of a source is the minimum of the request,
  the project's current ceiling (`enterprise_projects.max_sharing`, or the
  tenant default `enterprise_organizations.default_max_sharing` when the project
  has none; `private` unless the operator sets otherwise) and the capture
  connection's visibility (a private connection never shares). It is stored per
  source (`enterprise_sources.sharing`) next to the request (`sharing_requested`).
* **At query time** a raw-representation passage, its cited evidence and its
  conversation context are readable by a principal other than the owner only when
  every source it depends on is stored as `team`, the source project's ceiling is
  still `team`, the reader is an active member of that project, and every existing
  source policy (tenant, lifecycle, delegation, connection readers and freshness,
  visibility, quarantine) passes. Lowering a ceiling hides passages immediately;
  raising it never exposes a source that was captured as private.

Retained originals are unaffected: they keep needing `source_read` and ownership or
an explicit owner grant (`agenthub.raw_access`). Operator methods take no request
context and audit as `local_operator`.
"""
import re
import time

SHARING = ('private', 'team')
OPERATOR = 'local_operator'


def effective_sharing(requested, ceiling, visibility):
    """Minimum of the request, the project ceiling and the connection visibility."""
    if requested == 'team' and ceiling == 'team' and visibility in ('team', 'organization'):
        return 'team'
    return 'private'


class ProjectSharingMixin:
    # Ingest ------------------------------------------------------------------

    @staticmethod
    def _sharing_ceiling(db, tenant, project):
        row = db.execute('''SELECT coalesce(p.max_sharing,o.default_max_sharing) ceiling
            FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
            WHERE p.tenant=? AND p.id=? AND p.active=1''', (tenant, project)).fetchone()
        return row['ceiling'] if row else 'private'

    def _ingest_sharing(self, db, ctx, connection, value):
        """(requested, effective) for one v2 event; a capped request is audited."""
        requested = value.get('sharing', 'private')
        if requested not in SHARING:
            raise ValueError('invalid_sharing_request')
        effective = effective_sharing(requested, self._sharing_ceiling(db, ctx['tenant'], value['project']),
                                      connection['visibility'])
        return requested, effective

    # Query-time oracle ---------------------------------------------------------

    def _team_shared(self, db, ctx, source):
        """Python mirror of search_permissions.TEAM_SHARE for one source row."""
        if source.get('sharing') != 'team' or source['visibility'] not in ('team', 'organization'):
            return False
        if not self._project_member(db, ctx, source['external_project']):
            return False
        return self._sharing_ceiling(db, ctx['tenant'], source['external_project']) == 'team'

    def _passage_readers(self, db, source):
        """Principals whose redacted-passage tier covers this source under current policy.

        A policy report for `explain`, not a credential check: the owner, plus active
        members of the source project when the source is team-shared and every
        ordinary source rule (visibility, quarantine, connection readers) admits them.
        """
        import json
        if not source or not source['active']:
            return []
        readers = {source['owner']}
        if (source.get('sharing') == 'team' and source['visibility'] in ('team', 'organization')
                and not source.get('principal_quarantine')
                and self._sharing_ceiling(db, source['tenant'], source['external_project']) == 'team'):
            members = {row['principal'] for row in db.execute('''SELECT m.principal FROM enterprise_memberships m
                JOIN enterprise_projects p ON p.tenant=m.tenant AND p.id=m.project
                JOIN enterprise_principals who ON who.tenant=m.tenant AND who.id=m.principal
                WHERE m.tenant=? AND m.project=? AND m.active=1 AND p.active=1 AND who.active=1''',
                (source['tenant'], source['external_project']))}
            link = db.execute('''SELECT c.owner,c.reader_ids FROM backend_source_revisions r
                JOIN backend_connections c ON c.id=r.connection WHERE r.source_id=?''', (source['id'],)).fetchone()
            listed = json.loads(link['reader_ids']) if link else []
            if listed:
                members &= set(listed) | {link['owner']}
            readers |= members
        return sorted(readers)

    # Operator policy -----------------------------------------------------------

    def _sharing_audit(self, db, operation, disposition, target):
        db.execute('''INSERT INTO enterprise_audit(tenant,actor,operation,disposition,target_id,created)
            VALUES(?,?,?,?,?,?)''', (self.tenant_id, OPERATOR, operation, disposition, target, time.time()))

    def set_project_sharing(self, project, max_sharing):
        """Set one project's ceiling ('private', 'team') or None to inherit the tenant default."""
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

    def sharing_policy_status(self):
        """Ceilings and per-project source counts by effective sharing; no content or IDs."""
        with self.open() as state:
            db = state.db
            default = db.execute('SELECT default_max_sharing FROM enterprise_organizations WHERE id=?',
                                 (self.tenant_id,)).fetchone()
            projects = db.execute('''SELECT p.id,p.max_sharing,p.active,
                coalesce(p.max_sharing,o.default_max_sharing) ceiling
                FROM enterprise_projects p JOIN enterprise_organizations o ON o.id=p.tenant
                WHERE p.tenant=? ORDER BY p.id''', (self.tenant_id,)).fetchall()
            counts = db.execute('''SELECT external_project,sharing_requested,sharing,count(*) n
                FROM enterprise_sources WHERE tenant=? AND active=1
                GROUP BY external_project,sharing_requested,sharing''', (self.tenant_id,)).fetchall()
        sources = {}
        for row in counts:
            entry = sources.setdefault(row['external_project'], {'team': 0, 'private': 0, 'team_requested_capped': 0})
            entry[row['sharing']] += row['n']
            if row['sharing_requested'] == 'team' and row['sharing'] == 'private':
                entry['team_requested_capped'] += row['n']
        return {'tenant': self.tenant_id, 'default_max_sharing': default['default_max_sharing'] if default else None,
                'projects': [{'project': row['id'], 'max_sharing': row['max_sharing'],
                              'effective_ceiling': row['ceiling'], 'active': bool(row['active']),
                              'active_sources': sources.get(row['id'], {'team': 0, 'private': 0,
                                                                         'team_requested_capped': 0})}
                             for row in projects]}
