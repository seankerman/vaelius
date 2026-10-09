"""Frozen local callback failures and isolated, provider-free enrollment."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlencode
from urllib.request import urlopen,Request
from urllib.error import HTTPError


class CloudEnrollmentTests(unittest.TestCase):
    def callback(self):
        from agentclient.cloud_enroll import CallbackServer
        return CallbackServer('http://127.0.0.1:0/callback')

    def send(self,url):
        try:
            with urlopen(url,timeout=2) as response:return response.status
        except HTTPError as error:return error.code

    def test_wrong_duplicate_callback_and_replay(self):
        with self.callback() as callback:
            callback.arm('expected')
            base=callback.uri
            self.assertEqual(self.send(base+'?state=wrong&code=c'),400)
            self.assertEqual(self.send(base+'?state=expected&state=expected&code=c'),400)
            self.assertEqual(self.send(base+'?state=expected&code=a&code=b'),400)
            self.assertEqual(self.send(base.replace('/callback','/wrong')+'?state=expected&code=c'),400)
            self.assertEqual(self.send(Request(base+'?state=expected&code=c',headers={'Host':'malicious.example'})),400)
            self.assertEqual(self.send(base+'?state=expected&code=accepted'),200)
            self.assertEqual(callback.wait(.5),{'state':'expected','code':'accepted'})
            self.assertEqual(self.send(base+'?state=expected&code=accepted'),409)

    def test_authorization_requires_exact_callback_pkce_and_not_expired(self):
        from agentclient.cloud_enroll import validate_authorization,EnrollmentError
        state='s';callback='http://127.0.0.1:59001/callback'
        value={'state':state,'expires_at':time.time()+20,'url':'http://127.0.0.1:4444/auth?'+urlencode({
            'state':state,'redirect_uri':callback,'response_type':'code','code_challenge_method':'S256',
            'code_challenge':'a'*43})}
        validate_authorization(value,callback)
        for name,extra in [('pkce',{'code_challenge_method':'plain'}),('callback',{'redirect_uri':callback+'/other'}),
                           ('response',{'response_type':'token'}),('state',{'state':'other'})]:
            with self.subTest(name=name):
                changed=dict(value);changed['url']=value['url']+'&'+urlencode(extra)
                with self.assertRaises(EnrollmentError):validate_authorization(changed,callback)
        with self.assertRaises(EnrollmentError):validate_authorization(dict(value,expires_at=0),callback)

    def test_new_home_only_and_no_symlinks_or_founder(self):
        from agentclient.cloud_enroll import prepare_home,EnrollmentError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fresh=prepare_home(root/'fresh');self.assertEqual(fresh.stat().st_mode&0o777,0o700)
            (fresh/'config.json').write_text('{}')
            with self.assertRaises(EnrollmentError):prepare_home(fresh)
            (root/'link').symlink_to(root/'fresh',target_is_directory=True)
            with self.assertRaises(EnrollmentError):prepare_home(root/'link')
        with self.assertRaises(EnrollmentError):prepare_home(Path.home()/'.local/share/agentnetwork/client')

    def test_success_and_failure_are_model_free_and_private(self):
        from agentclient.cloud_enroll import enroll,EnrollmentError
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def request(path,value):
                calls.append((path,value))
                if path.endswith('begin'):
                    callback_uri=value['callback_uri'] if 'callback_uri' in value else callback.uri
                    return {'state':'expected','expires_at':time.time()+10,'url':'http://127.0.0.1:4444/auth?'+urlencode({
                        'state':'expected','redirect_uri':callback_uri,'response_type':'code',
                        'code_challenge_method':'S256','code_challenge':'a'*43})}
                return {'credential':'synthetic-token'}
            def browser(url):
                self.assertEqual(self.send(callback.uri+'?state=expected&code=synthetic-code'),200)
                return True
            with self.callback() as callback:
                result=enroll(Path(directory)/'profile',url='http://127.0.0.1:8888',tenant='orchard',
                    broker='fixture',enrollment='plugin',callback=callback,request=request,browser=browser,timeout=2)
            profile=Path(result['profile']);config=json.loads((profile/'config.json').read_text())
            self.assertEqual(config['knowledge_backend']['api_version'],'cloud-local-1')
            self.assertFalse(config['publication']['enabled']);self.assertFalse(config['observer']['enabled'])
            self.assertEqual(config['projects'],{});self.assertEqual(result['client_model_calls'],0)
            self.assertEqual((profile/'credential').stat().st_mode&0o777,0o600)
            self.assertEqual((profile/'config.json').stat().st_mode&0o777,0o600)
            self.assertFalse((profile/'knowledge.sqlite').exists())
            self.assertEqual(calls[-1][1]['callback_uri'],callback.uri)
            with self.callback() as callback:
                with self.assertRaises(EnrollmentError):
                    enroll(Path(directory)/'failed',url='http://127.0.0.1:8888',tenant='orchard',broker='fixture',
                        enrollment='plugin',callback=callback,request=request,browser=lambda _:False,timeout=.1)
            self.assertFalse((Path(directory)/'failed'/'credential').exists())

    def test_callback_timeout_has_no_result(self):
        from agentclient.cloud_enroll import EnrollmentError
        with self.callback() as callback:
            callback.arm('expected')
            with self.assertRaises(EnrollmentError):callback.wait(.05)

    def test_remote_endpoint_remains_disabled_and_no_default_profile(self):
        from agentclient.cloud_enroll import local_request,EnrollmentError,main
        with self.assertRaises(EnrollmentError):local_request('https://example.invalid')
        with self.assertRaises(SystemExit) as failure:main([])
        self.assertEqual(failure.exception.code,2)


if __name__=='__main__':unittest.main()
