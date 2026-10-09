"""Frozen provider seam cases. All API calls use synthetic in-memory transport."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agenthub.cloud_execution import ApiExecution, execution_from_config


def response(value):
    return {'id':'synthetic-response','status':'completed','output':[{'type':'message',
        'content':[{'type':'output_text','text':json.dumps(value)}]}],
        'usage':{'input_tokens':20,'output_tokens':5,'input_tokens_details':{'cached_tokens':10}}}

class ProviderPortability(unittest.TestCase):
    def test_api_stage_contract_and_stateless_reconstruction(self):
        seen=[]
        provider=ApiExecution(model='configured-model',api_key='fixture',
            send=lambda request,timeout:(seen.append(request) or response({'ok':True})))
        self.assertFalse(provider.resumable)
        for purpose in ('durable_memory_curate','episode_resolve','serving_rerank'):
            result=provider('.',{'_purpose':purpose,'observer':{'reasoning':'low'}},'stable',{'prior':'source'}, {})
            self.assertEqual(len(result),3 if purpose=='durable_memory_curate' else 2)
            if len(result)==3:self.assertIsNone(result[2])
        self.assertTrue(all(x['reasoning']=={'effort':'low'} and not x['store'] for x in seen))
        self.assertTrue(all('previous_response_id' not in x for x in seen))

    def test_secret_reference_and_execution_identity(self):
        from agenthub.cloud_execution import execution_identity
        with tempfile.TemporaryDirectory() as td:
            key=Path(td)/'key';key.write_text('fixture');key.chmod(0o600)
            cfg={'backend_execution':{'kind':'operator_api','model':'configured-model','credential_file':str(key)}}
            api=execution_from_config(cfg)
            self.assertEqual(api.model,'configured-model')
            self.assertNotEqual(execution_identity(cfg),execution_identity({'observer':{'model':'configured-model'}}))
            identity=execution_identity(cfg);key.write_text('rotated-fixture')
            self.assertEqual(identity,execution_identity(cfg))
            key.chmod(0o644)
            with self.assertRaisesRegex(Exception,'credential_permissions'):execution_from_config(cfg)

    def test_api_unspecified_reasoning_is_not_the_same_as_explicit_low(self):
        from agenthub.cloud_execution import execution_identity
        config={'backend_execution':{'kind':'operator_api','model':'configured-model'}}
        self.assertNotEqual(execution_identity(config),execution_identity(dict(config,observer={'reasoning':'low'})))

    def test_ranker_dispatches_configured_adapter_not_codex(self):
        from agenthub.serving_reranker import ServingReranker
        class Meter:
            def reserve(self,*a,**k):pass
            def dispatched(self,*a,**k):pass
            def finish(self,*a,**k):pass
        seen=[]
        api=ApiExecution(model='configured-model',api_key='fixture',send=lambda req,timeout:
            (seen.append(req) or response({'order':['c0'],'support':'complete'})))
        with tempfile.TemporaryDirectory() as td,patch('agenthub.cloud_execution.execution_from_config',return_value=api),\
             patch('agenthub.serving_reranker.reserve'),patch('agenthub.serving_reranker.finish'),\
             patch('agenthub.processing.harness.run_structured',side_effect=AssertionError('Codex bypass')):
            ranker=ServingReranker(td,{'backend_execution':{'kind':'operator_api','model':'configured-model'}},ledger='fixture',meter=Meter())
            value=ranker.rerank('where?', [{'id':'d','revision':'r','title':'path','lesson':'/fixture'}])
        self.assertTrue(value['answerable']);self.assertEqual(seen[0]['model'],'configured-model')
