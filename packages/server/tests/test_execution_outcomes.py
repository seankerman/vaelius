"""Frozen provider outcome/usage cases; transport never leaves this process."""
import unittest
from unittest.mock import patch
from agenthub.cloud_execution import ApiExecution, CodexExecution, ExecutionError

SCHEMA={'type':'object','properties':{'items':{'type':'array','maxItems':1,
    'items':{'type':'string','maxLength':4}}},'required':['items'],'additionalProperties':False}
USAGE={'input_tokens':30,'output_tokens':7,'input_tokens_details':{'cached_tokens':20}}

def api(value):
    return ApiExecution(model='fixture',api_key='synthetic',send=lambda *_:value)

class ExecutionOutcomes(unittest.TestCase):
    def test_refusal_incomplete_and_invalid_return_keep_usage(self):
        for reply in [dict(status='completed',output=[None]),dict(status='completed',output=[{'type':'message','content':[None]}]),dict(status='incomplete',output=[]),dict(status='completed',output=[
            {'type':'message','content':[{'type':'refusal'}]}]),dict(status='completed',output=[
            {'type':'message','content':[{'type':'output_text','text':'not json'}]}])]:
            with self.subTest(reply=reply),self.assertRaises(ExecutionError) as caught:
                api(dict(reply,usage=USAGE))('.',{},'stable',{},SCHEMA)
            self.assertEqual(caught.exception.outcome,'returned')
            self.assertEqual(caught.exception.usage,{'input_tokens':30,'output_tokens':7,'cached_input_tokens':20})

    def test_schema_is_enforced_for_both_live_adapters(self):
        import json
        for raw in ({'items':['longer']},{'items':['one','two']},{'items':[],'extra':'no'}):
            reply={'status':'completed','usage':USAGE,'output':[{'type':'message','content':[
                {'type':'output_text','text':json.dumps(raw)}]}]}
            with self.assertRaisesRegex(ExecutionError,'schema') as caught:
                api(reply)('.',{},'stable',{},SCHEMA)
            self.assertEqual(caught.exception.usage['output_tokens'],7)
            with patch('agenthub.processing.harness.run_structured',return_value=(raw,{'output_tokens':7})):
                with self.assertRaisesRegex(ExecutionError,'schema') as caught:
                    CodexExecution()('.',{},'stable',{},SCHEMA)
            self.assertEqual(caught.exception.outcome,'returned')

    def test_no_dispatch_for_invalid_schema_and_unknown_transport_not_retried(self):
        calls=[]
        def send(*_):calls.append(1);raise TimeoutError()
        a=ApiExecution(model='fixture',api_key='synthetic',send=send)
        with self.assertRaises(ExecutionError) as caught:a('.',{},'stable',{}, {'type':'invalid'})
        self.assertEqual(caught.exception.outcome,'cancelled');self.assertEqual(calls,[])
        with self.assertRaises(ExecutionError) as caught:a('.',{},'stable',{},SCHEMA)
        self.assertEqual(caught.exception.outcome,'uncertain');self.assertEqual(len(calls),1)
