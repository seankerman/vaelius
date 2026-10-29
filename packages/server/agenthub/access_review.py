"""Periodic user-access review, attestation, idle-access hygiene and offboarding.

Reports are content-free: identifiers, grants, timestamps and flags. Tokens,
credential digests, source text and claim text are never selected. Revocations
reuse the credential-family, membership and delegation paths, run under the
tenant delivery lock and are audited. Owned content is never deleted here; it
stays under the retention and legal-hold policy.

Operator command (private profile, no model or provider)::

    python -m agenthub.access_review --profile PROFILE --tenant TENANT report --format csv --output FILE
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

PRIVILEGED_ACTIONS = frozenset({'audit', 'curate', 'moderate', 'policy', 'settings', 'source_read'})
DEFAULT_STALE_DAYS = 90
ROUTE_PREFIX = '/enterprise/v3/access-review/'
LAST_USED_BASIS = ('latest credential issuance, refresh-token rotation or delivery receipt; precise to one '
                   'access-token lifetime (at most 24 hours for Vaelius-issued tokens; provider-issued tokens '
                   'are recorded at first use)')
CSV_COLUMNS = ('line_id', 'principal', 'principal_active', 'kind', 'target', 'actions',
               'last_used_at', 'flags')
_REVIEWER = re.compile(r'[A-Za-z0-9_.@:-]{1,128}')
_REVIEW_ID = re.compile(r'rev_[0-9a-f]{24}')
_LINE_ID = re.compile(r'ar_[0-9a-f]{24}')
_DAY = 86400


def _denied():
    from agenthub.enterprise import Denied
    return Denied()


def _days(value, name):
    if type(value) is not int or not 1 <= value <= 3650:
        raise ValueError('invalid_' + name)
    return value


def _text(value, name, limit):
    from agentclient.cleaning import SECRET
    if not isinstance(value, str) or len(value) > limit or SECRET.search(value):
        raise ValueError('invalid_' + name)
    return value


def _reviewer(value):
    if not isinstance(value, str) or not _REVIEWER.fullmatch(value):
        raise ValueError('invalid_reviewer')
    return value


def settings_from_runtime(settings=None):
    """Validated `access_review` block of runtime.json; hygiene is off by default."""
    value = (settings or {}).get('access_review', {})
    hygiene = value.get('hygiene', {}) if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) - {'stale_days', 'hygiene'}
            or not isinstance(hygiene, dict) or set(hygiene) - {
                'enabled', 'credential_idle_days', 'deactivate_bindings',
                'binding_idle_days', 'include_never_used_bindings'}
            or any(type(hygiene.get(key, False)) is not bool for key in
                   ('enabled', 'deactivate_bindings', 'include_never_used_bindings'))):
        raise ValueError('runtime_access_review_configuration')
    result = {'stale_days': _days(value.get('stale_days', DEFAULT_STALE_DAYS), 'stale_days'),
              'hygiene': {'enabled': hygiene.get('enabled', False),
                          'credential_idle_days': _days(hygiene.get('credential_idle_days', DEFAULT_STALE_DAYS),
                                                        'credential_idle_days'),
                          'deactivate_bindings': hygiene.get('deactivate_bindings', False),
                          'binding_idle_days': _days(hygiene.get('binding_idle_days', 180), 'binding_idle_days'),
                          'include_never_used_bindings': hygiene.get('include_never_used_bindings', False)}}
    return result


def _line_id(tenant, kind, *parts):
    raw = json.dumps([tenant, kind, *parts], separators=(',', ':'))
    return 'ar_' + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _latest(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _actions(values):
    result = set()
    for raw in values or ():
        result.update(json.loads(raw))
    return result


@contextmanager
def _snapshot(store):
    """One consistent read-only snapshot of the tenant authority."""
    with store.open() as state:
        db = state.db
        db.rollback()
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        try:
            yield db
        finally:
            db.rollback()


def _control_bindings(store):
    registry = getattr(store, 'registry', None)
    if registry is None or not hasattr(registry, 'open_control'):
        return None
    with registry.open_control() as db:
        modern = db.execute('''SELECT issuer,subject,principal,active,federation_required,allowed_providers
            FROM cloud_identity_bindings WHERE tenant=%s AND active=1''', (store.tenant_id,)).fetchall()
        legacy = db.execute('''SELECT issuer,subject,principal,active FROM cloud_external_bindings
            WHERE tenant=%s AND active=1''', (store.tenant_id,)).fetchall()
    return ([dict(row, kind='oidc') for row in modern] +
            [dict(row, kind='legacy_external', federation_required=0, allowed_providers='[]') for row in legacy])


def collect(store, *, stale_days=DEFAULT_STALE_DAYS, now=None):
    """Build the content-free access report with a fixed number of statements."""
    _days(stale_days, 'stale_days')
    now = time.time() if now is None else now
    cutoff = now - stale_days * _DAY
    tenant = store.tenant_id
    bindings = _control_bindings(store)
    with _snapshot(store) as db:
        principal_rows = db.execute('''SELECT id,active,settings_admin,contributions_default FROM enterprise_principals
            WHERE tenant=%s ORDER BY id''',
            (tenant,)).fetchall()
        projects = {row['id']: bool(row['active']) for row in db.execute(
            'SELECT id,active FROM enterprise_projects WHERE tenant=%s', (tenant,))}
        widest = db.execute('SELECT widest_entry FROM enterprise_organizations WHERE id=%s',
                            (tenant,)).fetchone()['widest_entry']
        credentials = db.execute('''SELECT principal,acting_for,enrollment,
                bool_or(oauth_provider IS NOT NULL) AS provider_issued,
                count(*) FILTER (WHERE active=1 AND expires_at>%s) AS active_access,
                max(expires_at) FILTER (WHERE active=1 AND expires_at>%s) AS expires_at,
                max(created) AS last_issued,
                array_agg(DISTINCT actions) FILTER (WHERE active=1 AND expires_at>%s) AS active_actions
            FROM enterprise_credentials WHERE tenant=%s GROUP BY principal,acting_for,enrollment''',
            (now, now, now, tenant)).fetchall()
        families = db.execute('''SELECT f.principal,f.acting_for,f.enrollment,
                count(DISTINCT f.id) FILTER (WHERE f.revoked_at IS NULL AND f.absolute_expires_at>%s
                    AND r.active=1 AND r.idle_expires_at>%s) AS active_sessions,
                max(r.used_at) AS last_refresh, max(f.created) AS last_login,
                array_agg(DISTINCT f.actions) FILTER (WHERE f.revoked_at IS NULL AND f.absolute_expires_at>%s
                    AND r.active=1 AND r.idle_expires_at>%s) AS active_actions
            FROM enterprise_credential_families f LEFT JOIN enterprise_refresh_tokens r ON r.family=f.id
            WHERE f.tenant=%s GROUP BY f.principal,f.acting_for,f.enrollment''',
            (now, now, now, now, tenant)).fetchall()
        receipts = db.execute('''SELECT principal,enrollment,max(created) AS last_receipt FROM enterprise_receipts
            WHERE tenant=%s GROUP BY principal,enrollment''', (tenant,)).fetchall()
        audited = {row['actor']: row['last_audit'] for row in db.execute(
            'SELECT actor,max(created) AS last_audit FROM enterprise_audit WHERE tenant=%s GROUP BY actor', (tenant,))}
        identity_use = db.execute('''SELECT identity_issuer AS issuer,identity_subject AS subject,max(created) AS used
                FROM enterprise_credentials WHERE tenant=%s AND identity_issuer IS NOT NULL GROUP BY 1,2
            UNION ALL
            SELECT f.identity_issuer,f.identity_subject,greatest(max(f.created),max(r.used_at))
                FROM enterprise_credential_families f LEFT JOIN enterprise_refresh_tokens r ON r.family=f.id
                WHERE f.tenant=%s AND f.identity_issuer IS NOT NULL GROUP BY 1,2''', (tenant, tenant)).fetchall()
        # Access lists (agenthub.workspace_access): entries, effective access and how.
        entries = db.execute('''SELECT workspace,subject_kind,subject,role,granted_by,created FROM workspace_entries
            WHERE tenant=%s ORDER BY workspace,subject_kind,subject''', (tenant,)).fetchall()
        access = db.execute('''SELECT a.principal,a.workspace,a.principal_role,a.group_role,
                coalesce((SELECT jsonb_agg(jsonb_build_object('group',g.id,'source',g.source,'sync_state',g.sync_state,
                    'role',e.role) ORDER BY g.id) FROM workspace_entries e JOIN groups g ON g.tenant=e.tenant AND g.id=e.subject
                    WHERE e.tenant=a.tenant AND e.workspace=a.workspace AND e.subject_kind='group'
                    AND EXISTS(SELECT 1 FROM group_closure c WHERE c.tenant=g.tenant AND c.group_id=g.id
                        AND c.principal=a.principal)),'[]'::jsonb) AS via_groups
            FROM workspace_access a WHERE a.tenant=%s ORDER BY a.principal,a.workspace''', (tenant,)).fetchall()
        edges = db.execute('''SELECT e.group_id,e.member,e.role,e.created,g.source,g.sync_state,g.active,g.workspace
            FROM group_edges e JOIN groups g ON g.tenant=e.tenant AND g.id=e.group_id
            WHERE e.tenant=%s AND e.member_kind='principal' ORDER BY e.group_id,e.member''', (tenant,)).fetchall()
        stale_groups = db.execute('''SELECT id,source,sync_state,last_synced FROM groups WHERE tenant=%s
            AND source!='local' AND sync_state!='current' ORDER BY id''', (tenant,)).fetchall()
        organization_entries = db.execute('''SELECT p.id,p.org_role FROM enterprise_projects p
            WHERE p.tenant=%s AND p.org_role>0 ORDER BY p.id''', (tenant,)).fetchall()
        repos = db.execute('''SELECT remote,workspace,linked_by,created FROM workspace_repos WHERE tenant=%s
            ORDER BY remote''', (tenant,)).fetchall()
        delegations = db.execute('''SELECT principal,acting_for,actions,projects FROM enterprise_delegations
            WHERE tenant=%s AND active=1''', (tenant,)).fetchall()
        directory_users = db.execute('''SELECT u.external_id,u.principal,r.active,r.requires_reconcile
            FROM cloud_directory_users u LEFT JOIN cloud_directory_resources r
            ON r.kind='user' AND r.external_id=u.external_id''').fetchall()
        groups = db.execute('''SELECT external_id,active,requires_reconcile,payload_json
            FROM cloud_directory_resources WHERE kind='group' ''').fetchall()
        group_projects = db.execute('''SELECT g.external_id,w.workspace AS project FROM groups g
            JOIN group_edges e ON e.tenant=g.tenant AND e.member_kind='group' AND e.member=g.id
            JOIN groups w ON w.tenant=e.tenant AND w.id=e.group_id AND w.workspace IS NOT NULL
            WHERE g.tenant=%s AND g.source='scim' ''', (tenant,)).fetchall()
        releases = db.execute('SELECT document_id,actor,readers FROM backend_releases WHERE tenant=%s',
            (tenant,)).fetchall()
        connections = db.execute('''SELECT id,owner,project,reader_ids,active FROM backend_connections
            WHERE tenant=%s''', (tenant,)).fetchall()

    people = {}

    def person(ident):
        if ident not in people:
            people[ident] = {'principal': ident, 'known': False, 'active': False, 'settings_admin': False,
                'contributions_default': None,
                'identity_bindings': [], 'directory': [], 'groups': [], 'credentials': [],
                'workspace_access': [], 'workspace_entries': [], 'group_memberships': [],
                'repo_links': [], 'document_grants': [], 'connection_reader_grants': [],
                'owned_connections': [], 'delegations': {'acts_for': [], 'represented_by': []},
                'last_activity_at': None, '_flags': set()}
        return people[ident]

    for row in principal_rows:
        record = person(row['id'])
        # The person's default for new items (shared with the workspace, or private).
        record.update(known=True, active=bool(row['active']), settings_admin=bool(row['settings_admin']),
                      contributions_default=row['contributions_default'])
    active = {ident for ident, record in people.items() if record['active']}
    lines = []

    def line(kind, principal, target, *, parts, actions=(), last_used=None, flags=()):
        record = person(principal)
        flags = set(flags)
        if not record['active']:
            flags.add('orphaned:unknown_principal' if not record['known'] else 'orphaned:inactive_principal')
        if set(actions) & PRIVILEGED_ACTIONS:
            flags.add('privileged')
        if 'source_read' in actions:
            flags.add('raw_source_read')
        if kind in ('credential', 'binding') and (last_used is None or last_used < cutoff):
            flags.add('stale')
        item = {'line_id': _line_id(tenant, kind, principal, *parts), 'principal': principal,
                'principal_active': record['active'], 'kind': kind, 'target': target,
                'actions': sorted(actions), 'last_used_at': last_used, 'flags': sorted(flags)}
        lines.append(item)
        record['_flags'].update(flag for flag in flags if flag in ('privileged', 'raw_source_read'))
        if any(flag.startswith('orphaned:') for flag in flags) and kind != 'principal':
            record['_flags'].add('inactive_with_access')
        return item

    # Credentials: one review line per enrollment that still has a live token or session.
    usage = {}
    for row in credentials:
        key = (row['principal'], row['acting_for'], row['enrollment'])
        usage[key] = dict(provider_issued=row['provider_issued'], active_access=row['active_access'],
            expires_at=row['expires_at'], last_issued=row['last_issued'], actions=_actions(row['active_actions']),
            active_sessions=0, last_refresh=None, last_login=None, last_receipt=None)
    for row in families:
        key = (row['principal'], row['acting_for'], row['enrollment'])
        item = usage.setdefault(key, dict(provider_issued=False, active_access=0, expires_at=None,
            last_issued=None, actions=set(), active_sessions=0, last_refresh=None, last_login=None, last_receipt=None))
        item.update(active_sessions=row['active_sessions'], last_refresh=row['last_refresh'], last_login=row['last_login'])
        item['actions'] |= _actions(row['active_actions'])
    receipt_use = {(row['principal'], row['enrollment']): row['last_receipt'] for row in receipts}
    delegation_rows = {(row['principal'], row['acting_for']): row for row in delegations}
    for (principal, acting_for, enrollment), item in sorted(usage.items(), key=lambda pair: tuple(x or '' for x in pair[0])):
        last_used = _latest(item['last_issued'], item['last_refresh'], item['last_login'],
                            receipt_use.get((principal, enrollment)))
        record = person(principal)
        record['last_activity_at'] = _latest(record['last_activity_at'], last_used)
        if not item['active_access'] and not item['active_sessions']:
            continue
        flags = set()
        actions = set(item['actions'])
        if acting_for:
            flags.add('delegated')
            grant = delegation_rows.get((principal, acting_for))
            if grant is None:
                flags.add('orphaned:delegation_inactive')
            else:
                actions &= set(json.loads(grant['actions']))
            if acting_for not in active:
                flags.add('orphaned:inactive_represented_principal')
        target = {'enrollment': enrollment, 'acting_for': acting_for, 'provider_issued': bool(item['provider_issued']),
                  'active_access_tokens': item['active_access'], 'active_refresh_sessions': item['active_sessions'],
                  'expires_at': item['expires_at']}
        entry = line('credential', principal, target, parts=(acting_for or '', enrollment), actions=actions,
                     last_used=last_used, flags=flags)
        record['credentials'].append(dict(target, line_id=entry['line_id'], actions=entry['actions'],
                                          last_used_at=last_used, flags=entry['flags']))
    for ident, last in audited.items():
        if ident in people:
            people[ident]['last_activity_at'] = _latest(people[ident]['last_activity_at'], last)

    # Control-plane issuer/subject bindings, with last observed use from credentials.
    used = {}
    for row in identity_use:
        key = (row['issuer'], row['subject'])
        used[key] = _latest(used.get(key), row['used'])
    for row in bindings or ():
        last_used = used.get((row['issuer'], row['subject']))
        flags = {'never_used'} if last_used is None else set()
        target = {'binding_kind': row['kind'], 'issuer': row['issuer'], 'subject': row['subject'],
                  'federation_required': bool(row['federation_required']),
                  'allowed_providers': json.loads(row['allowed_providers'])}
        entry = line('binding', row['principal'], target, parts=(row['kind'], row['issuer'], row['subject']),
                     last_used=last_used, flags=flags)
        person(row['principal'])['identity_bindings'].append(dict(target, line_id=entry['line_id'],
            last_used_at=last_used, flags=entry['flags']))

    # Directory (SCIM) state: the latest pushed user state and group membership.
    directory_ids = {}
    for row in directory_users:
        record = person(row['principal'])
        state = {'external_id': row['external_id'],
                 'directory_active': None if row['active'] is None else bool(row['active']),
                 'requires_reconcile': bool(row['requires_reconcile'])}
        record['directory'].append(state)
        directory_ids.setdefault(row['external_id'], []).append(row['principal'])
        if record['active'] and row['active'] is not None and not row['active']:
            record['_flags'].add('directory_deactivated')
        if row['requires_reconcile']:
            record['_flags'].add('directory_reconcile_pending')
    mapped_projects = {}
    for row in group_projects:
        mapped_projects.setdefault(row['external_id'], []).append(row['project'])
    for row in groups:
        members = json.loads(row['payload_json']).get('members', [])
        for external in members:
            for principal in directory_ids.get(external, ()):
                person(principal)['groups'].append({'external_id': row['external_id'],
                    'active': bool(row['active']), 'requires_reconcile': bool(row['requires_reconcile']),
                    'projects': sorted(mapped_projects.get(row['external_id'], []))})

    roles = {1: 'reader', 2: 'writer', 3: 'admin'}

    def effective(principal_role, group_role):
        if widest == 'none':
            return 3 if principal_role == 3 else 0
        return max(principal_role, group_role)

    # Effective access per workspace and how it is held: a direct entry, groups (with
    # source and sync state). The organization entry is listed once per workspace.
    for row in access:
        role = effective(row['principal_role'], row['group_role'])
        via_groups = row['via_groups'] if isinstance(row['via_groups'], list) else json.loads(row['via_groups'])
        if not role and not via_groups:
            continue
        via = (['entry'] if row['principal_role'] else []) + ['group:' + item['group'] for item in via_groups]
        flags = set()
        if role == 3:
            flags.add('admin_role')
            flags.add('privileged')
        if not role:
            flags.add('suspended_by_tenant_policy')
        if any(item['sync_state'] != 'current' for item in via_groups):
            flags.add('group_stale')
        if not projects.get(row['workspace'], False):
            flags.add('orphaned:inactive_project')
        target = {'workspace': row['workspace'], 'role': roles.get(role), 'via': via,
                  'groups': [{'group': item['group'], 'source': item['source'], 'sync_state': item['sync_state']}
                             for item in via_groups]}
        entry = line('workspace_access', row['principal'], target, parts=(row['workspace'],), flags=flags)
        person(row['principal'])['workspace_access'].append(dict(target, line_id=entry['line_id'],
                                                                 flags=entry['flags']))
    for row in entries:
        if row['subject_kind'] != 'principal':
            continue
        flags = {'admin_role', 'privileged'} if row['role'] == 'admin' else set()
        if not projects.get(row['workspace'], False):
            flags.add('orphaned:inactive_project')
        target = {'workspace': row['workspace'], 'subject_kind': 'principal', 'role': row['role'],
                  'granted_by': row['granted_by'], 'created': row['created']}
        entry = line('workspace_entry', row['subject'], target, parts=(row['workspace'],), flags=flags)
        person(row['subject'])['workspace_entries'].append(dict(target, line_id=entry['line_id'],
                                                                flags=entry['flags']))
    for row in edges:
        synced = row['source'] != 'local'
        flags = set()
        if synced and row['sync_state'] != 'current':
            flags.add('group_stale')
        if row['role'] == 'admin':
            flags.add('admin_role')
        target = {'group': row['group_id'], 'source': row['source'], 'sync_state': row['sync_state'],
                  'role': row['role'], 'workspace': row['workspace'], 'created': row['created']}
        entry = line('synced_group_membership' if synced else 'group_membership', row['member'], target,
                     parts=(row['group_id'],), flags=flags)
        person(row['member'])['group_memberships'].append(dict(target, line_id=entry['line_id'],
                                                               flags=entry['flags']))
    for row in repos:
        target = {'remote': row['remote'], 'workspace': row['workspace'], 'created': row['created']}
        entry = line('repo_link', row['linked_by'], target, parts=(row['remote'],))
        person(row['linked_by'])['repo_links'].append(dict(target, line_id=entry['line_id'], flags=entry['flags']))
    workspace_summary = {
        'organization_entries': [{'workspace': row['id'], 'role': roles.get(row['org_role']),
                                  'suspended': widest != 'organization', 'flags': ['org_wide_entry']}
                                 for row in organization_entries],
        'stale_groups': [dict(row) for row in stale_groups],
        'widest_entry': widest,
        'without_admin': sorted(name for name in projects if not any(
            e['workspace'] == name and e['role'] == 'admin' and e['subject_kind'] in ('principal', 'group')
            for e in entries)),
    }

    for row in delegations:
        scope = json.loads(row['projects'])
        flags = set()
        if row['acting_for'] not in active:
            flags.add('orphaned:inactive_represented_principal')
        if any(not projects.get(project, False) for project in scope):
            flags.add('orphaned:inactive_project')
        target = {'acting_for': row['acting_for'], 'projects': scope}
        entry = line('delegation', row['principal'], target, parts=(row['acting_for'],),
                     actions=json.loads(row['actions']), flags=flags)
        person(row['principal'])['delegations']['acts_for'].append(dict(target, line_id=entry['line_id'],
            actions=entry['actions'], flags=entry['flags']))
        person(row['acting_for'])['delegations']['represented_by'].append({'principal': row['principal'],
            'line_id': entry['line_id'], 'actions': entry['actions'], 'projects': scope})

    for row in releases:
        for reader in sorted(set(json.loads(row['readers']))):
            target = {'document_id': row['document_id'], 'granted_by': row['actor']}
            entry = line('document_grant', reader, target, parts=(row['document_id'],))
            person(reader)['document_grants'].append(dict(target, line_id=entry['line_id'], flags=entry['flags']))
    for row in connections:
        person(row['owner'])['owned_connections'].append({'connection': row['id'], 'project': row['project'],
                                                          'active': bool(row['active'])})
        if not row['active']:
            continue
        for reader in sorted(set(json.loads(row['reader_ids']))):
            target = {'connection': row['id'], 'owner': row['owner'], 'project': row['project']}
            entry = line('connection_reader', reader, target, parts=(row['id'],))
            person(reader)['connection_reader_grants'].append(dict(target, line_id=entry['line_id'],
                                                                   flags=entry['flags']))

    principals = []
    for ident in sorted(people):
        record = people[ident]
        if record['settings_admin']:
            line('settings_admin', ident, {'settings_admin': True}, parts=(), flags={'privileged'})
            record['_flags'].add('privileged')
        actions = set()
        for item in record['credentials']:
            actions.update(item['actions'])
        if not record['known']:
            record['_flags'].add('unknown_principal')
        if record['active'] and (record['last_activity_at'] is None or record['last_activity_at'] < cutoff):
            record['_flags'].add('stale')
        if record['active']:
            line('principal', ident, {'principal': ident}, parts=(),
                 last_used=record['last_activity_at'], flags=record['_flags'] & {
                     'stale', 'privileged', 'directory_deactivated', 'directory_reconcile_pending'})
        record['granted_actions'] = sorted(actions)
        record['privileged_actions'] = sorted(actions & PRIVILEGED_ACTIONS)
        record['raw_source_read'] = 'source_read' in actions
        record['active_access_tokens'] = sum(item['active_access_tokens'] for item in record['credentials'])
        record['active_refresh_sessions'] = sum(item['active_refresh_sessions'] for item in record['credentials'])
        record['flags'] = sorted(record.pop('_flags'))
        principals.append(record)

    lines.sort(key=lambda item: (item['principal'], item['kind'], item['line_id']))
    flag_counts = {}
    for item in lines:
        for flag in item['flags']:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    orphaned = [{'line_id': item['line_id'], 'principal': item['principal'], 'kind': item['kind'],
                 'reasons': [flag.split(':', 1)[1] for flag in item['flags'] if flag.startswith('orphaned:')]}
                for item in lines if any(flag.startswith('orphaned:') for flag in item['flags'])]
    return {'tenant': tenant, 'generated_at': now, 'stale_days': stale_days, 'stale_before': cutoff,
            'control_plane': 'unavailable' if bindings is None else 'read',
            'last_used_basis': LAST_USED_BASIS, 'privileged_actions': sorted(PRIVILEGED_ACTIONS),
            'summary': {'principals': sum(1 for record in principals if record['known']),
                        'active_principals': len(active), 'lines': len(lines),
                        'orphaned_grants': len(orphaned),
                        'stale_principals': sum('stale' in record['flags'] for record in principals),
                        'privileged_principals': sum('privileged' in record['flags'] for record in principals),
                        'directory_deactivated': sum('directory_deactivated' in record['flags'] for record in principals),
                        'flags': dict(sorted(flag_counts.items()))},
            'principals': principals, 'lines': lines, 'orphaned_grants': orphaned,
            'workspaces': workspace_summary}


def _iso(value):
    if value is None:
        return ''
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(value))


def _cell(value):
    # Spreadsheet formula injection: identifiers come from external directories.
    text = str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def _target_text(target):
    parts = []
    for key in sorted(target):
        value = target[key]
        if value is None or value == [] or key == 'expires_at':
            continue
        parts.append(key + '=' + (','.join(map(str, value)) if isinstance(value, list) else str(value)))
    return ';'.join(parts)


def to_csv(lines, extra=()):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(CSV_COLUMNS + tuple(extra))
    for item in lines:
        row = [item['line_id'], item['principal'], item['principal_active'], item['kind'],
               _target_text(item['target']), ' '.join(item['actions']), _iso(item['last_used_at']),
               ';'.join(item['flags'])] + [item.get(name, '') for name in extra]
        writer.writerow([_cell('' if value is None else value) for value in row])
    return output.getvalue()


# --- authorization -----------------------------------------------------------

def authorize(store, db, ctx, *, revoke=False):
    """Tenant access reviewer: audit/policy action AND the tenant administrator flag.

    `policy` alone is held by ordinary owners to narrow their own sources, so it
    never opens a tenant-wide report. Delegated (acting_for) credentials and
    inactive principals are refused. Revocation additionally requires `policy`.
    """
    store.current_identity(db, ctx)
    if ctx.get('acting_for') or not ctx['actions'] & ({'policy'} if revoke else {'audit', 'policy'}):
        raise _denied()
    row = db.execute('SELECT active,settings_admin FROM enterprise_principals WHERE tenant=%s AND id=%s',
        (ctx['tenant'], ctx['principal'])).fetchone()
    if not row or not row['active'] or not row['settings_admin']:
        raise _denied()
    return ctx['principal']


def _audit(store, db, actor, operation, disposition, target, request_id=None):
    store._audit(db, {'tenant': store.tenant_id, 'actor': actor, 'request_id': request_id},
                 operation, disposition, target)


# --- revocation through the existing grant and credential paths --------------

def _revoke_credentials(store, db, where, params, *, actor, reason, request_id=None):
    """Revoke refresh families and deactivate access tokens matching `where`."""
    families = db.execute('SELECT * FROM enterprise_credential_families WHERE tenant=%s AND ' + where +
                          ' ORDER BY id FOR UPDATE', (store.tenant_id, *params)).fetchall()
    revoked = 0
    for family in families:
        if store._revoke_family(db, family, reason):
            revoked += 1
            _audit(store, db, actor, 'credential_revocation', 'applied', family['id'], request_id)
    tokens = db.execute('UPDATE enterprise_credentials SET active=0 WHERE tenant=%s AND active=1 AND ' + where,
                        (store.tenant_id, *params)).rowcount
    return {'refresh_families_revoked': revoked, 'access_tokens_deactivated': tokens}


_ENROLLMENT = "principal=%s AND coalesce(acting_for,'')=%s AND enrollment=%s"


def enrollment_last_used(db, tenant, principal, acting_for, enrollment):
    """Latest issuance, refresh rotation or receipt for one enrollment (same basis as the report)."""
    params = (tenant, principal, acting_for or '', enrollment)
    return db.execute('''SELECT greatest(
        (SELECT max(created) FROM enterprise_credentials WHERE tenant=%s AND ''' + _ENROLLMENT + '''),
        (SELECT greatest(max(f.created),max(r.used_at)) FROM enterprise_credential_families f
            LEFT JOIN enterprise_refresh_tokens r ON r.family=f.id WHERE f.tenant=%s AND f.principal=%s
            AND coalesce(f.acting_for,'')=%s AND f.enrollment=%s),
        (SELECT max(created) FROM enterprise_receipts WHERE tenant=%s AND principal=%s AND enrollment=%s)) AS used''',
        (*params, *params, tenant, principal, enrollment)).fetchone()['used']


def revoke_enrollment(store, principal, acting_for, enrollment, *, actor, reason, idle_before=None,
                      request_id=None):
    with store.delivery_lock(), store.open() as state, state.db:
        db = state.db
        if idle_before is not None:
            last = enrollment_last_used(db, store.tenant_id, principal, acting_for, enrollment)
            if last is not None and last >= idle_before:
                return {'status': 'skipped_recent_use', 'last_used_at': last}
        result = _revoke_credentials(store, db, _ENROLLMENT, (principal, acting_for or '', enrollment),
                                     actor=actor, reason=reason, request_id=request_id)
        changed = any(result.values())
        _audit(store, db, actor, 'access_credential_revocation', 'applied' if changed else 'already_revoked',
               principal + ':' + enrollment, request_id)
    return dict(result, status='applied' if changed else 'noop')


def revoke_workspace_entry(store, principal, workspace, *, actor, request_id=None):
    """Remove a principal's direct entry from a workspace (the last-admin rule still holds)."""
    with store.delivery_lock(), store.open() as state, state.db:
        db = state.db
        ctx = {'tenant': store.tenant_id, 'actor': actor, 'principal': actor, 'request_id': request_id}
        try:
            result = store._delete_entry(db, ctx, workspace, 'principal', principal)
        except ValueError as error:
            if str(error) != 'last_admin':
                raise
            return {'status': 'refused_last_admin',
                    'action_required': 'add another admin to the workspace before removing this one'}
    return {'status': 'applied' if result['removed'] else 'noop'}


