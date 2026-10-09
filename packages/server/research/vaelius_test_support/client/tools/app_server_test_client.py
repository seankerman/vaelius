"""Small stdio client for bounded tests of an owned Codex app-server process."""
import json
import os
import queue
import signal
import subprocess
import threading
import time


class AppServer:
    def __init__(self, args, cwd, stderr, env):
        self.process=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=stderr,text=True,bufsize=1,start_new_session=True)
        self.inbox=queue.Queue();self.events=[];self.sequence=0;self.owned=set()
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:message=json.loads(line)
                except ValueError:continue
                # Never retain reasoning, token deltas or unrelated app notifications.
                if 'id' in message:
                    self.inbox.put(message)
                elif message.get('method') in ('turn/started','turn/completed','item/started','item/completed','thread/tokenUsage/updated'):
                    params=message.get('params',{});item=params.get('item',{})
                    if message['method'].startswith('item/') and item.get('type') not in ('commandExecution','agentMessage','contextCompaction'):
                        continue
                    if message['method'].startswith('turn/'):
                        # Turn envelopes may include full items; keep lifecycle metadata only.
                        turn=params.get('turn',{})
                        message={'method':message['method'],'params':{'threadId':params.get('threadId'),
                            'turn':{k:turn.get(k) for k in ('id','status')}}}
                    self.inbox.put(message)
        finally:self.inbox.put(None)

    def send(self, value):
        self.process.stdin.write(json.dumps(value)+'\n');self.process.stdin.flush()

    def receive(self, timeout):
        try:message=self.inbox.get(timeout=max(.001,timeout))
        except queue.Empty:raise TimeoutError('app_server_response_timeout')
        if message is None:raise RuntimeError('app_server_closed')
        if 'method' in message and 'id' in message:
            self.send({'id':message['id'],'error':{'code':-32601,'message':'Interactive requests are unavailable in this bounded test'}})
            raise RuntimeError('unexpected_interactive_request:'+message['method'])
        if 'method' in message and message.get('params',{}).get('threadId') in self.owned:
            self.events.append(message)
        return message

    def request(self, method, params, timeout=20):
        self.sequence+=1;ident=self.sequence
        self.send({'id':ident,'method':method,'params':params});deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            reply=self.receive(deadline-time.monotonic())
            if reply.get('id')!=ident:continue
            if 'error' in reply:raise RuntimeError('app_server_rpc_error:'+method+':'+str(reply['error'].get('code')))
            return reply['result']
        raise TimeoutError(method)

    def initialize(self):
        self.request('initialize',{'clientInfo':{'name':'agentnetwork_lifecycle_test','version':'0.1'},
            'capabilities':{'experimentalApi':True}})
        self.send({'method':'initialized','params':{}})

    def until(self, predicate, timeout=90):
        deadline=time.monotonic()+timeout
        while True:
            result=predicate(self.events)
            if result:return result
            if time.monotonic()>=deadline:raise TimeoutError('app_server_event_timeout')
            self.receive(deadline-time.monotonic())

    def interrupt(self, thread, turn):
        if thread not in self.owned:raise ValueError('test_does_not_own_thread')
        return self.request('turn/interrupt',{'threadId':thread,'turnId':turn})

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGTERM)
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdin.close();self.process.stdout.close()
