"""GitHub App team sync and the device-flow identity link (docs/ACCESS_MODEL.md section 3, PR C).

GitHub teams become read-only `github` groups. The service talks to GitHub only
with short-lived installation tokens scoped to organization *Members: read*, and,
for the identity link, runs the OAuth device flow for the App's client id: the
person approves in the browser, the service reads `GET /user` once with the user
token and discards it. Only the numeric user id and current login are stored.

A team's members count only through a verified link: the snapshot of GitHub
members is joined to active `identity_links` of active principals whenever the
group's edges are rebuilt. Webhooks are hints: every delivery is verified
(HMAC-SHA256, constant-time compare), de-duplicated by delivery id and answered
by re-reading current state from GitHub, so duplicates and out-of-order
deliveries converge. A periodic reconcile fixes drift and missed deliveries, and
a group whose last successful sync is older than `max_staleness_seconds`
contributes no members until the next good sync (fail safe).
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import quote, urlsplit

from agenthub.secret_files import MountedSecret

SETTINGS = {'app_id', 'client_id', 'private_key_file', 'webhook_secret_file', 'installations',
            'reconcile_seconds', 'max_staleness_seconds', 'api_url', 'web_url'}
DEFAULT_RECONCILE_SECONDS = 6 * 3600
DEFAULT_MAX_STALENESS_SECONDS = 24 * 3600
# The installation token is narrowed to exactly this when it is minted.
TOKEN_PERMISSIONS = {'members': 'read'}
LOGIN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})')
SLUG = re.compile(r'[a-z0-9][a-z0-9_.-]{0,99}')
DELIVERY = re.compile(r'[A-Za-z0-9-]{1,64}')
FLOW = re.compile(r'[0-9a-f]{32}')
SYNC_ACTOR = 'github_sync'
DELIVERY_RETENTION_SECONDS = 30 * 86400
MAX_PAGES = 100
MAX_PENDING_FLOWS = 3
WEBHOOK_EVENTS = ('membership', 'team', 'organization', 'installation', 'ping')


class GitHubUnavailable(Exception):
    """GitHub could not be reached, refused the App's credentials or answered badly."""


class InvalidSignature(Exception):
    """A webhook delivery whose X-Hub-Signature-256 does not verify."""


class ReplayedDelivery(Exception):
    """A webhook delivery id that was already applied."""


def _https(value, name):
    if not isinstance(value, str):
        raise ValueError('github_configuration:' + name)
    parsed = urlsplit(value)
    loopback = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if (parsed.scheme not in ('https', 'http') or (parsed.scheme == 'http' and not loopback) or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('github_configuration:' + name)
    return value.rstrip('/')


def verify_signature(secret, body, header):
    """X-Hub-Signature-256 over the exact request bytes, compared in constant time."""
    if not isinstance(header, str) or not header.startswith('sha256=') or not secret:
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode())