def revoke_group_membership(store, principal, group, *, actor, request_id=None):
    """Remove a principal from a local group; a synced group changes at its source."""
    with store.delivery_lock(), store.open() as state, state.db:
        db = state.db
        row = db.execute('SELECT * FROM groups WHERE tenant=%s AND id=%s', (store.tenant_id, group)).fetchone()
        if not row:
            return {'status': 'noop'}
        if row['source'] != 'local':
            _audit(store, db, actor, 'group_membership', 'change_at_source', group + ':' + principal, request_id)
            return {'status': 'change_at_source',
                    'action_required': 'remove the person from the group at its source (identity provider or GitHub), '
                                       'or remove the group\'s entry from the workspace'}
        ctx = {'tenant': store.tenant_id, 'actor': actor, 'principal': actor, 'request_id': request_id}
        try:
            removed = store._delete_member(db, ctx, dict(row), 'principal', principal)
        except ValueError as error:
            if str(error) != 'last_admin':
                raise
            return {'status': 'refused_last_admin',
                    'action_required': 'add another admin before removing this one'}
    return {'status': 'applied' if removed else 'noop'}


def revoke_delegation(store, principal, acting_for, *, actor, request_id=None):
    with store.open() as state:
        row = state.db.execute('''SELECT actions,projects,active FROM enterprise_delegations
            WHERE tenant=%s AND principal=%s AND acting_for=%s''', (store.tenant_id, principal, acting_for)).fetchone()
    if not row or not row['active']:
        return {'status': 'noop'}
    try:
        store.set_delegation(store.tenant_id, principal, acting_for, json.loads(row['actions']),
                             json.loads(row['projects']), False)
    except ValueError:
        # Rows predating current validation still deactivate with the same effect.
        with store.delivery_lock(), store.open() as state, state.db:
            state.db.execute('''UPDATE enterprise_delegations SET active=0,policy_version=policy_version+1
                WHERE tenant=%s AND principal=%s AND acting_for=%s''', (store.tenant_id, principal, acting_for))
    with store.open() as state, state.db:
        _audit(store, state.db, actor, 'delegation', 'revoked', principal + ':' + acting_for, request_id)
    return {'status': 'applied'}


