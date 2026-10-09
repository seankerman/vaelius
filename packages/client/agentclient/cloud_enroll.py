"""Explicit, model-free enrollment into a new local enterprise client profile.

The server owns issuer verification, PKCE verifier storage and credential scope.
This client binds the exact configured loopback callback, checks state, and stores
only the resulting short-lived credential. It never installs global hooks.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from urllib.parse import parse_qs,urlsplit
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
import webbrowser


class EnrollmentError(ValueError):
    pass


def _callback_uri(value,*,allow_zero=False):
    parsed=urlsplit(value)
    try:port=parsed.port
    except ValueError:raise EnrollmentError('invalid_callback_uri') from None
    if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username
            or parsed.password or parsed.path!='/callback' or parsed.query or parsed.fragment
            or port is None or (port==0 and not allow_zero)):
        raise EnrollmentError('exact_loopback_callback_required')
    if value!=f'http://127.0.0.1:{port}/callback':raise EnrollmentError('exact_loopback_callback_required')
    return port


class CallbackServer:
    """One bounded callback; unexpected requests never consume the expected state."""
    def __init__(self,uri):
        port=_callback_uri(uri,allow_zero=True)
        self.state=None;self.result=queue.Queue(maxsize=1);self.used=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup();self.connection.settimeout(2)
            def log_message(self,*args):pass
            def do_GET(self):
                status=400
                try:
                    parsed=urlsplit(self.path);args=parse_qs(parsed.query,strict_parsing=True)
                    valid=(self.headers.get('Host')==urlsplit(owner.uri).netloc and parsed.path=='/callback'
                        and not parsed.scheme and not parsed.netloc and not parsed.fragment
                        and len(self.path)<=8192 and set(args)=={'state','code'}
                        and len(args['state'])==len(args['code'])==1 and owner.state is not None
                        and hmac.compare_digest(args['state'][0],owner.state)
                        and 0<len(args['code'][0])<=4096)
                    if valid:
                        if owner.used:status=409
                        else:
                            owner.used=True;owner.result.put_nowait({'state':args['state'][0],'code':args['code'][0]})
                            status=200
                except (ValueError,KeyError,queue.Full):pass
                body=b'Enrollment received. You may close this tab.' if status==200 else b'Callback refused.'
                self.send_response(status);self.send_header('Content-Type','text/plain; charset=utf-8')
                self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        try:self.server=HTTPServer(('127.0.0.1',port),Handler)
        except OSError:raise EnrollmentError('callback_port_unavailable') from None
        self.uri=f'http://127.0.0.1:{self.server.server_port}/callback'
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
    def arm(self,state):
        if not isinstance(state,str) or not 1<=len(state)<=512 or self.state is not None:
            raise EnrollmentError('invalid_authorization_state')
        self.state=state
    def wait(self,timeout):
        try:return self.result.get(timeout=timeout)
        except queue.Empty:raise EnrollmentError('enrollment_callback_timeout') from None


def validate_authorization(value,callback_uri):
    _callback_uri(callback_uri)
    if not isinstance(value,dict):raise EnrollmentError('invalid_authorization_response')
    state=value.get('state');expires=value.get('expires_at');url=value.get('url')
    if (not isinstance(state,str) or not 1<=len(state)<=512 or not isinstance(expires,(float,int))
            or not time.time()<expires<=time.time()+360 or not isinstance(url,str) or len(url)>8192):
        raise EnrollmentError('invalid_authorization_response')
    parsed=urlsplit(url)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or (parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'))):
        raise EnrollmentError('invalid_authorization_url')
    args=parse_qs(parsed.query)
    expected={'state':state,'redirect_uri':callback_uri,'response_type':'code','code_challenge_method':'S256'}
    if any(args.get(key)!=[val] for key,val in expected.items()):raise EnrollmentError('authorization_callback_or_pkce_mismatch')
    challenge=args.get('code_challenge',[])
    if len(challenge)!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}',challenge[0]):
        raise EnrollmentError('authorization_pkce_required')
    return value


def prepare_home(value):
    path=Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():raise EnrollmentError('new_explicit_isolated_home_required')
    path=path.resolve()
    founder=Path.home()/'.local/share/agentnetwork'
    if path in (founder,founder/'client'):raise EnrollmentError('founder_profile_must_not_change')
    if path.exists():
        if not path.is_dir() or path.stat().st_uid!=os.getuid() or any(path.iterdir()):
            raise EnrollmentError('new_explicit_isolated_home_required')
    else:path.mkdir(parents=True,mode=0o700)
    path.chmod(0o700)
    return path


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def local_request(url):
    from agentclient.transport import LoopbackTransport
    try:hub=LoopbackTransport(url,'unused',timeout=8)
    except ValueError:raise EnrollmentError('local_enterprise_endpoint_required') from None
    opener=build_opener(ProxyHandler({}),_NoRedirect())
    def request(path,value):
        body=json.dumps(value).encode()
        try:
            with opener.open(Request(hub.url+path,data=body,headers={'Content-Type':'application/json'},method='POST'),timeout=8) as response:
                raw=response.read(262145)
                if len(raw)>262144:raise EnrollmentError('enrollment_response_too_large')
                return json.loads(raw)
        except EnrollmentError:raise
        except Exception:raise EnrollmentError('enrollment_server_request_failed') from None
    return request


def enroll(home,*,url,tenant,broker,enrollment,callback,request=None,browser=None,timeout=180,notify=None):
    """No source/project enrollment is implicit in login; the new profile is empty."""
    request=request or local_request(url)
    for identifier in (tenant,broker,enrollment):
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',identifier):
            raise EnrollmentError('explicit_enrollment_identifiers_required')
    if not 0<timeout<=300:raise EnrollmentError('bounded_callback_timeout_required')
    profile=prepare_home(home)
    begin=validate_authorization(request('/enterprise/v3/auth/begin',{'tenant':tenant,'broker':broker}),callback.uri)
    callback.arm(begin['state'])
    if notify:notify({'authorization_url':begin['url'],'callback_uri':callback.uri})
    if browser is not None and not browser(begin['url']):raise EnrollmentError('browser_launch_failed')
    result=callback.wait(min(timeout,max(.001,begin['expires_at']-time.time())))
    if begin['expires_at']<=time.time():raise EnrollmentError('authorization_expired')
    response=request('/enterprise/v3/auth/complete',dict(result,tenant=tenant,broker=broker,
        callback_uri=callback.uri,enrollment=enrollment))
    token=response.get('credential') if isinstance(response,dict) else None
    if not isinstance(token,str) or not 1<=len(token)<=256 or '\n' in token or '\r' in token:
        raise EnrollmentError('invalid_credential_response')
    credential=profile/'credential';config_file=profile/'config.json'
    config={'paused':False,'projects':{},'sessions':{},'desktop_transcripts':{},'capture_all_codex_sessions':False,
        'observer':{'enabled':False},'publication':{'enabled':False},'remote_publication':False,
        'knowledge_backend':{'mode':'enterprise_local','url':url,'credential_file':str(credential),
            'capture_version':'enterprise-local-2','capture_owner':'transcript','api_version':'cloud-local-1',
            'tenant':tenant,'broker':broker,'enrollment_id':enrollment}}
    # Exclusive private writes protect against a second enrollment process and
    # symlink replacement; a partial profile is never a silently activated one.
    try:
        for path,content in ((credential,token+'\n'),(config_file,json.dumps(config,indent=2)+'\n')):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
    except OSError:raise EnrollmentError('private_profile_write_failed') from None
    return {'profile':str(profile),'credential_stored':True,'projects_enrolled':0,'client_model_calls':0,
            'local_corpus_opened':False,'host_hooks_installed':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Enroll a new isolated local enterprise client profile')
    for name in ('home','url','tenant','broker','enrollment','callback-uri'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--timeout',type=float,default=180);parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    try:
        _callback_uri(args.callback_uri)
        with CallbackServer(args.callback_uri) as callback:
            result=enroll(args.home,url=args.url,tenant=args.tenant,broker=args.broker,enrollment=args.enrollment,
                callback=callback,timeout=args.timeout,browser=None if args.no_browser else webbrowser.open,
                notify=lambda value:print(json.dumps(value),flush=True))
        print(json.dumps(result),flush=True);return 0
    except EnrollmentError as error:
        print(json.dumps({'error':str(error)}),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