class GitHubApp:
    """One GitHub App: its configuration, secrets and the few API calls Vaelius makes."""

    def __init__(self, settings, *, http=None, clock=time.time):
        if not isinstance(settings, dict) or set(settings) - SETTINGS:
            raise ValueError('github_configuration')
        for key in ('app_id', 'client_id', 'private_key_file', 'webhook_secret_file', 'installations'):
            if key not in settings:
                raise ValueError('github_configuration:' + key)
        app_id = settings['app_id']
        if type(app_id) is not int or app_id <= 0:
            raise ValueError('github_configuration:app_id')
        if not isinstance(settings['client_id'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', settings['client_id']):
            raise ValueError('github_configuration:client_id')
        self.app_id = app_id
        self.client_id = settings['client_id']
        self.reconcile_seconds = settings.get('reconcile_seconds', DEFAULT_RECONCILE_SECONDS)
        self.max_staleness_seconds = settings.get('max_staleness_seconds', DEFAULT_MAX_STALENESS_SECONDS)
        if type(self.reconcile_seconds) is not int or not 60 <= self.reconcile_seconds <= 86400:
            raise ValueError('github_configuration:reconcile_seconds')
        if (type(self.max_staleness_seconds) is not int or not 600 <= self.max_staleness_seconds <= 30 * 86400
                or self.max_staleness_seconds <= self.reconcile_seconds):
            raise ValueError('github_configuration:max_staleness_seconds')
        self.api_url = _https(settings.get('api_url', 'https://api.github.com'), 'api_url')
        self.web_url = _https(settings.get('web_url', 'https://github.com'), 'web_url')
        installations = settings['installations']
        if not isinstance(installations, list) or not 1 <= len(installations) <= 64:
            raise ValueError('github_configuration:installations')
        self.installations = {}
        for item in installations:
            if (not isinstance(item, dict) or set(item) != {'id', 'tenant', 'org'} or type(item['id']) is not int
                    or item['id'] <= 0 or not isinstance(item['tenant'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', item['tenant'])
                    or not isinstance(item['org'], str) or not LOGIN.fullmatch(item['org'])
                    or item['id'] in self.installations):
                raise ValueError('github_configuration:installations')
            self.installations[item['id']] = {'tenant': item['tenant'], 'org': item['org'].lower()}
        # Owner-only files, re-read after rotation (agenthub.secret_files); never logged.
        self._private_key = MountedSecret(settings['private_key_file'])
        self._webhook_secret = MountedSecret(settings['webhook_secret_file'])
        if http is None:
            import requests
            http = requests.Session()
            http.trust_env = False
        self.http = http
        self.clock = clock
        self._tokens = {}
        self._lock = threading.Lock()

    def __repr__(self):
        return 'GitHubApp(app_id=%d)' % self.app_id

    def tenant_installations(self, tenant):
        return sorted((ident, item['org']) for ident, item in self.installations.items() if item['tenant'] == tenant)

    def tenants(self):
        return sorted({item['tenant'] for item in self.installations.values()})

    def verify(self, body, header):
        return verify_signature(self._webhook_secret.value(), body, header)

    # HTTP --------------------------------------------------------------------------

    def _call(self, method, url, *, headers, body=None, form=None, limit=1048576):
        try:
            if method == 'GET':
                response = self.http.get(url, headers=headers, timeout=10, allow_redirects=False)
            else:
                response = self.http.post(url, headers=headers, json=body, data=form, timeout=10,
                                          allow_redirects=False)
        except Exception:
            raise GitHubUnavailable('unreachable') from None
        if len(response.content) > limit:
            raise GitHubUnavailable('response_too_large')
        return response

    def _app_jwt(self):
        from joserfc import jwt
        from joserfc.jwk import RSAKey
        now = int(self.clock())
        try:
            key = RSAKey.import_key(self._private_key.value())
        except Exception:
            raise GitHubUnavailable('private_key_unusable') from None
        # GitHub accepts at most 10 minutes; backdated a minute for clock drift.
        return jwt.encode({'alg': 'RS256'}, {'iat': now - 60, 'exp': now + 540, 'iss': str(self.app_id)}, key)

    def installation_token(self, installation):
        """A cached installation token narrowed to TOKEN_PERMISSIONS."""
        with self._lock:
            cached = self._tokens.get(installation)
            if cached and cached[1] - 300 > self.clock():
                return cached[0]
            response = self._call('POST', '%s/app/installations/%d/access_tokens' % (self.api_url, installation),
                                  headers={'Accept': 'application/vnd.github+json',
                                           'Authorization': 'Bearer ' + self._app_jwt(),
                                           'X-GitHub-Api-Version': '2022-11-28'},
                                  body={'permissions': TOKEN_PERMISSIONS}, limit=65536)
            if response.status_code in (401, 403, 404):
                raise GitHubUnavailable('credentials_rejected')
            if response.status_code != 201:
                raise GitHubUnavailable('token_status_%d' % response.status_code)
            try:
                value = response.json()
                token = value['token']
                if not isinstance(token, str) or not 1 <= len(token) <= 1024:
                    raise ValueError()
            except Exception:
                raise GitHubUnavailable('token_response') from None
            # An hour by GitHub's rule; cached for at most 50 minutes either way.
            self._tokens[installation] = (token, self.clock() + 3300)
            return token

    def _pages(self, path, token):
        url = self.api_url + path + ('&' if '?' in path else '?') + 'per_page=100'
        items = []
        for _ in range(MAX_PAGES):
            response = self._call('GET', url, headers={'Accept': 'application/vnd.github+json',
                                                       'Authorization': 'Bearer ' + token,
                                                       'X-GitHub-Api-Version': '2022-11-28'})
            if response.status_code in (401, 403):
                raise GitHubUnavailable('credentials_rejected')
            if response.status_code == 404:
                raise LookupError('not_found')
            if response.status_code != 200:
                raise GitHubUnavailable('status_%d' % response.status_code)
            try:
                page = response.json()
                if not isinstance(page, list):
                    raise ValueError()
            except Exception:
                raise GitHubUnavailable('list_response') from None
            items.extend(page)
            following = _next_link(response.headers.get('link') or response.headers.get('Link') or '')
            if following is None:
                return items
            if not following.startswith(self.api_url + '/'):
                raise GitHubUnavailable('pagination_host')
            url = following
        raise GitHubUnavailable('pagination_bound')

    def teams(self, installation, org):
        """[{id, slug, name, parent}] for every team the installation can see."""
        token = self.installation_token(installation)
        try:
            rows = self._pages('/orgs/%s/teams' % quote(org, safe=''), token)
        except LookupError:
            raise GitHubUnavailable('organization_not_found') from None
        teams = []
        for row in rows:
            parent = row.get('parent') if isinstance(row, dict) else None
            if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                    or not isinstance(row.get('slug'), str) or not SLUG.fullmatch(row['slug'])
                    or (parent is not None and (not isinstance(parent, dict) or type(parent.get('id')) is not int))):
                raise GitHubUnavailable('team_response')
            teams.append({'id': row['id'], 'slug': row['slug'], 'name': str(row.get('name') or row['slug'])[:200],
                          'parent': parent['id'] if parent else None})
        return teams

    def team_members(self, installation, org, slug):
        """{user id: login}; GitHub includes the members of child teams. None if the team is gone."""
        token = self.installation_token(installation)
        try:
            rows = self._pages('/orgs/%s/teams/%s/members' % (quote(org, safe=''), quote(slug, safe='')), token)
        except LookupError:
            return None
        members = {}
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                    or not isinstance(row.get('login'), str) or not LOGIN.fullmatch(row['login'])):
                raise GitHubUnavailable('member_response')
            members[row['id']] = row['login']
        return members

    # Device flow (RFC 8628 as GitHub implements it) ------------------------------------

    def device_code(self):
        response = self._call('POST', self.web_url + '/login/device/code', headers={'Accept': 'application/json'},
                              form={'client_id': self.client_id}, limit=65536)
        try:
            value = response.json()
            if response.status_code != 200 or 'error' in value:
                raise ValueError()
            result = {key: value[key] for key in ('device_code', 'user_code', 'verification_uri', 'expires_in', 'interval')}
            if (not all(isinstance(result[k], str) and 1 <= len(result[k]) <= 512
                        for k in ('device_code', 'user_code', 'verification_uri'))
                    or type(result['expires_in']) is not int or type(result['interval']) is not int
                    or not 1 <= result['interval'] <= 60 or not 60 <= result['expires_in'] <= 3600
                    or not result['verification_uri'].startswith('https://')):
                raise ValueError()
        except Exception:
            raise GitHubUnavailable('device_code_response') from None
        return result

    def poll_device(self, device_code):
        """('token', token) | ('pending'|'slow_down'|'expired'|'denied', None)."""
        response = self._call('POST', self.web_url + '/login/oauth/access_token',
                              headers={'Accept': 'application/json'},
                              form={'client_id': self.client_id, 'device_code': device_code,
                                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}, limit=65536)
        try:
            value = response.json()
        except Exception:
            raise GitHubUnavailable('device_token_response') from None
        if response.status_code == 200 and isinstance(value.get('access_token'), str) and value['access_token']:
            return 'token', value['access_token']
        error = value.get('error') if isinstance(value, dict) else None
        if error == 'authorization_pending':
            return 'pending', None
        if error == 'slow_down':
            return 'slow_down', None
        if error in ('expired_token', 'incorrect_device_code'):
            return 'expired', None
        if error == 'access_denied':
            return 'denied', None
        raise GitHubUnavailable('device_flow_' + (error if isinstance(error, str) and error.isidentifier() else 'error'))

    def user(self, token):
        """(numeric id, login) of the user token's account. The token is not kept."""
        response = self._call('GET', self.api_url + '/user', headers={'Accept': 'application/vnd.github+json',
                                                                    'Authorization': 'Bearer ' + token,
                                                                    'X-GitHub-Api-Version': '2022-11-28'},
                              limit=65536)
        try:
            value = response.json()
            if (response.status_code != 200 or type(value.get('id')) is not int or value['id'] <= 0
                    or not isinstance(value.get('login'), str) or not LOGIN.fullmatch(value['login'])):
                raise ValueError()
        except Exception:
            raise GitHubUnavailable('user_response') from None
        return value['id'], value['login']


def _next_link(header):
    for part in header.split(','):
        pieces = part.strip().split(';')
        if len(pieces) >= 2 and pieces[0].strip().startswith('<') and pieces[0].strip().endswith('>') \
                and any(p.strip() == 'rel="next"' for p in pieces[1:]):
            return pieces[0].strip()[1:-1]
    return None


def group_id(team_id):
    return 'gh-%d' % team_id


def group_name(org, slug):
    name = 'gh_%s_%s' % (org.lower(), slug)
    return name if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', name) else None


class GitHubSyncMixin:
    """Store side of the sync: snapshots, edges, staleness, webhooks and identity links."""

    github = None  # GitHubApp, set by the runtime factory when `github` is configured

    def _github_max_staleness(self):
        return self.github.max_staleness_seconds if self.github is not None else DEFAULT_MAX_STALENESS_SECONDS

    def _github_ctx(self):
        return {'tenant': self.tenant_id, 'actor': SYNC_ACTOR, 'principal': SYNC_ACTOR}

    def _github_require(self):
        if self.github is None or not self.github.tenant_installations(self.tenant_id):
            raise FileNotFoundError('github_not_configured')
        return self.github

    # Edges ---------------------------------------------------------------------------

    def _github_rebuild(self, db, groups, now):
        """Rewrite the principal edges of github groups from the member snapshot and the
        active identity links of active principals, then their closure and workspaces."""
        groups = sorted(set(groups))
        if not groups:
            return
        tenant = self.tenant_id
        db.execute('''DELETE FROM group_edges e USING groups g WHERE g.tenant=e.tenant AND g.id=e.group_id
            AND e.tenant=? AND g.source='github' AND e.member_kind='principal' AND e.group_id=ANY(?::text[])''',
                   (tenant, groups))
        # A member counts only through a verified, active link of an active principal.
        db.execute('''INSERT INTO group_edges(tenant,group_id,member_kind,member,role,added_by,created)
            SELECT DISTINCT t.tenant,t.group_id,'principal',l.principal,'member',?,? FROM github_teams t
            JOIN groups g ON g.tenant=t.tenant AND g.id=t.group_id AND g.active=1
            JOIN github_team_members m ON m.tenant=t.tenant AND m.team_id=t.team_id
            JOIN identity_links l ON l.tenant=t.tenant AND l.provider='github' AND l.active=1
                AND l.external_id=m.user_id::text
            JOIN enterprise_principals p ON p.tenant=l.tenant AND p.id=l.principal AND p.active=1
            WHERE t.tenant=? AND t.group_id=ANY(?::text[]) ON CONFLICT DO NOTHING''',
                   (SYNC_ACTOR, now, tenant, groups))
        self._refresh_groups(db, tenant, groups)

    def _github_groups_of_user(self, db, user_ids):
        ids = sorted({int(item) for item in user_ids if item is not None})
        if not ids:
            return []
        return [row[0] for row in db.execute('''SELECT DISTINCT t.group_id FROM github_team_members m
            JOIN github_teams t ON t.tenant=m.tenant AND t.team_id=m.team_id WHERE m.tenant=? AND m.user_id=ANY(?::bigint[])''',
            (self.tenant_id, ids)).fetchall()]

    def _github_state_change(self, db, group, state, now, *, synced):
        prior = db.execute('SELECT sync_state FROM groups WHERE tenant=? AND id=?', (self.tenant_id, group)).fetchone()
        if synced:
            db.execute('UPDATE groups SET sync_state=?,last_synced=? WHERE tenant=? AND id=?',
                       (state, now, self.tenant_id, group))
        else:
            db.execute('UPDATE groups SET sync_state=? WHERE tenant=? AND id=?', (state, self.tenant_id, group))
        if not prior or prior['sync_state'] != state:
            self._audit(db, self._github_ctx(), 'group_sync', 'applied' if state == 'current' else state, group)
            return True
        return False

    # Reconcile -----------------------------------------------------------------------

    def _github_installation_rows(self, db):
        for ident, org in self.github.tenant_installations(self.tenant_id):
            db.execute('''INSERT INTO github_installations(tenant,installation,org) VALUES(?,?,?)
                ON CONFLICT(tenant,installation) DO UPDATE SET org=excluded.org''', (self.tenant_id, ident, org))

    def github_reconcile(self, *, now=None, installation=None):
        """List every team and its members, replace the snapshot and rebuild edges.

        Network reads happen before the write transaction. A failure records the
        error and leaves the groups as they are; the staleness sweep then takes
        their members away once the last good sync is older than max_staleness."""
        app = self._github_require()
        now = time.time() if now is None else now
        results = []
        with self.open() as state, state.db:
            self._github_installation_rows(state.db)
            rows = {r['installation']: dict(r) for r in state.db.execute(
                'SELECT * FROM github_installations WHERE tenant=?', (self.tenant_id,)).fetchall()}
        for ident, org in app.tenant_installations(self.tenant_id):
            if installation is not None and ident != installation:
                continue
            if rows.get(ident, {}).get('state', 'active') != 'active':
                results.append({'installation': ident, 'status': rows[ident]['state']})
                continue
            try:
                teams = app.teams(ident, org)
                members = {}
                for team in teams:
                    found = app.team_members(ident, org, team['slug'])
                    if found is not None:
                        members[team['id']] = found
                teams = [team for team in teams if team['id'] in members]
            except GitHubUnavailable as failure:
                with self.open() as state, state.db:
                    state.db.execute('''UPDATE github_installations SET last_attempt=?,last_error=?
                        WHERE tenant=? AND installation=?''', (now, str(failure)[:200], self.tenant_id, ident))
                results.append({'installation': ident, 'status': 'error', 'error': str(failure)[:200]})
                continue
            with self.delivery_lock(), self.open() as state, state.db:
                applied = self._github_apply_installation(state.db, ident, org, teams, members, now)
            results.append(dict(applied, installation=ident, status='applied'))
        with self.delivery_lock(), self.open() as state, state.db:
            results_stale = self._github_expire(state.db, now)
        return {'installations': results, 'expired': results_stale}

    def _github_apply_installation(self, db, installation, org, teams, members, now):
        tenant = self.tenant_id
        seen = {team['id'] for team in teams}
        known = {row['team_id']: row['group_id'] for row in db.execute(
            'SELECT team_id,group_id FROM github_teams WHERE tenant=? AND installation=?', (tenant, installation))}
        changed = set()
        for team in teams:
            ident = group_id(team['id'])
            name = group_name(org, team['slug']) or ident
            clash = db.execute('SELECT 1 FROM groups WHERE tenant=? AND name=? AND id<>?', (tenant, name, ident)).fetchone()
            if clash:
                name = ident
            db.execute('''INSERT INTO groups(tenant,id,name,source,external_id,display,sync_state,last_synced,active,created)
                VALUES(?,?,?,'github',?,?,'current',?,1,?) ON CONFLICT(tenant,id) DO UPDATE SET name=excluded.name,
                display=excluded.display,active=1''',
                       (tenant, ident, name, str(team['id']), team['name'], now, now))
            self._github_state_change(db, ident, 'current', now, synced=True)
            db.execute('''INSERT INTO github_teams(tenant,team_id,installation,slug,parent_team_id,group_id)
                VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,team_id) DO UPDATE SET installation=excluded.installation,
                slug=excluded.slug,parent_team_id=excluded.parent_team_id,group_id=excluded.group_id''',
                       (tenant, team['id'], installation, team['slug'], team['parent'], ident))
            self._github_replace_members(db, team['id'], members[team['id']])
            changed.add(ident)
        # Teams that disappeared become inactive groups with no members.
        for team_id, ident in known.items():
            if team_id in seen:
                continue
            db.execute('DELETE FROM github_team_members WHERE tenant=? AND team_id=?', (tenant, team_id))
            db.execute('DELETE FROM github_teams WHERE tenant=? AND team_id=?', (tenant, team_id))
            db.execute('UPDATE groups SET active=0 WHERE tenant=? AND id=?', (tenant, ident))
            self._audit(db, self._github_ctx(), 'group_sync', 'team_removed', ident)
            changed.add(ident)
        # Nesting: a parent team's group contains its child teams' groups.
        db.execute('''DELETE FROM group_edges e USING groups g WHERE g.tenant=e.tenant AND g.id=e.group_id
            AND e.tenant=? AND g.source='github' AND e.member_kind='group' AND e.group_id=ANY(?::text[])''',
                   (tenant, sorted(changed)))
        db.execute('''INSERT INTO group_edges(tenant,group_id,member_kind,member,role,added_by,created)
            SELECT c.tenant,p.group_id,'group',c.group_id,'member',?,? FROM github_teams c
            JOIN github_teams p ON p.tenant=c.tenant AND p.team_id=c.parent_team_id
            WHERE c.tenant=? AND c.installation=? ON CONFLICT DO NOTHING''', (SYNC_ACTOR, now, tenant, installation))
        db.execute('''UPDATE github_installations SET last_success=?,last_attempt=?,last_error=NULL,
            next_reconcile=greatest(next_reconcile,?) WHERE tenant=? AND installation=?''',
                   (now, now, now + self.github.reconcile_seconds, tenant, installation))
        self._github_rebuild(db, changed, now)
        return {'teams': len(teams), 'removed_teams': len(set(known) - seen)}

    def _github_replace_members(self, db, team_id, members):
        tenant = self.tenant_id
        db.execute('DELETE FROM github_team_members WHERE tenant=? AND team_id=?', (tenant, team_id))
        for user_id, login in sorted(members.items()):
            db.execute('INSERT INTO github_team_members(tenant,team_id,user_id,login) VALUES(?,?,?,?)',
                       (tenant, team_id, user_id, login))
            # A rename keeps the link (it is by id); the stored login follows GitHub.
            db.execute('''UPDATE identity_links SET login=? WHERE tenant=? AND provider='github' AND active=1
                AND external_id=? AND login<>?''', (login, tenant, str(user_id), login))

    def github_sync_team(self, installation, team_id, *, now=None):
        """Re-read one team's members (a webhook hint) and apply them."""
        app = self._github_require()
        now = time.time() if now is None else now
        org = app.installations[installation]['org']
        with self.open() as state:
            row = state.db.execute('SELECT slug,group_id FROM github_teams WHERE tenant=? AND team_id=?',
                                   (self.tenant_id, team_id)).fetchone()
            inst = state.db.execute('SELECT state FROM github_installations WHERE tenant=? AND installation=?',
                                    (self.tenant_id, installation)).fetchone()
        if inst is not None and inst['state'] != 'active':
            return {'status': inst['state']}
        if row is None:
            # A team this service has not seen yet: a full reconcile places it.
            return self.github_reconcile(now=now, installation=installation)
        members = app.team_members(installation, org, row['slug'])
        if members is None:
            return self.github_reconcile(now=now, installation=installation)
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            self._github_replace_members(db, team_id, members)
            self._github_state_change(db, row['group_id'], 'current', now, synced=True)
            self._github_rebuild(db, [row['group_id']], now)
        return {'status': 'applied', 'group': row['group_id'], 'members': len(members)}

    # Staleness -------------------------------------------------------------------------

    def _github_expire(self, db, now):
        """Groups without a successful sync inside max_staleness contribute nobody (stale,
        or failed when their installation is suspended, deleted or erroring)."""
        cutoff = now - self._github_max_staleness()
        rows = db.execute('''SELECT g.id,coalesce(i.state,'deleted') AS installation_state,i.last_error
            FROM groups g LEFT JOIN github_teams t ON t.tenant=g.tenant AND t.group_id=g.id
            LEFT JOIN github_installations i ON i.tenant=t.tenant AND i.installation=t.installation
            WHERE g.tenant=? AND g.source='github' AND g.sync_state='current' AND g.active=1
            AND coalesce(g.last_synced,0)<?''', (self.tenant_id, cutoff)).fetchall()
        expired = []
        for row in rows:
            failed = row['installation_state'] != 'active' or row['last_error'] is not None
            self._github_state_change(db, row['id'], 'failed' if failed else 'stale', now, synced=False)
            expired.append(row['id'])
        if expired:
            self._refresh_groups(db, self.tenant_id, expired)
        return expired

    def github_expire_stale(self, *, now=None):
        now = time.time() if now is None else now
        with self.delivery_lock(), self.open() as state, state.db:
            return self._github_expire(state.db, now)

    def _github_installation_failed(self, db, installation, state, now):
        db.execute('''UPDATE github_installations SET state=?,last_attempt=? WHERE tenant=? AND installation=?''',
                   (state, now, self.tenant_id, installation))
        groups = [r[0] for r in db.execute('SELECT group_id FROM github_teams WHERE tenant=? AND installation=?',
                                           (self.tenant_id, installation))]
        for group in groups:
            self._github_state_change(db, group, 'failed', now, synced=False)
        if groups:
            self._refresh_groups(db, self.tenant_id, groups)
        return groups

    # Webhooks --------------------------------------------------------------------------

    def github_webhook(self, installation, event, delivery, payload, *, now=None):
        """Apply a verified delivery. Replays (a delivery id seen before) are refused."""
        app = self._github_require()
        now = time.time() if now is None else now
        if not DELIVERY.fullmatch(delivery or '') or event not in WEBHOOK_EVENTS or not isinstance(payload, dict):
            raise ValueError('invalid_webhook')
        with self.open() as state, state.db:
            db = state.db
            self._github_installation_rows(db)
            db.execute('DELETE FROM github_webhook_deliveries WHERE tenant=? AND received<?',
                       (self.tenant_id, now - DELIVERY_RETENTION_SECONDS))
            inserted = db.execute('''INSERT INTO github_webhook_deliveries(tenant,delivery,event,received)
                VALUES(?,?,?,?) ON CONFLICT DO NOTHING''', (self.tenant_id, delivery, event, now)).rowcount
        if not inserted:
            raise ReplayedDelivery()
        try:
            return self._github_apply_webhook(installation, event, payload, now)
        except Exception:
            # Not applied: forget the id so GitHub's redelivery of it is accepted.
            with self.open() as state, state.db:
                state.db.execute('DELETE FROM github_webhook_deliveries WHERE tenant=? AND delivery=?',
                                 (self.tenant_id, delivery))
            raise

    def _github_apply_webhook(self, installation, event, payload, now):
        action = payload.get('action')
        if event == 'ping':
            return {'status': 'pong'}
        if event == 'installation':
            if action in ('deleted', 'suspend'):
                with self.delivery_lock(), self.open() as state, state.db:
                    groups = self._github_installation_failed(state.db, installation,
                                                              'deleted' if action == 'deleted' else 'suspended', now)
                    self._audit(state.db, self._github_ctx(), 'github_installation', action, str(installation))
                return {'status': 'applied', 'failed_groups': len(groups)}
            if action in ('created', 'unsuspend', 'new_permissions_accepted'):
                with self.open() as state, state.db:
                    state.db.execute('''UPDATE github_installations SET state='active',next_reconcile=0
                        WHERE tenant=? AND installation=?''', (self.tenant_id, installation))
                    self._audit(state.db, self._github_ctx(), 'github_installation', action, str(installation))
                return self.github_reconcile(now=now, installation=installation)
            return {'status': 'ignored'}
        if event == 'membership':
            team = payload.get('team')
            if not isinstance(team, dict) or type(team.get('id')) is not int:
                raise ValueError('invalid_webhook')
            return self.github_sync_team(installation, team['id'], now=now)
        if event == 'organization':
            member = (payload.get('membership') or {}).get('user') if isinstance(payload.get('membership'), dict) else None
            if action == 'member_removed' and isinstance(member, dict) and type(member.get('id')) is int:
                # Removal is applied at once from the delivery: it can only take access away.
                with self.delivery_lock(), self.open() as state, state.db:
                    db = state.db
                    groups = [r[0] for r in db.execute('''SELECT t.group_id FROM github_team_members m
                        JOIN github_teams t ON t.tenant=m.tenant AND t.team_id=m.team_id
                        WHERE m.tenant=? AND m.user_id=? AND t.installation=?''',
                        (self.tenant_id, member['id'], installation))]
                    db.execute('''DELETE FROM github_team_members m USING github_teams t WHERE t.tenant=m.tenant
                        AND t.team_id=m.team_id AND m.tenant=? AND m.user_id=? AND t.installation=?''',
                               (self.tenant_id, member['id'], installation))
                    self._github_rebuild(db, groups, now)
                return {'status': 'applied', 'groups': len(groups)}
            if action in ('renamed', 'member_added'):
                with self.open() as state, state.db:
                    state.db.execute('''UPDATE github_installations SET next_reconcile=0 WHERE tenant=?
                        AND installation=?''', (self.tenant_id, installation))
                return {'status': 'reconcile_due'}
            return {'status': 'ignored'}
        if event == 'team':
            # Created, deleted, edited (name, parent) or moved: re-read the whole installation.
            return self.github_reconcile(now=now, installation=installation)
        return {'status': 'ignored'}

    def github_run_due(self, *, now=None):
        """One reconciler tick: reconcile installations whose time has come, then sweep."""
        now = time.time() if now is None else now
        due = []
        if self.github is not None and self.github.tenant_installations(self.tenant_id):
            with self.open() as state, state.db:
                self._github_installation_rows(state.db)
                due = [r[0] for r in state.db.execute('''UPDATE github_installations SET next_reconcile=?
                    WHERE tenant=? AND state='active' AND next_reconcile<=? RETURNING installation''',
                    (now + min(self.github.reconcile_seconds, 300), self.tenant_id, now)).fetchall()]
                state.db.execute('DELETE FROM identity_link_flows WHERE tenant=? AND expires<?', (self.tenant_id, now))
        expired = []
        for installation in due:
            expired += self.github_reconcile(now=now, installation=installation)['expired']
        return {'reconciled': due, 'expired': sorted(set(expired + self.github_expire_stale(now=now)))}

    def github_status(self, *, now=None):
        now = time.time() if now is None else now
        expired = self.github_expire_stale(now=now)
        with self.open() as state:
            db = state.db
            installations = [dict(r) for r in db.execute('''SELECT installation,org,state,last_success,last_attempt,
                last_error,next_reconcile FROM github_installations WHERE tenant=? ORDER BY installation''',
                (self.tenant_id,))]
            groups = db.execute('''SELECT g.id,g.name,g.sync_state,g.last_synced,g.active FROM groups g
                WHERE g.tenant=? AND g.source='github' ORDER BY g.name''', (self.tenant_id,)).fetchall()
            unlinked = db.execute('''SELECT count(DISTINCT m.user_id) FROM github_team_members m WHERE m.tenant=?
                AND NOT EXISTS(SELECT 1 FROM identity_links l WHERE l.tenant=m.tenant AND l.provider='github'
                    AND l.active=1 AND l.external_id=m.user_id::text)''', (self.tenant_id,)).fetchone()[0]
            links = db.execute('''SELECT count(*) FROM identity_links WHERE tenant=? AND provider='github'
                AND active=1''', (self.tenant_id,)).fetchone()[0]
        configured = self.github.tenant_installations(self.tenant_id) if self.github is not None else []
        return {'configured': bool(configured), 'installations': installations,
                'max_staleness_seconds': self._github_max_staleness(),
                'reconcile_seconds': self.github.reconcile_seconds if self.github is not None else None,
                'groups': len([g for g in groups if g['active']]),
                'stale_groups': [{'group': g['id'], 'name': g['name'], 'sync_state': g['sync_state'],
                                  'last_synced': g['last_synced']} for g in groups
                                 if g['active'] and g['sync_state'] != 'current'],
                'linked_principals': links, 'unlinked_members': unlinked, 'expired_now': expired}

    def github_mark_due(self):
        self._github_require()
        with self.open() as state, state.db:
            self._github_installation_rows(state.db)
            state.db.execute("UPDATE github_installations SET next_reconcile=0 WHERE tenant=? AND state='active'",
                             (self.tenant_id,))
        return {'reconcile_due': True}

    # Identity link ----------------------------------------------------------------------

    def _github_self(self, db, ctx):
        from agenthub.enterprise import Denied
        if hasattr(self, 'current_identity'):
            self.current_identity(db, ctx)
        if ctx.get('acting_for') or ctx.get('delegated_projects') is not None:
            raise Denied()
        return ctx['principal']

    def _github_link_view(self, db, principal):
        row = db.execute('''SELECT external_id,login,linked FROM identity_links WHERE tenant=? AND provider='github'
            AND principal=? AND active=1''', (self.tenant_id, principal)).fetchone()
        if not row:
            return {'provider': 'github', 'linked': False}
        groups = db.execute('''SELECT count(*) FROM group_edges e JOIN groups g ON g.tenant=e.tenant AND g.id=e.group_id
            WHERE e.tenant=? AND e.member_kind='principal' AND e.member=? AND g.source='github' ''',
            (self.tenant_id, principal)).fetchone()[0]
        return {'provider': 'github', 'linked': True, 'user_id': int(row['external_id']), 'login': row['login'],
                'linked_at': row['linked'], 'github_groups': groups}

    def github_identity(self, ctx, value=None):
        if value not in (None, {}):
            raise ValueError('invalid_request')
        with self.open() as state:
            principal = self._github_self(state.db, ctx)
            return self._github_link_view(state.db, principal)

    def github_link_start(self, ctx, value=None):
        """Begin a device-flow authorization; the client shows the code and polls."""
        if value not in (None, {}):
            raise ValueError('invalid_request')
        app = self._github_require()
        now = time.time()
        with self.open() as state, state.db:
            principal = self._github_self(state.db, ctx)
            state.db.execute('DELETE FROM identity_link_flows WHERE tenant=? AND expires<?', (self.tenant_id, now))
            pending = state.db.execute('SELECT count(*) FROM identity_link_flows WHERE tenant=? AND principal=?',
                                       (self.tenant_id, principal)).fetchone()[0]
            if pending >= MAX_PENDING_FLOWS:
                from agenthub.enterprise import Conflict
                raise Conflict()
        code = app.device_code()
        flow = secrets.token_hex(16)
        with self.open() as state, state.db:
            state.db.execute('''INSERT INTO identity_link_flows(tenant,id,principal,credential_digest,device_code,
                interval_seconds,next_poll,expires,created) VALUES(?,?,?,?,?,?,?,?,?)''',
                (self.tenant_id, flow, principal, ctx.get('credential_digest', ''), code['device_code'],
                 code['interval'], now + code['interval'], now + code['expires_in'], now))
        return {'flow': flow, 'user_code': code['user_code'], 'verification_uri': code['verification_uri'],
                'expires_in': code['expires_in'], 'interval': code['interval']}

    def github_link_poll(self, ctx, value):
        """Poll the flow once. On approval: read the account id, discard the token, link."""
        from agenthub.enterprise import Conflict, Denied
        if not isinstance(value, dict) or set(value) != {'flow'} or not isinstance(value['flow'], str) \
                or not FLOW.fullmatch(value['flow']):
            raise ValueError('invalid_request')
        app = self._github_require()
        now = time.time()
        with self.open() as state:
            principal = self._github_self(state.db, ctx)
            flow = state.db.execute('SELECT * FROM identity_link_flows WHERE tenant=? AND id=?',
                                    (self.tenant_id, value['flow'])).fetchone()
        if not flow or flow['principal'] != principal:
            raise Denied()
        if flow['expires'] <= now:
            self._github_end_flow(flow['id'])
            return {'status': 'expired'}
        if now < flow['next_poll']:
            return {'status': 'pending', 'interval': flow['interval_seconds']}
        status, token = app.poll_device(flow['device_code'])
        if status in ('pending', 'slow_down'):
            interval = flow['interval_seconds'] + (5 if status == 'slow_down' else 0)
            with self.open() as state, state.db:
                state.db.execute('''UPDATE identity_link_flows SET interval_seconds=?,next_poll=? WHERE tenant=?
                    AND id=?''', (interval, now + interval, self.tenant_id, flow['id']))
            return {'status': 'pending', 'interval': interval}
        if status in ('expired', 'denied'):
            self._github_end_flow(flow['id'])
            return {'status': status}
        try:
            user_id, login = app.user(token)
        finally:
            token = None  # never stored, logged or returned
        self._github_end_flow(flow['id'])
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            self._github_self(db, ctx)
            holder = db.execute('''SELECT principal FROM identity_links WHERE tenant=? AND provider='github'
                AND external_id=? AND active=1''', (self.tenant_id, str(user_id))).fetchone()
            refused = bool(holder and holder['principal'] != principal)
            if refused:
                # Recorded and committed; the conflict is raised after the transaction.
                self._audit(db, ctx, 'identity_link', 'refused_linked_elsewhere', 'github:%d' % user_id)
                view = None
            else:
                view = self._github_link_write(db, ctx, principal, holder, user_id, login, now)
        if refused:
            raise Conflict()
        return dict(view, status='linked')

    def _github_link_write(self, db, ctx, principal, holder, user_id, login, now):
        prior = db.execute('''SELECT external_id FROM identity_links WHERE tenant=? AND provider='github'
            AND principal=? AND active=1''', (self.tenant_id, principal)).fetchone()
        if holder:
            db.execute('''UPDATE identity_links SET login=? WHERE tenant=? AND provider='github' AND external_id=?
                AND active=1''', (login, self.tenant_id, str(user_id)))
            self._audit(db, ctx, 'identity_link', 'reverified', 'github:%d' % user_id)
        else:
            if prior:
                db.execute('''UPDATE identity_links SET active=0,deactivated=?,deactivated_by=? WHERE tenant=?
                    AND provider='github' AND principal=? AND active=1''', (now, principal, self.tenant_id, principal))
            db.execute('''INSERT INTO identity_links(tenant,provider,external_id,principal,login,active,linked,linked_by)
                VALUES(?,'github',?,?,?,1,?,?)''', (self.tenant_id, str(user_id), principal, login, now, principal))
            self._audit(db, ctx, 'identity_link', 'relinked' if prior else 'linked', 'github:%d' % user_id)
            self._github_rebuild(db, self._github_groups_of_user(
                db, [user_id, int(prior['external_id']) if prior else None]), now)
        return self._github_link_view(db, principal)

    def _github_end_flow(self, flow):
        with self.open() as state, state.db:
            state.db.execute('DELETE FROM identity_link_flows WHERE tenant=? AND id=?', (self.tenant_id, flow))

    def github_unlink(self, ctx, value=None):
        """Unlink one's own account, or (settings administrator) another principal's."""
        from agenthub.enterprise import Denied
        value = value or {}
        if not isinstance(value, dict) or set(value) - {'principal'}:
            raise ValueError('invalid_request')
        with self.delivery_lock(), self.open() as state, state.db:
            db = state.db
            if value.get('principal') not in (None, ctx.get('principal')):
                if hasattr(self, 'current_identity'):
                    self.current_identity(db, ctx)
                if not self._settings_admin(db, ctx):
                    raise Denied()
                principal, disposition = value['principal'], 'unlinked_by_admin'
            else:
                principal, disposition = self._github_self(db, ctx), 'unlinked'
            removed = self._github_deactivate_links(db, principal, ctx, disposition)
        return {'provider': 'github', 'principal': principal, 'unlinked': bool(removed)}

    def _github_deactivate_links(self, db, principal, ctx, disposition, now=None):
        now = time.time() if now is None else now
        rows = db.execute('''UPDATE identity_links SET active=0,deactivated=?,deactivated_by=? WHERE tenant=?
            AND principal=? AND active=1 RETURNING provider,external_id''',
                          (now, ctx['actor'], self.tenant_id, principal)).fetchall()
        for row in rows:
            self._audit(db, ctx, 'identity_link', disposition, '%s:%s' % (row['provider'], row['external_id']))
        if rows:
            self._github_rebuild(db, self._github_groups_of_user(db, [int(r['external_id']) for r in rows]), now)
        return len(rows)


class GitHubReconciler:
    """Background ticks in the API process: due reconciles and the staleness sweep.

    Every tick is independent and exception-safe, and the sweep needs only the
    database, so groups still go stale while GitHub is unreachable."""

    def __init__(self, registry, app, *, poll_seconds=60):
        self.registry = registry
        self.app = app
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = None

    def tick(self, now=None):
        results = {}
        for tenant in self.app.tenants():
            try:
                results[tenant] = self.registry.resolve(tenant).github_run_due(now=now)
            except Exception as failure:  # one tenant's failure never stops the others
                results[tenant] = {'error': type(failure).__name__}
        return results

    def _run(self):
        while not self._stop.wait(self.poll_seconds):
            self.tick()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='vaelius-github-sync', daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()


def webhook(registry, app, headers, body, *, now=None):
    """Verify and route one delivery. Returns (status code, JSON body)."""
    if not app.verify(body, headers.get('x-hub-signature-256')):
        raise InvalidSignature()
    event = headers.get('x-github-event', '')
    delivery = headers.get('x-github-delivery', '')
    try:
        payload = json.loads(bytes(body))
    except ValueError:
        raise ValueError('invalid_webhook') from None
    if not isinstance(payload, dict) or event not in WEBHOOK_EVENTS or not DELIVERY.fullmatch(delivery):
        raise ValueError('invalid_webhook')
    installation = payload.get('installation', {}).get('id') if isinstance(payload.get('installation'), dict) else None
    if type(installation) is not int or installation not in app.installations:
        return 202, {'status': 'ignored'}
    store = registry.resolve(app.installations[installation]['tenant'])
    try:
        result = store.github_webhook(installation, event, delivery, payload, now=now)
    except GitHubUnavailable:
        # Accepted; the reconcile and the staleness sweep take over.
        return 202, {'status': 'deferred'}
    status = 'applied' if result.get('status') != 'ignored' else 'ignored'
    return 200, {'status': status}