def _remove_release_reader(store, db, principal, *, actor, document_id=None, request_id=None):
    clause, params = ('AND document_id=%s', (document_id,)) if document_id else ('', ())
    removed = 0
    for row in db.execute('SELECT id,document_id,readers FROM backend_releases WHERE tenant=%s ' + clause +
                          ' ORDER BY id FOR UPDATE', (store.tenant_id, *params)).fetchall():
        readers = json.loads(row['readers'])
        if principal not in readers:
            continue
        # Release readers are checked live at delivery; removal only narrows.
        db.execute('UPDATE backend_releases SET readers=%s WHERE id=%s',
                   (json.dumps(sorted(set(readers) - {principal})), row['id']))
        _audit(store, db, actor, 'release_reader', 'revoked', row['document_id'], request_id)
        removed += 1
    return removed


def revoke_document_grant(store, principal, document_id, *, actor, request_id=None):
    with store.delivery_lock(), store.open() as state, state.db:
        removed = _remove_release_reader(store, state.db, principal, actor=actor, document_id=document_id,
                                         request_id=request_id)
    return {'status': 'applied' if removed else 'noop', 'release_readers_removed': removed}


def revoke_settings_admin(store, principal, *, actor, request_id=None):
    with store.delivery_lock(), store.open() as state, state.db:
        changed = state.db.execute('''UPDATE enterprise_principals SET settings_admin=0
            WHERE tenant=%s AND id=%s AND settings_admin=1''', (store.tenant_id, principal)).rowcount
        if changed:
            _audit(store, state.db, actor, 'settings_admin', 'revoked', principal, request_id)
    return {'status': 'applied' if changed else 'noop'}


