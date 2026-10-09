from copy import deepcopy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from urllib.parse import parse_qs,urlsplit

from agenthub.slack_connector import (SlackConnector,SlackHTTP,SlackFailure,
    SlackHeld,SlackRateLimited,verify_event)

FIXTURE=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_objects_v1.json').read_text())['slack']


class SlackServer:
    def __init__(self):
        self.history=deepcopy(FIXTURE['history_pages']);self.members=list(FIXTURE['members'])
        self.channel={'id':FIXTURE['channel'],'name':'maple','is_private':True,'is_im':False,'is_mpim':False,
            'is_shared':False,'is_ext_shared':False,'is_org_shared':False,'is_archived':False}
        self.replies=deepcopy(FIXTURE['replies']);self.calls=[];self.rate={};self.fail_page=False
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                url=urlsplit(self.path);method=url.path.rsplit('/',1)[1];params=parse_qs(url.query)
                fixture.calls.append((method,params))
                if self.headers.get('Authorization')!='Bearer synthetic-token':
                    self.send_response(403);self.end_headers();return
                if method in fixture.rate:
                    self.send_response(429);self.send_header('Retry-After',str(fixture.rate[method]));self.end_headers();return
                cursor=params.get('cursor',[''])[0]
                if method=='auth.test':value={'ok':True,'team_id':FIXTURE['team']}
                elif method=='conversations.info':value={'ok':True,'channel':fixture.channel}
                elif method=='conversations.list':value={'ok':True,'channels':[fixture.channel],'response_metadata':{'next_cursor':''}}
                elif method=='conversations.members':
                    value={'ok':True,'members':fixture.members[:1] if not cursor else fixture.members[1:],
                        'response_metadata':{'next_cursor':'members2' if not cursor and len(fixture.members)>1 else ''}}
                elif method=='conversations.history':
                    value=fixture.history[0 if not cursor else 1]
                    if cursor and fixture.fail_page:value={'ok':False,'error':'fixture_failure'}
                elif method=='conversations.replies':
                    value={'ok':True,'messages':fixture.replies[:1] if not cursor else fixture.replies[1:],
                        'response_metadata':{'next_cursor':'replies2' if not cursor else ''}}
                else:value={'ok':False,'error':'unhandled'}
                raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.endpoint=f'http://127.0.0.1:{self.server.server_port}/api/'

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()


class SlackHTTPTests(unittest.TestCase):
    def setUp(self):self.server=SlackServer();self.http=SlackHTTP('synthetic-token',endpoint=self.server.endpoint)
    def tearDown(self):self.server.close()

    def test_actual_http_response_shapes_auth_and_readonly(self):
        self.assertEqual(self.http.call('auth.test')['team_id'],FIXTURE['team'])
        self.assertEqual(self.http.call('conversations.history',channel=FIXTURE['channel'])['messages'],FIXTURE['history_pages'][0]['messages'])
        with self.assertRaises(ValueError):self.http.call('chat.postMessage')
        with self.assertRaises(ValueError):SlackHTTP('token')
        with self.assertRaises(ValueError):SlackHTTP('token',endpoint='http://external.example/api/')

    def test_rate_limit_is_bounded_deadline_without_sleep(self):
        self.server.rate['conversations.history']=123
        start=time.time()
        with self.assertRaises(SlackRateLimited) as caught:self.http.call('conversations.history')
        self.assertEqual(caught.exception.method,'conversations.history')
        self.assertGreaterEqual(caught.exception.retry_at,start+123)
        self.assertLess(time.time()-start,1)

    def test_signature_authenticates_exact_bytes_and_expires_replay(self):
        raw=json.dumps({'type':'event_callback','event_id':'E1'}).encode();secret='synthetic-secret';when=int(time.time())
        signature='v0='+hmac.new(secret.encode(),b'v0:'+str(when).encode()+b':'+raw,hashlib.sha256).hexdigest()
        headers={'X-Slack-Request-Timestamp':str(when),'X-Slack-Signature':signature}
        self.assertEqual(verify_event(raw,headers,secret)['event_id'],'E1')
        with self.assertRaises(ValueError):verify_event(raw+b' ',headers,secret)
        with self.assertRaises(ValueError):verify_event(raw,headers,secret,now=when+301)


