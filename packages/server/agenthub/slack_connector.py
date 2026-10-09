"""Bounded read-only selected-channel Slack adapter through canonical intake.

No DMs, Slack Connect/shared channels, vendor registration or file-sharing ACL
claims. Explicit configuration is supplied by the operator, never source content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from agentclient.enterprise_capture import normalize_capture, redact
from agentclient.general_contract import canonical, digest


class SlackFailure(Exception):
    pass


class SlackHeld(SlackFailure):
    pass


class SlackRateLimited(SlackFailure):
    def __init__(self, method, retry_at):
        self.method = method; self.retry_at = retry_at
        super().__init__('slack_rate_limited')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SlackFailure('slack_redirect_rejected')


class SlackHTTP:
    """No ambient credentials; local endpoint by default, explicit vendor opt-in."""
    METHODS = {'auth.test','conversations.list','conversations.info',
        'conversations.members','conversations.history','conversations.replies'}

    def __init__(self, token, *, endpoint='https://slack.com/api/', allow_vendor=False,
                 local_hosts=(), clock=time.time):
        url = urlsplit(endpoint)
        local = url.hostname in {'localhost','127.0.0.1','::1',*local_hosts}
        vendor = allow_vendor and url.scheme == 'https' and url.hostname == 'slack.com' and url.path == '/api/'
        if (not (local or vendor) or url.scheme not in {'http','https'} or url.username
                or url.password or url.query or url.fragment or not token):
            raise ValueError('explicit_slack_endpoint_required')
        self.token = token; self.endpoint = endpoint.rstrip('/') + '/'; self.clock = clock
        self.opener = build_opener(_NoRedirect())

    def call(self, method, **params):
        if method not in self.METHODS:
            raise ValueError('readonly_slack_method_required')
        request = Request(self.endpoint + method + '?' + urlencode(params),
            headers={'Authorization':'Bearer ' + self.token,'Accept':'application/json'})
        try:
            response = self.opener.open(request,timeout=15)
        except HTTPError as error:
            if error.code == 429:
                raw = error.headers.get('Retry-After','60')
                try:
                    delay = max(1,min(86400,int(raw)))
                except ValueError:
                    delay = 60
                raise SlackRateLimited(method,self.clock()+delay) from None
            raise SlackFailure('slack_http_failure') from None
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise SlackFailure('slack_response_bound')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SlackFailure('slack_invalid_response') from None
        if not isinstance(value,dict) or value.get('ok') is not True:
            # Vendor error messages are not safe telemetry payloads.
            raise SlackFailure('slack_method_failed')
        return value


def verify_event(raw, headers, signing_secret, *, now=None):
    if len(raw) > 1024 * 1024:
        raise ValueError('slack_event_bound')
    lower = {key.lower():value for key,value in headers.items()}
    timestamp = lower.get('x-slack-request-timestamp','')
    try:
        when = int(timestamp)
    except (ValueError, TypeError):
        raise ValueError('slack_signature_invalid') from None
    if abs((time.time() if now is None else now)-when) > 300:
        raise ValueError('slack_event_timestamp_expired')
    calculated = 'v0=' + hmac.new(signing_secret.encode(),
        b'v0:' + timestamp.encode() + b':' + raw,hashlib.sha256).hexdigest()
    signature = lower.get('x-slack-signature','')
    if not hmac.compare_digest(signature,calculated):
        raise ValueError('slack_signature_invalid')
    try:
        event = json.loads(raw)
    except ValueError:
        raise ValueError('slack_event_invalid') from None
    if not isinstance(event,dict):
        raise ValueError('slack_event_invalid')
    return event


def _timestamp(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,16}(?:\.[0-9]{1,6})?',value):
        raise ValueError('slack_timestamp_invalid')
    return Decimal(value)


class SlackConnector:
    DELETE_REVISION_LIMIT = 100
    DELETE_SECONDS = 60
    CAPABILITIES = {'source':'selected channels and complete fetched threads',
        'reader_policy':'verified member bindings; stale/unknown mappings hold',
        'unsupported':'DMs, Slack Connect/shared channels, files and unmapped bots',
        'deletion':'authenticated explicit deletion only; absence never deletes',
        'membership_expansion':'operator re-enrollment required after narrowed grants',
        'model_calls':'zero connector model calls; canonical backend observer handles conversations'}

    def __init__(self, store, http, *, clock=time.time):
        self.store = store; self.http = http; self.clock = clock

    def _row(self, ctx, ident):
        from agenthub.enterprise import Denied
        with self.store.open() as state:
            row = state.db.execute('SELECT * FROM backend_slack_connectors WHERE id=%s AND tenant=%s', (ident,ctx['tenant'])).fetchone()
            if not row:
                raise Denied()
            self.store._connection(state.db,ctx,row['connection'],allow_stale=True)
            return dict(row)

    def _call(self, ident, method, **params):
        with self.store.open() as state:
            deadline = state.db.execute('SELECT retry_at FROM backend_slack_limits WHERE connector=%s AND method=%s', (ident,method)).fetchone()
        if deadline and deadline['retry_at'] > self.clock():
            raise SlackRateLimited(method,deadline['retry_at'])
        try:
            return self.http.call(method,**params)
        except SlackRateLimited as error:
            with self.store.open() as state,state.db:
                state.db.execute('''INSERT INTO backend_slack_limits VALUES(%s,%s,%s)
                    ON CONFLICT(connector,method) DO UPDATE SET retry_at=excluded.retry_at''', (ident,method,error.retry_at))
            raise

    def _pages(self, ident, method, field, *, max_pages=30, max_items=10000, **params):
        cursor = ''; seen = set(); items = []
        for _ in range(max_pages):
            value = self._call(ident,method,**(params | {'cursor':cursor,'limit':15}))
            batch = value.get(field)
            if not isinstance(batch,list):
                raise SlackFailure('slack_page_shape')
            items.extend(batch)
            if len(items) > max_items:
                raise SlackFailure('slack_snapshot_bound')
            following = value.get('response_metadata',{}).get('next_cursor','')
            if not isinstance(following,str) or len(following)>2048:
                raise SlackFailure('slack_cursor_invalid')
            if not following:
                if value.get('has_more') is True:
                    raise SlackFailure('slack_incomplete_pagination')
                return items
            if following in seen:
                raise SlackFailure('slack_repeated_cursor')
            seen.add(following);cursor = following
        raise SlackFailure('slack_page_bound')

    @staticmethod
    def _channel_allowed(channel):
        if (not isinstance(channel,dict) or channel.get('is_im') or channel.get('is_mpim')
                or channel.get('is_shared') or channel.get('is_ext_shared')
                or channel.get('is_org_shared') or channel.get('is_archived')):
            raise SlackHeld('slack_channel_capability_excluded')

    def discover(self, *, max_pages=3):
        # Discovery reports bounded channel metadata; it does not enroll or read.
        channels = self._pages('', 'conversations.list','channels',max_pages=max_pages,
            types='public_channel,private_channel',exclude_archived='true')
        result = []
        for channel in channels:
            try:
                self._channel_allowed(channel)
            except SlackHeld:
                continue
            result.append({'id':channel['id'],'name':str(channel.get('name',''))[:128],
                'private':bool(channel.get('is_private'))})
        return result

    def enroll(self, ctx, ident, team, channel, project, bindings, *, freshness=3600):
        self.store._need(ctx,'ingest'); self.store._need(ctx,'policy')
        if (not all(isinstance(x,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',x) for x in (ident,team,channel))
                or not isinstance(bindings,dict) or not bindings or not 1<=freshness<=86400):
            raise ValueError('invalid_slack_enrollment')
        auth = self.http.call('auth.test')
        if auth.get('team_id') != team:
            raise SlackHeld('slack_workspace_mismatch')
        info = self.http.call('conversations.info',channel=channel).get('channel')
        self._channel_allowed(info)
        if info.get('id') != channel:
            raise SlackHeld('slack_channel_mismatch')
        connection = 'slack-' + ident
        # Bound initial visibility to the supplied verified enterprise IDs.
        # Source authors, display names and email addresses cannot grant access.
        readers = sorted(set(bindings.values()))
        with self.store.open() as state:
            for upstream,principal in bindings.items():
                if not isinstance(upstream,str) or not isinstance(principal,str) or not state.db.execute(
                    'SELECT 1 FROM enterprise_principals WHERE tenant=%s AND id=%s AND active=1',
                    (ctx['tenant'],principal)).fetchone():
                    raise ValueError('unverified_slack_binding')
        self.store.enroll_connection(ctx,connection,'slack:'+team+':'+channel,project,['conversation'],
            visibility='team',reader_ids=readers,freshness_seconds=freshness,capabilities=self.CAPABILITIES)
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO backend_slack_connectors
                (id,tenant,connection,team,channel,project,bindings,freshness,updated)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (ident,ctx['tenant'],connection,team,channel,project,json.dumps(bindings,sort_keys=True),freshness,self.clock()))
            # No captured content becomes readable until membership reconciliation.
            state.db.execute('UPDATE backend_connections SET permission_observed=0 WHERE id=%s',(connection,))
        return {'connector':ident,'connection':connection,'capabilities':self.CAPABILITIES}

    def _hold(self, row, reason):
        with self.store.delivery_lock(),self.store.open() as state,state.db:
            state.db.execute('UPDATE backend_connections SET permission_observed=0 WHERE id=%s', (row['connection'],))
            for source in state.db.execute('SELECT source_id FROM backend_source_revisions WHERE connection=%s', (row['connection'],)).fetchall():
                self.store._invalidate_source(state.db,source['source_id'],reason)
            state.db.execute("UPDATE backend_slack_connectors SET status='held',observed=0,updated=%s WHERE id=%s", (self.clock(),row['id']))

    def _members(self, ctx, row):
        info = self._call(row['id'],'conversations.info',channel=row['channel']).get('channel')
        self._channel_allowed(info)
        if info.get('id') != row['channel']:
            raise SlackHeld('slack_channel_mismatch')
        members = self._pages(row['id'],'conversations.members','members',channel=row['channel'])
        bindings = json.loads(row['bindings'])
        if not members or any(not isinstance(m,str) or m not in bindings for m in members):
            raise SlackHeld('slack_unknown_readership')
        readers = sorted({bindings[m] for m in members})
        # Existing canonical policy permits narrowing, not silent expansion.
        with self.store.open() as state:
            current = self.store._connection(state.db,ctx,row['connection'],allow_stale=True)
            prior = set(json.loads(current['reader_ids']))
            if not set(readers)<=prior:
                raise SlackHeld('slack_membership_expansion_requires_enrollment')
        self.store.connection_policy(ctx,row['connection'],reader_ids=readers)
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE backend_slack_connectors SET status='ready',observed=%s,updated=%s WHERE id=%s", (self.clock(),self.clock(),row['id']))
        return set(members)

    def _message(self, row, message, *, complete=False):
        bindings = json.loads(row['bindings'])
        if not isinstance(message,dict) or not isinstance(message.get('text',''),str):
            raise SlackFailure('slack_message_invalid')
        stamp = str(message.get('ts','')); _timestamp(stamp)
        if message.get('files'):
            raise SlackHeld('slack_file_acl_unsupported')
        if message.get('subtype') not in {None,'thread_broadcast'}:
            raise SlackHeld('slack_message_subtype_unsupported')
        speaker = bindings.get(message.get('user'))
        if not speaker:
            raise SlackHeld('slack_unmapped_speaker')
        thread = str(message.get('thread_ts') or stamp); _timestamp(thread)
        changed = str(message.get('edited',{}).get('ts') or stamp); _timestamp(changed)
        # Preserve raw vendor speaker and timestamps as data alongside the
        # verified internal speaker. Text equality never deduplicates people.
        safe,manifest = redact([{'type':'text','value':message.get('text','')},
            {'type':'record_fields','value':{'vendor_user':message.get('user'),
                'message_ts':stamp,'thread_ts':thread,'edited_ts':changed}}])
        return {'id':row['channel']+':'+stamp,'stamp':stamp,'changed':changed,
            'thread':row['channel']+':'+thread,'speaker':speaker,'blocks':safe,
            'redactions':manifest,'complete':complete}

    def _upsert(self, ctx, row, item):
        with self._item_transaction(row['id'],item['id']) as db:
            return self._upsert_locked(ctx,row,item,db)

    @contextmanager
    def _item_transaction(self, ident, external_id):
        # Absent rows need the same serialization as existing rows. This lock is
        # held only for database/source installation, never a vendor HTTP call.
        key=int.from_bytes(hashlib.sha256(canonical([ident,external_id])).digest()[:8],'big',signed=True)
        with self.store.open() as state,state.db:
            state.db.execute('SELECT pg_advisory_xact_lock(%s)',(key,))
            yield state.db

    def _upsert_locked(self, ctx, row, item, db):
        from agenthub.enterprise import Conflict
        hashed = digest(item)
        old = db.execute('SELECT * FROM backend_slack_items WHERE connector=%s AND external_id=%s', (row['id'],item['id'])).fetchone()
        if old and _timestamp(item['changed']) < _timestamp(old['event_version']):
            return 'out_of_order'
        if old and not old['active']:
            return 'deleted'
        if old and _timestamp(item['changed']) == _timestamp(old['event_version']):
            if old['content_hash'] == hashed and old['active']:
                return 'duplicate'
            # Equal-version differing bodies are ambiguous, never last-write wins.
            if old['active']:
                raise Conflict()
            return 'deleted'
        revision = 1 if old is None else old['revision']+1
        event = normalize_capture({'hook_event_name':'UserPromptSubmit','event_id':item['id'],
            'session_id':item['thread'],'turn_id':item['thread'],'revision':str(revision),
            'speaker':item['speaker'],'timestamp':datetime.fromtimestamp(float(_timestamp(item['stamp'])),timezone.utc).isoformat(),
            'source_order':int(_timestamp(item['stamp'])*1000000),
            'source_url':'slack://'+row['team']+'/'+row['channel']+'/'+item['stamp']},
            row['project'],row['connection'],source_type='conversation',origin='slack_connector')
        event['blocks']=item['blocks']; event['redactions']=item['redactions']
        event['disposition']='redacted' if item['redactions'] else 'accepted'
        result = self.store.ingest_general(ctx,event)
        db.execute('''INSERT INTO backend_slack_items VALUES(%s,%s,%s,%s,%s,%s,1)
                ON CONFLICT(connector,external_id) DO UPDATE SET content_hash=excluded.content_hash,
                revision=excluded.revision,source_id=excluded.source_id,event_version=excluded.event_version,active=1''',
                (row['id'],item['id'],hashed,revision,result['source_id'],item['changed']))
        return 'upserted'

    def _complete(self, ctx, row, items):
        by_thread = {}
        for item in items:
            by_thread.setdefault(item['thread'],[]).append(item)
        for thread,messages in by_thread.items():
            external = thread + ':complete'
            checkpoint = digest(sorted([(m['id'],m['changed']) for m in messages]))
            with self.store.open() as state:
                old = state.db.execute('SELECT * FROM backend_slack_items WHERE connector=%s AND external_id=%s',(row['id'],external)).fetchone()
            if old and old['content_hash']==checkpoint:
                continue
            revision = 1 if old is None else old['revision']+1
            value = normalize_capture({'hook_event_name':'Stop','event_id':external,
                'session_id':thread,'turn_id':thread,'revision':str(revision),
                'timestamp':'unknown','source_order':max(int(_timestamp(m['stamp'])*1000000) for m in messages)+1},
                row['project'],row['connection'],source_type='conversation',origin='slack_connector')
            value['event']['role']='source';value['blocks']=[]
            result = self.store.ingest_general(ctx,value)
            with self.store.open() as state,state.db:
                state.db.execute('''INSERT INTO backend_slack_items VALUES(%s,%s,%s,%s,%s,%s,1)
                    ON CONFLICT(connector,external_id) DO UPDATE SET content_hash=excluded.content_hash,
                    revision=excluded.revision,source_id=excluded.source_id,event_version=excluded.event_version''',
                    (row['id'],external,checkpoint,revision,result['source_id'],max(m['changed'] for m in messages)))

    def sync(self, ctx, ident, *, dry_run=False, max_messages=1000, max_pages=30):
        if not 1<=max_messages<=10000 or not 1<=max_pages<=100:
            raise ValueError('slack_sync_bound')
        row = self._row(ctx,ident)
        try:
            if dry_run:
                # Dry-run never mutates membership, capture or checkpoints.
                members = self._pages(ident,'conversations.members','members',channel=row['channel'],max_pages=max_pages)
                if any(m not in json.loads(row['bindings']) for m in members):
                    raise SlackHeld('slack_unknown_readership')
            else:
                self._members(ctx,row)
            messages = self._pages(ident,'conversations.history','messages',channel=row['channel'],
                oldest=row['cursor'],inclusive='true',max_pages=max_pages,max_items=max_messages)
            collected = {str(m.get('ts')):m for m in messages}
            for message in messages:
                if message.get('reply_count',0):
                    replies = self._pages(ident,'conversations.replies','messages',channel=row['channel'],
                        ts=message['ts'],max_pages=max_pages,max_items=max_messages)
                    for reply in replies:
                        collected[str(reply.get('ts'))]=reply
            if len(collected)>max_messages:
                raise SlackFailure('slack_snapshot_bound')
            items = [self._message(row,message) for message in collected.values()]
        except SlackHeld as error:
            if not dry_run:
                self._hold(row,str(error))
            raise
        if dry_run:
            return {'connector':ident,'messages':len(items),'dry_run':True,'deletions':0,'model_calls':0}
        try:
            results = [self._upsert(ctx,row,item) for item in sorted(items,key=lambda i:_timestamp(i['stamp']))]
            self._complete(ctx,row,items)
            cursor = str(max([_timestamp(row['cursor']),*[_timestamp(i['stamp']) for i in items]]))
            with self.store.open() as state,state.db:
                state.db.execute('UPDATE backend_slack_connectors SET cursor=%s,updated=%s WHERE id=%s',(cursor,self.clock(),ident))
        except Exception:
            # A partial canonical capture must not advance the window or let
            # an observer consume it. Reconciliation and exact idempotent retry
            # refresh the current connection before any later dispatch.
            self._hold(row,'slack_capture_failed')
            raise SlackHeld('slack_capture_failed') from None
        return {'connector':ident,'messages':len(items),'upserts':results.count('upserted'),
            'duplicates':results.count('duplicate'),'deletions':0,'cursor':cursor,'model_calls':0}

    def event(self, ctx, ident, raw, headers, signing_secret):
        row = self._row(ctx,ident)
        value = verify_event(raw,headers,signing_secret,now=self.clock())
        if value.get('team_id') != row['team'] or value.get('type') != 'event_callback':
            raise SlackHeld('slack_event_scope')
        event_id = value.get('event_id'); event = value.get('event',{})
        if not isinstance(event_id,str) or not event_id or len(event_id)>256 or event.get('channel') != row['channel']:
            raise SlackHeld('slack_event_scope')
        hashed = hashlib.sha256(raw).hexdigest()
        with self.store.open() as state:
            old = state.db.execute('SELECT * FROM backend_slack_events WHERE connector=%s AND event_id=%s',(ident,event_id)).fetchone()
            connection = self.store._connection(state.db,ctx,row['connection'])
        if old:
            if old['digest'] != hashed:
                raise ValueError('slack_event_identity_conflict')
            if old['status']=='accepted':
                return {'disposition':'duplicate','model_calls':0}
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO backend_slack_events VALUES(%s,%s,%s,'processing',%s)
                ON CONFLICT(connector,event_id) DO NOTHING''',(ident,event_id,hashed,self.clock()))
            reserved=state.db.execute('SELECT digest FROM backend_slack_events WHERE connector=%s AND event_id=%s FOR UPDATE',(ident,event_id)).fetchone()
            if reserved['digest'] != hashed:
                raise ValueError('slack_event_identity_conflict')
        subtype = event.get('subtype')
        if event.get('type') != 'message' or subtype not in {None,'message_changed','message_deleted'}:
            raise SlackHeld('slack_event_capability')
        if subtype=='message_deleted':
            external = row['channel']+':'+str(event.get('deleted_ts',''));_timestamp(str(event.get('deleted_ts','')))
            changed = str(event.get('event_ts',''));_timestamp(changed)
            try:
                # Inventory, all revision purges and the tombstone serialize
                # with capture/edit for this exact channel/message. No HTTP or
                # model call occurs while the per-message lock is held.
                with self._item_transaction(ident,external) as db:
                    prior=db.execute('SELECT * FROM backend_slack_items WHERE connector=%s AND external_id=%s',(ident,external)).fetchone()
                    if prior and _timestamp(changed)<=_timestamp(prior['event_version']):
                        disposition='out_of_order'
                    elif prior:
                        self._delete_revisions(ctx,row,external,event_id,db)
                        db.execute('UPDATE backend_slack_items SET active=0,event_version=%s WHERE connector=%s AND external_id=%s',(changed,ident,external))
                        disposition='deleted'
                    else:
                        # A deletion before initial history remains a tombstone.
                        db.execute('''INSERT INTO backend_slack_items VALUES(%s,%s,%s,0,'',%s,0)
                            ON CONFLICT(connector,external_id) DO NOTHING''',
                            (ident,external,digest({'deleted':external}),changed))
                        disposition='deleted_before_capture'
            except Exception:
                self._hold(row,'slack_deletion_failed')
                raise SlackHeld('slack_deletion_failed') from None
        else:
            message = event.get('message') if subtype=='message_changed' else event
            if not isinstance(message,dict):
                raise SlackFailure('slack_message_invalid')
            if subtype=='message_changed':
                message = message | {'edited':{'ts':event.get('event_ts') or message.get('edited',{}).get('ts')}}
            item=self._message(row,message)
            disposition=self._upsert(ctx,row,item)
            if disposition=='upserted':
                self._complete(ctx,row,[item])
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO backend_slack_events VALUES(%s,%s,%s,'accepted',%s)
                ON CONFLICT(connector,event_id) DO UPDATE SET status='accepted' ''',(ident,event_id,hashed,self.clock()))
        return {'disposition':disposition,'model_calls':0}

    def _delete_revisions(self,ctx,row,external,event_id,db):
        revisions=db.execute('''SELECT r.source_id,r.payload,s.tenant,s.owner,s.source_version
            FROM backend_source_revisions r JOIN enterprise_sources s ON s.id=r.source_id
            WHERE r.connection=%s AND r.external_id=%s ORDER BY r.received,r.source_id LIMIT %s''',
            (row['connection'],external,self.DELETE_REVISION_LIMIT+1)).fetchall()
        if len(revisions)>self.DELETE_REVISION_LIMIT or not revisions:
            raise SlackHeld('slack_deletion_revision_bound')
        for revision in revisions:
            if revision['tenant']!=ctx['tenant'] or revision['owner']!=ctx['actor']:
                raise SlackHeld('slack_deletion_scope')
            payload=json.loads(revision['payload'])
            if payload and (payload.get('source_type')!='conversation' or
                    payload.get('connection')!=row['connection'] or payload.get('external_id')!=external):
                raise SlackHeld('slack_deletion_scope')
            if not payload and not db.execute("SELECT 1 FROM enterprise_deletion_journal WHERE source_id=%s AND operation='delete'",(revision['source_id'],)).fetchone():
                raise SlackHeld('slack_deletion_scope')
        deadline=time.monotonic()+self.DELETE_SECONDS
        for revision in revisions:
            if time.monotonic()>deadline:raise SlackHeld('slack_deletion_time_bound')
            self.store.lifecycle(ctx,{'version':'enterprise-local-1','operation':'delete',
                'target_id':revision['source_id'],'expected_revision':str(revision['source_version']),
                'idempotency_key':'slack:delete:'+digest([event_id,revision['source_id']]),
                'reason':'authenticated Slack message deletion'})

    def status(self, ctx, ident):
        row = self._row(ctx,ident)
        return {'connector':ident,'status':row['status'],'cursor':row['cursor'],
            'policy_fresh':row['status']=='ready' and row['observed']+row['freshness']>=self.clock(),
            'capabilities':self.CAPABILITIES,'model_calls':0}


def configured_store(config):
    """Explicit cloud registry routing, or the independent legacy PG-only mode."""
    from pathlib import Path
    from agenthub.postgres import PostgresEnterpriseStore
    if 'backend_profile' not in config:
        return PostgresEnterpriseStore(config['home'],config['dsn'],config['tenant'])
    from agenthub.cloud_local import runtime
    _,registry=runtime(Path(config['backend_profile']).expanduser().resolve(strict=True))
    store=registry.resolve(config['tenant'])
    if getattr(store,'conversation_segments',None) is None:
        raise ValueError('slack_cloud_original_objects_required')
    if 'dsn' in config:
        from psycopg.conninfo import conninfo_to_dict
        if conninfo_to_dict(config['dsn'])!=conninfo_to_dict(store.dsn):
            raise ValueError('slack_backend_route_conflict')
    if 'home' in config and Path(config['home']).expanduser().resolve()!=Path(store.home).resolve():
        raise ValueError('slack_backend_home_conflict')
    return store


def main(argv=None):
    """Explicit operator entry point; startup alone never synchronizes data."""
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Read-only explicitly enrolled Slack connector')
    parser.add_argument('--profile',required=True,help='Private connector configuration JSON')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('discover');commands.add_parser('enroll');commands.add_parser('status')
    sync=commands.add_parser('sync');sync.add_argument('--dry-run',action='store_true')
    sync.add_argument('--max-messages',type=int,default=100);sync.add_argument('--max-pages',type=int,default=5)
    args=parser.parse_args(argv)
    path=Path(args.profile).expanduser().resolve(strict=True)
    if path.stat().st_mode & 0o077:
        parser.error('private profile requires mode 0600')
    config=json.loads(path.read_text())
    store=configured_store(config)
    ctx=store.authenticate(config['enterprise_token'])
    http=SlackHTTP(config['slack_token'],endpoint=config['endpoint'],allow_vendor=config.get('allow_vendor',False))
    connector=SlackConnector(store,http)
    if args.command=='discover':result=connector.discover()
    elif args.command=='enroll':result=connector.enroll(ctx,config['connector'],config['team'],
        config['channel'],config['project'],config['bindings'],freshness=config.get('freshness',3600))
    elif args.command=='status':result=connector.status(ctx,config['connector'])
    else:result=connector.sync(ctx,config['connector'],dry_run=args.dry_run,
        max_messages=args.max_messages,max_pages=args.max_pages)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