def revoke_binding(store, principal, binding_kind, issuer, subject, *, actor, control_admin=None,
                   reason='access_review', request_id=None):
    """Deactivate one issuer/subject binding; control-plane writes need the operator.

    Credentials already issued through the binding are revoked in either case.
    """
    changed = False
    if control_admin is None:
        pass
    elif binding_kind == 'oidc':
        from agenthub.cloud_identity import IdentityBindings
        with control_admin() as db:
            row = db.execute('''SELECT * FROM cloud_identity_bindings WHERE issuer=%s AND subject=%s AND tenant=%s''',
                             (issuer, subject, store.tenant_id)).fetchone()
        if row and row['active']:
            IdentityBindings(control_admin).bind(issuer, subject, store.tenant_id, row['principal'],
                federation_required=bool(row['federation_required']),
                allowed_providers=json.loads(row['allowed_providers']), active=False)
            changed = True
    else:
        with control_admin() as db, db.transaction():
            changed = bool(db.execute('''UPDATE cloud_external_bindings SET active=0
                WHERE issuer=%s AND subject=%s AND tenant=%s AND active=1''',
                (issuer, subject, store.tenant_id)).rowcount)
    with store.delivery_lock(), store.open() as state, state.db:
        result = _revoke_credentials(store, state.db, 'identity_issuer=%s AND identity_subject=%s', (issuer, subject),
                                     actor=actor, reason=reason, request_id=request_id)
        disposition = 'pending_operator' if control_admin is None else 'deactivated' if changed else 'already_inactive'
        _audit(store, state.db, actor, 'identity_binding', disposition,
               _line_id(store.tenant_id, 'binding', principal, binding_kind, issuer, subject), request_id)
    if control_admin is None:
        return dict(result, status='pending_operator',
                    action_required='run the operator access-review command with control-plane admin credentials')
    return dict(result, status='applied' if changed or any(result.values()) else 'noop')


