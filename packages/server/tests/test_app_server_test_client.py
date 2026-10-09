import os
from pathlib import Path
import sys
import tempfile
import unittest

from vaelius_test_support.client.tools.app_server_test_client import AppServer


FAKE='''import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r:continue
 if r['method']=='hang':continue
 if r['method']=='probe':
  for kind in ['reasoning','commandExecution']:
   print(json.dumps({'method':'item/completed','params':{'threadId':'owned','turnId':'turn','item':{'type':kind,'id':kind}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':'other','turn':{'id':'other'}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':'owned','turn':{'id':'turn','status':'completed','items':[{'type':'reasoning','text':'CANARY'}]}}}),flush=True)
 print(json.dumps({'id':r['id'],'result':{}}),flush=True)
'''


class AppServerClientTests(unittest.TestCase):
    def test_filters_reasoning_and_unowned_events_and_bounds_requests(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryFile(mode='w+') as log:
            server=AppServer([sys.executable,'-u','-c',FAKE],Path(temp),log,os.environ.copy())
            try:
                server.initialize();server.owned.add('owned')
                server.request('probe',{})
                self.assertEqual(len(server.events),2)
                self.assertEqual(server.events[0]['params']['item']['type'],'commandExecution')
                self.assertNotIn('CANARY',str(server.events))
                with self.assertRaises(ValueError):server.interrupt('other','turn')
                with self.assertRaises(TimeoutError):server.request('hang',{},timeout=.05)
            finally:server.close()
            self.assertIsNotNone(server.process.poll())


if __name__=='__main__':unittest.main()