@unittest.skipUnless(os.environ.get('CLOUD_TEST_DSN'),'real PostgreSQL fixture DSN required')
class SlackPostgresTests(unittest.TestCase):
    def setUp(self):
        from agenthub.postgres import PostgresEnterpriseStore
        self.temp=tempfile.TemporaryDirectory();suffix=uuid.uuid4().hex[:10]
        self.tenant=os.environ.get('CLOUD_TEST_TENANT','acme')
        self.store=PostgresEnterpriseStore(Path(self.temp.name),os.environ['CLOUD_TEST_DSN'],self.tenant)
        self.store.create_organization(self.tenant);self.project='slack-'+suffix
        self.store.create_project(self.tenant,self.project)
        self.alice='alice-'+suffix;self.bob='bob-'+suffix
        for principal in (self.alice,self.bob):
            self.store.create_principal(self.tenant,principal);self.store.set_membership(self.tenant,self.project,principal,True)
        token=self.store.enroll(self.tenant,self.alice,'slack-'+suffix,['ingest','read','source_read','policy','withdraw','correct'])
        self.ctx=self.store.authenticate(token);self.ident='fixture-'+suffix
        self.server=SlackServer();self.http=SlackHTTP('synthetic-token',endpoint=self.server.endpoint)
        self.connector=SlackConnector(self.store,self.http)
        self.bindings={'U_ALICE':self.alice,'U_BOB':self.bob}
        self.connector.enroll(self.ctx,self.ident,FIXTURE['team'],FIXTURE['channel'],self.project,self.bindings)

    def tearDown(self):self.server.close();self.temp.cleanup()

    def event(self,event,event_id='E1',secret='synthetic-secret'):
        value={'type':'event_callback','team_id':FIXTURE['team'],'event_id':event_id,'event':event}
        raw=json.dumps(value,sort_keys=True).encode();when=int(time.time())
        signature='v0='+hmac.new(secret.encode(),b'v0:'+str(when).encode()+b':'+raw,hashlib.sha256).hexdigest()
        return self.connector.event(self.ctx,self.ident,raw,{'X-Slack-Request-Timestamp':str(when),'X-Slack-Signature':signature},secret)

    def test_pagination_equal_text_speakers_restart_and_raw_not_searchable(self):
        dry=self.connector.sync(self.ctx,self.ident,dry_run=True);self.assertEqual(dry['messages'],4)
        self.assertFalse(self.connector.status(self.ctx,self.ident)['policy_fresh'])
        report=self.connector.sync(self.ctx,self.ident);self.assertEqual(report['upserts'],4)
        self.assertEqual(report['deletions'],0);self.assertEqual(report['model_calls'],0)
        restarted=SlackConnector(self.store,self.http)
        second=restarted.sync(self.ctx,self.ident);self.assertEqual(second['upserts'],0);self.assertEqual(second['duplicates'],4)
        with self.store.open() as state:
            rows=state.db.execute('SELECT payload FROM backend_source_revisions WHERE connection=%s',(self.connector._row(self.ctx,self.ident)['connection'],)).fetchall()
            events=[json.loads(row['payload']) for row in rows]
            identical=[e for e in events if any(b.get('value')=='Use TSV because headers stay stable.' for b in e['blocks'])]
            self.assertEqual(len(identical),2);self.assertEqual({e['actor'] for e in identical},{self.alice,self.bob})
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_native_artifacts n JOIN enterprise_sources s ON s.id=n.source_id WHERE s.external_project=%s',(self.project,)).fetchone()[0],0)
        methods=[method for method,_ in self.server.calls]
        self.assertIn('conversations.replies',methods)
        self.assertFalse(list(Path(self.temp.name).rglob('*.sqlite')))

    def test_edit_delete_duplicate_and_out_of_order(self):
        self.connector.sync(self.ctx,self.ident)
        stamp='1000.000001';event={'type':'message','subtype':'message_changed','channel':FIXTURE['channel'],
            'event_ts':'1100.000001','message':{'user':'U_ALICE','text':'Correction: JSON is required.','ts':stamp}}
        self.assertEqual(self.event(event)['disposition'],'upserted')
        self.assertEqual(self.event(event)['disposition'],'duplicate')
        stale=event|{'event_ts':'1050.000001'}
        self.assertEqual(self.event(stale,'E2')['disposition'],'out_of_order')
        deletion={'type':'message','subtype':'message_deleted','channel':FIXTURE['channel'],'deleted_ts':stamp,'event_ts':'1200.000001'}
        self.assertEqual(self.event(deletion,'E3')['disposition'],'deleted')
        self.assertEqual(self.event(event,'E4')['disposition'],'out_of_order')
        with self.store.open() as state:
            row=state.db.execute('SELECT * FROM backend_slack_items WHERE connector=%s AND external_id=%s',(self.ident,FIXTURE['channel']+':'+stamp)).fetchone()
            self.assertEqual(row['active'],0);self.assertEqual(row['revision'],2)

    def test_partial_page_never_deletes_or_advances(self):
        first=self.connector.sync(self.ctx,self.ident)
        self.server.fail_page=True
        with self.assertRaises(SlackFailure):self.connector.sync(self.ctx,self.ident)
        self.assertEqual(self.connector.status(self.ctx,self.ident)['cursor'],first['cursor'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM backend_slack_items WHERE connector=%s AND active=0',(self.ident,)).fetchone()[0],0)

    def test_deletion_before_initial_history_is_durable_tombstone(self):
        # Frozen C0 out-of-order/delete invariant, no new provider fixture.
        self.connector.sync(self.ctx,self.ident,dry_run=True)
        row=self.connector._row(self.ctx,self.ident);self.connector._members(self.ctx,row)
        deletion={'type':'message','subtype':'message_deleted','channel':FIXTURE['channel'],
            'deleted_ts':'1000.000001','event_ts':'1200.000001'}
        self.assertEqual(self.event(deletion,'early-delete')['disposition'],'deleted_before_capture')
        self.connector.sync(self.ctx,self.ident)
        with self.store.open() as state:
            item=state.db.execute('SELECT active,source_id FROM backend_slack_items WHERE connector=%s AND external_id=%s',
                (self.ident,FIXTURE['channel']+':1000.000001')).fetchone()
            self.assertEqual(item['active'],0);self.assertEqual(item['source_id'],'')

    def test_unknown_member_stale_acl_reconciles_and_membership_narrows(self):
        self.connector.sync(self.ctx,self.ident);self.server.members.append('U_UNKNOWN')
        with self.assertRaises(SlackHeld):self.connector.sync(self.ctx,self.ident)
        self.assertEqual(self.connector.status(self.ctx,self.ident)['status'],'held')
        self.assertFalse(self.connector.status(self.ctx,self.ident)['policy_fresh'])
        self.server.members.remove('U_UNKNOWN');self.connector.sync(self.ctx,self.ident)
        self.assertTrue(self.connector.status(self.ctx,self.ident)['policy_fresh'])
        self.server.members.remove('U_BOB');self.connector.sync(self.ctx,self.ident)
        with self.store.open() as state:
            connection=self.store._connection(state.db,self.ctx,'slack-'+self.ident)
            self.assertEqual(json.loads(connection['reader_ids']),[self.alice])
        self.server.members.append('U_BOB')
        with self.assertRaises(SlackHeld):self.connector.sync(self.ctx,self.ident)

    def test_excluded_channel_attachments_and_rate_limit_survive_restart(self):
        self.server.channel['is_ext_shared']=True
        with self.assertRaises(SlackHeld):self.connector.sync(self.ctx,self.ident)
        self.server.channel['is_ext_shared']=False;self.connector.sync(self.ctx,self.ident)
        self.server.rate['conversations.history']=60
        with self.assertRaises(SlackRateLimited):self.connector.sync(self.ctx,self.ident)
        count=len(self.server.calls);restarted=SlackConnector(self.store,self.http)
        with self.assertRaises(SlackRateLimited):restarted.sync(self.ctx,self.ident)
        # Membership may refresh; the specifically rate-limited method is skipped.
        self.assertEqual(len([m for m,_ in self.server.calls[count:] if m=='conversations.history']),0)
        self.assertTrue(self.http.call('conversations.info',channel=FIXTURE['channel'])['ok'])
        message={'type':'message','channel':FIXTURE['channel'],'user':'U_ALICE','ts':'1300.000001','text':'file','files':[{'id':'F1'}]}
        with self.assertRaises(SlackHeld):self.event(message,'E5')


if __name__=='__main__':unittest.main()