def apply_revocation(store, item, *, actor, control_admin=None, reason='access_review', request_id=None):
    kind, principal, target = item['kind'], item['principal'], item['target']
    common = {'actor': actor, 'request_id': request_id}
    if kind == 'principal':
        return offboard(store, principal, reason=reason, control_admin=control_admin, **common)
    if kind == 'credential':
        result = revoke_enrollment(store, principal, target['acting_for'], target['enrollment'], reason=reason,
                                   **common)
        if target.get('provider_issued'):
            # The authorization server keeps minting tokens while the binding is
            # active; only principal or binding deactivation stops new ones here.
            result['action_required'] = 'revoke the session at the authorization server or revoke the binding'
        return result
    if kind == 'workspace_entry':
        return revoke_workspace_entry(store, principal, target['workspace'], **common)
    if kind in ('group_membership', 'synced_group_membership'):
        return revoke_group_membership(store, principal, target['group'], **common)
    if kind == 'workspace_access':
        return {'status': 'revoke_entry_or_membership',
                'action_required': 'revoke the workspace_entry or group_membership lines that give this access'}
    if kind == 'repo_link':
        return {'status': 'requires_workspace_admin',
                'action_required': 'a workspace admin unlinks the repository (vaelius-client workspace unlink)'}
    if kind == 'delegation':
        return revoke_delegation(store, principal, target['acting_for'], **common)
    if kind == 'document_grant':
        return revoke_document_grant(store, principal, target['document_id'], **common)
    if kind == 'settings_admin':
        return revoke_settings_admin(store, principal, **common)
    if kind == 'binding':
        return revoke_binding(store, principal, target['binding_kind'], target['issuer'], target['subject'],
                              control_admin=control_admin, reason=reason, **common)
    if kind == 'connection_reader':
        # Removing the last listed reader would widen the connection to every
        # project member, and narrowing re-derives its documents: owner action.
        return {'status': 'requires_owner',
                'action_required': 'the connection owner narrows reader_ids through the connection policy'}
    raise ValueError('unknown_review_line_kind')


# --- reviews and attestations -------------------------------------------------

def open_review(store, *, opened_by, stale_days=DEFAULT_STALE_DAYS, ctx=None):
    if ctx is not None:
        with store.open() as state:
            opened_by = authorize(store, state.db, ctx)
    _reviewer(opened_by)
    report = collect(store, stale_days=stale_days)
    review_id = 'rev_' + secrets.token_hex(12)
    digest = hashlib.sha256(json.dumps(report['lines'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with store.open() as state, state.db:
        db = state.db
        if ctx is not None:
            authorize(store, db, ctx)
        db.execute('''INSERT INTO access_reviews(id,tenant,opened_by,opened,stale_days,report_sha256,line_count)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''', (review_id, store.tenant_id, opened_by, report['generated_at'],
            stale_days, digest, len(report['lines'])))
        db.executemany('''INSERT INTO access_review_lines(review_id,line_id,principal,kind,target_json,flags_json)
            VALUES(%s,%s,%s,%s,%s,%s)''', [(review_id, item['line_id'], item['principal'], item['kind'],
            json.dumps({key: item[key] for key in ('target', 'actions', 'last_used_at', 'principal_active')},
                       sort_keys=True), json.dumps(item['flags'])) for item in report['lines']])
        _audit(store, db, opened_by, 'access_review', 'opened', review_id, ctx and ctx.get('request_id'))
    return {'review_id': review_id, 'opened': report['generated_at'], 'opened_by': opened_by,
            'stale_days': stale_days, 'line_count': len(report['lines']), 'report_sha256': digest,
            'summary': report['summary']}


def _line(db, review_id, line_id):
    row = db.execute('SELECT * FROM access_review_lines WHERE review_id=%s AND line_id=%s',
                     (review_id, line_id)).fetchone()
    if not row:
        raise _denied()
    value = json.loads(row['target_json'])
    return dict(value, line_id=row['line_id'], principal=row['principal'], kind=row['kind'],
                flags=json.loads(row['flags_json']))


def decide(store, review_id, line_id, decision, *, reviewer=None, comment='', ctx=None, control_admin=None):
    """Record one final attestation; a revoke is applied immediately and audited.

    Identical retries are idempotent and re-apply an unfinished revocation. A
    different decision on a decided line is a conflict. Reviewers never attest
    their own access.
    """
    from agenthub.enterprise import Conflict
    if (not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id) or not isinstance(line_id, str)
            or not _LINE_ID.fullmatch(line_id) or decision not in ('approve', 'revoke')):
        raise ValueError('invalid_access_review_decision')
    _text(comment, 'comment', 1000)
    request_id = ctx.get('request_id') if ctx else None
    with store.delivery_lock():
        with store.open() as state, state.db:
            db = state.db
            if ctx is not None:
                reviewer = authorize(store, db, ctx, revoke=decision == 'revoke')
            _reviewer(reviewer)
            review = db.execute('SELECT * FROM access_reviews WHERE id=%s AND tenant=%s FOR UPDATE',
                                (review_id, store.tenant_id)).fetchone()
            if not review:
                raise _denied()
            item = _line(db, review_id, line_id)
            if item['principal'] == reviewer:
                raise _denied()
            prior = db.execute('SELECT * FROM access_review_decisions WHERE review_id=%s AND line_id=%s',
                               (review_id, line_id)).fetchone()
            if prior:
                # The recorded attestation never changes. Repeating the same
                # decision only retries an unfinished revocation (for example a
                # binding that waited for control-plane operator credentials).
                if prior['decision'] != decision:
                    raise Conflict()
                if decision == 'approve' or prior['applied'] is not None:
                    return _decision_result(prior, json.loads(prior['outcome_json']), duplicate=True)
                decided, comment = prior['decided'], prior['comment']
            else:
                if review['closed'] is not None:
                    raise Conflict()
                decided = time.time()
                db.execute('''INSERT INTO access_review_decisions(review_id,line_id,reviewer,decision,comment,decided)
                    VALUES(%s,%s,%s,%s,%s,%s)''', (review_id, line_id, reviewer, decision, comment, decided))
                _audit(store, db, reviewer, 'access_review_decision', decision, line_id, request_id)
        outcome = {}
        if decision == 'revoke':
            try:
                outcome = apply_revocation(store, item, actor=reviewer, control_admin=control_admin,
                                           reason='access_review:' + review_id, request_id=request_id)
            except Exception as error:
                outcome = {'status': 'failed', 'error': type(error).__name__}
            finished = outcome['status'] not in ('failed', 'pending_operator')
            with store.open() as state, state.db:
                state.db.execute('''UPDATE access_review_decisions SET outcome_json=%s,applied=%s
                    WHERE review_id=%s AND line_id=%s''', (json.dumps(outcome, sort_keys=True),
                    time.time() if finished else None, review_id, line_id))
                _audit(store, state.db, reviewer, 'access_review_revoke', outcome['status'], line_id, request_id)
    return {'review_id': review_id, 'line_id': line_id, 'principal': item['principal'], 'kind': item['kind'],
            'decision': decision, 'reviewer': prior['reviewer'] if prior else reviewer, 'decided': decided,
            'comment': comment, 'outcome': outcome, 'duplicate': bool(prior)}


def _decision_result(row, outcome, *, duplicate):
    return {'review_id': row['review_id'], 'line_id': row['line_id'], 'decision': row['decision'],
            'reviewer': row['reviewer'], 'decided': row['decided'], 'comment': row['comment'],
            'outcome': outcome, 'duplicate': duplicate}


def close_review(store, review_id, *, closed_by=None, ctx=None):
    if not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id):
        raise ValueError('invalid_access_review')
    with store.open() as state, state.db:
        db = state.db
        if ctx is not None:
            closed_by = authorize(store, db, ctx)
        _reviewer(closed_by)
        changed = db.execute('''UPDATE access_reviews SET closed=%s,closed_by=%s
            WHERE id=%s AND tenant=%s AND closed IS NULL''', (time.time(), closed_by, review_id,
            store.tenant_id)).rowcount
        if not changed and not db.execute('SELECT 1 FROM access_reviews WHERE id=%s AND tenant=%s',
                                          (review_id, store.tenant_id)).fetchone():
            raise _denied()
        _audit(store, db, closed_by, 'access_review', 'closed' if changed else 'already_closed', review_id,
               ctx and ctx.get('request_id'))
    return evidence(store, review_id)


def evidence(store, review_id, *, ctx=None):
    """The attestation record for one review: every line, decision and outcome."""
    if not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id):
        raise ValueError('invalid_access_review')
    with store.open() as state:
        db = state.db
        if ctx is not None:
            authorize(store, db, ctx)
        review = db.execute('SELECT * FROM access_reviews WHERE id=%s AND tenant=%s',
                            (review_id, store.tenant_id)).fetchone()
        if not review:
            raise _denied()
        rows = db.execute('''SELECT l.*,d.reviewer,d.decision,d.comment,d.decided,d.outcome_json,d.applied
            FROM access_review_lines l LEFT JOIN access_review_decisions d
            ON d.review_id=l.review_id AND d.line_id=l.line_id WHERE l.review_id=%s
            ORDER BY l.principal,l.kind,l.line_id''', (review_id,)).fetchall()
    lines = []
    for row in rows:
        value = json.loads(row['target_json'])
        lines.append(dict(value, line_id=row['line_id'], principal=row['principal'], kind=row['kind'],
            flags=json.loads(row['flags_json']), decision=row['decision'] or 'pending', reviewer=row['reviewer'],
            decided=row['decided'], comment=row['comment'],
            outcome=json.loads(row['outcome_json']) if row['outcome_json'] else None, applied=row['applied']))
    counts = {name: sum(item['decision'] == name for item in lines) for name in ('approve', 'revoke', 'pending')}
    return {'review_id': review_id, 'tenant': review['tenant'], 'opened': review['opened'],
            'opened_by': review['opened_by'], 'stale_days': review['stale_days'],
            'report_sha256': review['report_sha256'], 'closed': review['closed'], 'closed_by': review['closed_by'],
            'summary': dict(counts, lines=len(lines),
                            revocations_unfinished=sum(item['decision'] == 'revoke' and item['applied'] is None
                                                       for item in lines)),
            'lines': lines}


def evidence_csv(value):
    rows = [dict(item, decided=_iso(item['decided']), applied=_iso(item['applied']),
                 outcome=(item['outcome'] or {}).get('status', '')) for item in value['lines']]
    return to_csv(rows, extra=('decision', 'reviewer', 'decided', 'comment', 'outcome', 'applied'))


# --- hygiene and offboarding --------------------------------------------------

def hygiene(store, *, credential_idle_days, deactivate_bindings=False, binding_idle_days=180,
            include_never_used_bindings=False, apply=False, actor='access_hygiene', control_admin=None, now=None):
    """Expire credentials (and optionally bindings) with no observed use; dry-run by default."""
    _days(credential_idle_days, 'credential_idle_days')
    _days(binding_idle_days, 'binding_idle_days')
    now = time.time() if now is None else now
    report = collect(store, stale_days=credential_idle_days, now=now)
    credential_before = now - credential_idle_days * _DAY
    binding_before = now - binding_idle_days * _DAY
    credentials = [item for item in report['lines'] if item['kind'] == 'credential'
                   and (item['last_used_at'] is None or item['last_used_at'] < credential_before)]
    bindings = [item for item in report['lines'] if deactivate_bindings and item['kind'] == 'binding'
                and (item['last_used_at'] < binding_before if item['last_used_at'] is not None
                     else include_never_used_bindings)]
    if apply and bindings and control_admin is None:
        raise ValueError('control_plane_admin_required_for_binding_hygiene')
    result = {'tenant': store.tenant_id, 'dry_run': not apply, 'credential_idle_days': credential_idle_days,
              'binding_idle_days': binding_idle_days if deactivate_bindings else None,
              'credentials': [], 'bindings': []}
    for item in credentials:
        entry = {'line_id': item['line_id'], 'principal': item['principal'],
                 'enrollment': item['target']['enrollment'], 'last_used_at': item['last_used_at']}
        if apply:
            entry['outcome'] = revoke_enrollment(store, item['principal'], item['target']['acting_for'],
                item['target']['enrollment'], actor=actor, reason='access_hygiene_idle',
                idle_before=credential_before)
        result['credentials'].append(entry)
    for item in bindings:
        target = item['target']
        entry = {'line_id': item['line_id'], 'principal': item['principal'], 'issuer': target['issuer'],
                 'subject': target['subject'], 'last_used_at': item['last_used_at']}
        if apply:
            entry['outcome'] = revoke_binding(store, item['principal'], target['binding_kind'], target['issuer'],
                target['subject'], actor=actor, control_admin=control_admin, reason='access_hygiene_idle')
        result['bindings'].append(entry)
    if apply:
        with store.open() as state, state.db:
            _audit(store, state.db, actor, 'access_hygiene', 'applied',
                   'credentials:%d;bindings:%d' % (len(credentials), len(bindings)))
    return result


def offboard(store, principal, *, actor, reason, apply=True, control_admin=None, request_id=None):
    """Deactivate a principal and remove every access path; keep owned content.

    Idempotent. The first transaction denies the principal and every credential
    family (including credentials acting for them) before grants are cleaned up,
    so access ends even if a later step must be retried.
    """
    _reviewer(actor)
    _text(reason, 'reason', 500)
    if not isinstance(principal, str) or not 1 <= len(principal) <= 128:
        raise ValueError('invalid_principal')
    tenant = store.tenant_id
    with store.open() as state:
        db = state.db
        row = db.execute('SELECT active,settings_admin FROM enterprise_principals WHERE tenant=%s AND id=%s',
                         (tenant, principal)).fetchone()
        if not row:
            raise ValueError('unknown_principal')
        entry_rows = db.execute('''SELECT workspace,role FROM workspace_entries
            WHERE tenant=%s AND subject_kind='principal' AND subject=%s ORDER BY workspace''', (tenant, principal)).fetchall()
        projects = [r['workspace'] for r in entry_rows]
        admin_of = {r['workspace'] for r in entry_rows if r['role'] == 'admin'}
        local_groups = [r['group_id'] for r in db.execute('''SELECT e.group_id FROM group_edges e JOIN groups g
            ON g.tenant=e.tenant AND g.id=e.group_id WHERE e.tenant=%s AND e.member_kind='principal'
            AND e.member=%s AND g.source='local' ORDER BY e.group_id''', (tenant, principal))]
        delegations = [dict(r) for r in db.execute('''SELECT principal,acting_for FROM enterprise_delegations
            WHERE tenant=%s AND active=1 AND (principal=%s OR acting_for=%s) ORDER BY principal,acting_for''',
            (tenant, principal, principal))]
        sessions = db.execute('''SELECT count(*) FROM enterprise_credential_families WHERE tenant=%s
            AND revoked_at IS NULL AND (principal=%s OR acting_for=%s)''', (tenant, principal, principal)).fetchone()[0]
        tokens = db.execute('''SELECT count(*) FROM enterprise_credentials WHERE tenant=%s AND active=1
            AND (principal=%s OR acting_for=%s)''', (tenant, principal, principal)).fetchone()[0]
        releases = db.execute('''SELECT count(*) FROM backend_releases WHERE tenant=%s
            AND readers::jsonb ? %s''', (tenant, principal)).fetchone()[0]
    bindings = [item for item in (_control_bindings(store) or ()) if item['principal'] == principal]
    plan = {'principal_active': bool(row['active']), 'settings_admin': bool(row['settings_admin']),
            'refresh_families': sessions, 'access_tokens': tokens, 'workspace_entries': projects,
            'local_group_memberships': local_groups,
            'delegations': delegations, 'release_reader_grants': releases,
            'identity_bindings': [{'binding_kind': b['kind'], 'issuer': b['issuer'], 'subject': b['subject']}
                                  for b in bindings]}
    result = {'tenant': tenant, 'principal': principal, 'dry_run': not apply, 'plan': plan}
    if not apply:
        return dict(result, remaining=_remaining(store, principal, control_admin is None))
    changes = {}
    with store.delivery_lock():
        with store.open() as state, state.db:
            db = state.db
            changes['principal_deactivated'] = bool(db.execute('''UPDATE enterprise_principals
                SET active=0,settings_admin=0 WHERE tenant=%s AND id=%s AND (active=1 OR settings_admin=1)''',
                (tenant, principal)).rowcount)
            changes.update(_revoke_credentials(store, db, '(principal=%s OR acting_for=%s)', (principal, principal),
                                               actor=actor, reason='offboarding', request_id=request_id))
            revoked = 0
            for item in delegations:
                revoked += db.execute('''UPDATE enterprise_delegations SET active=0,policy_version=policy_version+1
                    WHERE tenant=%s AND principal=%s AND acting_for=%s AND active=1''',
                    (tenant, item['principal'], item['acting_for'])).rowcount
                _audit(store, db, actor, 'delegation', 'revoked', item['principal'] + ':' + item['acting_for'],
                       request_id)
            changes['delegations_revoked'] = revoked
            # Direct entries and local group memberships go; deactivation already ends
            # every access path, and synced memberships disappear at the next sync.
            # Workspaces where they were the last principal admin are reported in
            # `remaining`: a tenant administrator assigns one.
            entries = db.execute('''DELETE FROM workspace_entries WHERE tenant=%s AND subject_kind='principal'
                AND subject=%s''', (tenant, principal)).rowcount
            edges = db.execute('''DELETE FROM group_edges e USING groups g WHERE g.tenant=e.tenant AND g.id=e.group_id
                AND e.tenant=%s AND e.member_kind='principal' AND e.member=%s AND g.source='local' ''',
                (tenant, principal)).rowcount
            if local_groups:
                store._refresh_groups(db, tenant, local_groups)
            if projects:
                store._refresh_workspaces(db, tenant, projects)
            # Directory groups are recomputed from active principals only.
            store._reconcile_directory_memberships(db, tenant)
            for project in projects:
                _audit(store, db, actor, 'workspace_entry_remove',
                       'offboarding_admin' if project in admin_of else 'offboarding', project + ':' + principal,
                       request_id)
            for group in local_groups:
                _audit(store, db, actor, 'group_membership', 'offboarding', group + ':' + principal, request_id)
            changes['workspace_entries_removed'] = entries
            changes['group_memberships_removed'] = edges
            changes['release_reader_grants_removed'] = _remove_release_reader(store, db, principal, actor=actor,
                                                                              request_id=request_id)
            done = changes['principal_deactivated'] or any(
                changes[key] for key in ('refresh_families_revoked', 'access_tokens_deactivated',
                                         'delegations_revoked', 'workspace_entries_removed',
                                         'group_memberships_removed', 'release_reader_grants_removed'))
            _audit(store, db, actor, 'offboarding', 'applied' if done else 'already_offboarded', principal,
                   request_id)
        binding_results = [revoke_binding(store, principal, item['kind'], item['issuer'], item['subject'],
                                          actor=actor, control_admin=control_admin, reason='offboarding',
                                          request_id=request_id) for item in bindings]
    changes['identity_bindings_deactivated'] = sum(item['status'] == 'applied' for item in binding_results)
    result.update(changes=changes, status='applied' if done or changes['identity_bindings_deactivated']
                  else 'already_offboarded', remaining=_remaining(store, principal, control_admin is None))
    return result


def _remaining(store, principal, bindings_pending):
    """What intentionally or necessarily remains after offboarding (content-free)."""
    tenant = store.tenant_id
    with store.open() as state:
        db = state.db
        sources = db.execute('''SELECT count(*) FILTER (WHERE active=1) AS active,count(*) AS total
            FROM enterprise_sources WHERE tenant=%s AND owner=%s''', (tenant, principal)).fetchone()
        owned = [dict(row) for row in db.execute('''SELECT id AS connection,project,active FROM backend_connections
            WHERE tenant=%s AND owner=%s ORDER BY id''', (tenant, principal))]
        readers = [row['id'] for row in db.execute('''SELECT id FROM backend_connections WHERE tenant=%s
            AND active=1 AND reader_ids::jsonb ? %s ORDER BY id''', (tenant, principal))]
        directory = [dict(row) for row in db.execute('''SELECT u.external_id,r.active AS directory_active
            FROM cloud_directory_users u LEFT JOIN cloud_directory_resources r
            ON r.kind='user' AND r.external_id=u.external_id WHERE u.principal=%s ORDER BY u.external_id''',
            (principal,))]
        # Workspaces this principal administered (directly, or through a local
        # group) that no longer have a counting admin.
        last_admin = [row['workspace'] for row in db.execute('''SELECT DISTINCT a.target_id AS workspace
            FROM (SELECT split_part(target_id,':',1) AS target_id FROM enterprise_audit WHERE tenant=%s
                AND operation='workspace_entry_remove' AND disposition='offboarding_admin'
                AND split_part(target_id,':',2)=%s) a ORDER BY 1''', (tenant, principal))
            if not store._counting_admins(db, tenant, row['workspace'])]
        live = db.execute('''SELECT count(*) FROM enterprise_credentials WHERE tenant=%s AND active=1
            AND expires_at>%s AND (principal=%s OR acting_for=%s)''', (tenant, time.time(), principal,
            principal)).fetchone()[0]
    bindings = [{'binding_kind': item['kind'], 'issuer': item['issuer'], 'subject': item['subject']}
                for item in (_control_bindings(store) or ()) if item['principal'] == principal]
    result = {'owned_sources': {'active': sources['active'], 'total': sources['total'],
                                'disposition': 'retained under the retention and legal-hold policy; not deleted'},
              'owned_connections': owned, 'connection_reader_grants': readers,
              'directory_users': [dict(item, directory_active=None if item['directory_active'] is None
                                       else bool(item['directory_active'])) for item in directory],
              'live_access_tokens': live, 'identity_bindings': bindings,
              'workspaces_without_admin': last_admin, 'actions_required': []}
    if last_admin:
        result['actions_required'].append('a tenant administrator assigns an admin to workspaces where this '
                                          'principal was the last one')
    if bindings:
        result['actions_required'].append('deactivate identity bindings with control-plane admin credentials'
                                          if bindings_pending else 'identity bindings remain active')
    if readers:
        result['actions_required'].append('connection owners narrow reader lists that name this principal')
    if any(item['directory_active'] for item in directory):
        result['actions_required'].append('deactivate the user in the identity provider; a later SCIM update '
                                          'marking the user active would reactivate this principal')
    if any(item['active'] for item in owned):
        result['actions_required'].append('reassign or disable owned connections; processing for them stops '
                                          'because the owner has no active credential')
    return result


# --- authenticated admin route -------------------------------------------------

_ROUTE_FIELDS = {'report': {'stale_days', 'format'}, 'reviews': {'stale_days'},
                 'decisions': {'review_id', 'line_id', 'decision', 'comment'},
                 'evidence': {'review_id', 'format'}, 'close': {'review_id'}}


def route(store, ctx, method, path, data):
    """POST /enterprise/v3/access-review/{report,reviews,decisions,evidence,close}."""
    action = path.removeprefix(ROUTE_PREFIX)
    if method != 'POST' or action not in _ROUTE_FIELDS:
        raise FileNotFoundError()
    data = data or {}
    if set(data) - _ROUTE_FIELDS[action] or data.get('format', 'json') not in ('json', 'csv'):
        raise ValueError('invalid_access_review_request')
    stale_days = data.get('stale_days', DEFAULT_STALE_DAYS)
    if action == 'report':
        with store.open() as state:
            authorize(store, state.db, ctx)
        report = collect(store, stale_days=_days(stale_days, 'stale_days'))
        with store.open() as state, state.db:
            _audit(store, state.db, ctx['principal'], 'access_review_report', 'generated', None, ctx.get('request_id'))
        if data.get('format') == 'csv':
            return {'format': 'csv', 'content': to_csv(report['lines']), 'summary': report['summary']}
        return report
    if action == 'reviews':
        return open_review(store, opened_by=ctx['principal'], stale_days=_days(stale_days, 'stale_days'), ctx=ctx)
    if action == 'decisions':
        return decide(store, data.get('review_id'), data.get('line_id'), data.get('decision'),
                      comment=data.get('comment', ''), ctx=ctx)
    if action == 'close':
        value = close_review(store, data.get('review_id'), ctx=ctx)
        return {key: value[key] for key in value if key != 'lines'}
    value = evidence(store, data.get('review_id'), ctx=ctx)
    if data.get('format') == 'csv':
        return {'format': 'csv', 'content': evidence_csv(value), 'summary': value['summary']}
    return value


# --- operator command -------------------------------------------------------------

def _write_private(path, text):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(text)
    return str(path)


def _control_admin(profile):
    """Control-plane admin connection from the private operator.json, if present."""
    path = Path(profile) / 'operator.json'
    if not path.exists():
        return None
    from agenthub.cloud_maintenance import private_input
    from agenthub.postgres import TenantRegistry
    operator = private_input(path)
    if not operator.get('control_admin_dsn'):
        return None
    return TenantRegistry(operator['control_admin_dsn'], Path(profile) / 'operator-state').open_control


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=['report', 'review-open', 'review-decide', 'review-close',
                                            'review-evidence', 'hygiene', 'offboard'])
    parser.add_argument('--profile', required=True)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--stale-days', type=int)
    parser.add_argument('--format', choices=['json', 'csv'], default='json')
    parser.add_argument('--output', help='new private file for the report or evidence')
    parser.add_argument('--reviewer', help='operator identity recorded as reviewer or actor')
    parser.add_argument('--review'); parser.add_argument('--line')
    parser.add_argument('--decision', choices=['approve', 'revoke']); parser.add_argument('--comment', default='')
    parser.add_argument('--principal'); parser.add_argument('--reason')
    parser.add_argument('--apply', action='store_true', help='hygiene: apply instead of the default dry run')
    parser.add_argument('--dry-run', action='store_true', help='offboard: report the plan without changes')
    parser.add_argument('--credential-idle-days', type=int); parser.add_argument('--binding-idle-days', type=int)
    args = parser.parse_args(argv)
    os.umask(0o077)
    from agenthub.cloud_local import runtime, receipt
    profile = Path(args.profile).expanduser().resolve()
    settings, registry = runtime(profile)
    config = settings_from_runtime(settings)
    store = registry.resolve(args.tenant)
    control_admin = _control_admin(profile)
    stale_days = args.stale_days if args.stale_days is not None else config['stale_days']
    if args.command in ('review-open', 'review-decide', 'review-close', 'offboard') and not args.reviewer:
        parser.error('--reviewer required')
    if args.command == 'report':
        report = collect(store, stale_days=stale_days)
        text = to_csv(report['lines']) if args.format == 'csv' else json.dumps(report, sort_keys=True, indent=2) + '\n'
        if args.output:
            value = {'output': _write_private(args.output, text), 'format': args.format, 'summary': report['summary']}
        elif args.format == 'csv':
            sys.stdout.write(text)
            return
        else:
            value = report
        print(json.dumps(value, sort_keys=True))
        return
    if args.command == 'review-evidence':
        if not args.review:
            parser.error('--review required')
        value = evidence(store, args.review)
        text = evidence_csv(value) if args.format == 'csv' else json.dumps(value, sort_keys=True, indent=2) + '\n'
        if not args.output:
            parser.error('--output new private file required')
        print(json.dumps({'output': _write_private(args.output, text), 'summary': value['summary']}, sort_keys=True))
        return
    if args.command == 'review-open':
        value = open_review(store, opened_by=args.reviewer, stale_days=stale_days)
    elif args.command == 'review-decide':
        if not (args.review and args.line and args.decision):
            parser.error('--review, --line and --decision required')
        value = decide(store, args.review, args.line, args.decision, reviewer=args.reviewer,
                       comment=args.comment, control_admin=control_admin)
    elif args.command == 'review-close':
        if not args.review:
            parser.error('--review required')
        value = close_review(store, args.review, closed_by=args.reviewer)
        value = {key: value[key] for key in value if key != 'lines'}
    elif args.command == 'hygiene':
        options = dict(config['hygiene'])
        if args.credential_idle_days is not None:
            options['credential_idle_days'] = args.credential_idle_days
        if args.binding_idle_days is not None:
            options['binding_idle_days'] = args.binding_idle_days
        if args.apply and not options['enabled']:
            parser.error('access_review.hygiene.enabled is false in runtime.json; dry run only')
        value = hygiene(store, credential_idle_days=options['credential_idle_days'],
                        deactivate_bindings=options['deactivate_bindings'],
                        binding_idle_days=options['binding_idle_days'],
                        include_never_used_bindings=options['include_never_used_bindings'],
                        apply=args.apply, actor=args.reviewer or 'access_hygiene', control_admin=control_admin)
    else:
        if not args.principal or not args.reason:
            parser.error('--principal and --reason required')
        value = offboard(store, args.principal, actor=args.reviewer, reason=args.reason,
                         apply=not args.dry_run, control_admin=control_admin)
    print(json.dumps(receipt(profile, 'access-review-' + args.command, value), sort_keys=True))


if __name__ == '__main__':
    main()
