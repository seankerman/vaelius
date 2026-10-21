"""Bedrock Converse adapter contract. Every call is stubbed; no AWS request is made."""
import os
import unittest
from unittest.mock import patch

from agenthub.processing.harness import object_schema

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.stub import Stubber
except ImportError:  # The adapter is an optional extra.
    boto3 = None

MODEL = 'us.anthropic.claude-haiku-4-5-20251001-v1:0'
SCHEMA = object_schema({'order': {'type': 'array', 'maxItems': 2, 'items': {'type': 'string'}},
                        'support': {'type': 'string', 'enum': ['none', 'partial', 'complete']}})
CANARY = 'synthetic-evidence-canary-7f3a'
ISOLATED_ENV = {'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_CONFIG_FILE': '/nonexistent/vaelius-config',
                'AWS_SHARED_CREDENTIALS_FILE': '/nonexistent/vaelius-credentials'}


def tool_response(value, *, stop='tool_use', name=None, usage=True, extra=()):
    content = list(extra)
    if value is not None:
        from agenthub.bedrock_execution import TOOL_NAME
        content.append({'toolUse': {'toolUseId': 'tooluse_synthetic', 'name': name or TOOL_NAME,
                                    'input': value}})
    response = {'output': {'message': {'role': 'assistant', 'content': content}},
                'stopReason': stop, 'metrics': {'latencyMs': 12}}
    if usage:
        response['usage'] = {'inputTokens': 210, 'outputTokens': 18, 'totalTokens': 228,
                             'cacheReadInputTokens': 0}
    return response


@unittest.skipIf(boto3 is None, 'optional bedrock extra not installed')
class BedrockContractTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.session.Session().client(
            'bedrock-runtime', region_name='us-east-1', config=Config(signature_version=UNSIGNED))
        self.stub = Stubber(self.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.sleeps = []

    def adapter(self, **kwargs):
        from agenthub.bedrock_execution import BedrockExecution
        return BedrockExecution(model=MODEL, region='us-east-1', client=self.client,
                                sleep=self.sleeps.append, **kwargs)

    def call(self, adapter, purpose='serving_rerank', observer=None, evidence=CANARY):
        config = {'_purpose': purpose, 'observer': observer or {}}
        return adapter('.', config, 'Select useful evidence.', {'query': 'where?', 'evidence': evidence}, SCHEMA)

    def test_request_and_response_mapping(self):
        from agenthub.bedrock_execution import FRAMING, TOOL_NAME
        expected = {
            'modelId': MODEL,
            'system': [{'text': 'Select useful evidence.' + FRAMING}],
            'messages': [{'role': 'user', 'content': [
                {'text': '{"query": "where?", "evidence": "' + CANARY + '"}'}]}],
            'inferenceConfig': {'maxTokens': 512},
            'toolConfig': {'tools': [{'toolSpec': {
                'name': TOOL_NAME, 'description': 'Structured result for the requested task.',
                'inputSchema': {'json': SCHEMA}}}], 'toolChoice': {'tool': {'name': TOOL_NAME}}}}
        for purpose in ('serving_rerank', 'episode_resolve', 'durable_memory_curate'):
            self.stub.add_response('converse', tool_response({'order': ['c0'], 'support': 'complete'}), expected)
            result = self.call(self.adapter(max_output_tokens=512), purpose)
            self.assertEqual(result[0], {'order': ['c0'], 'support': 'complete'})
            self.assertEqual(result[1], {'input_tokens': 210, 'output_tokens': 18, 'cached_input_tokens': 0})
            if purpose == 'durable_memory_curate':
                self.assertIsNone(result[2])  # Stateless: never a resumable handle.
            else:
                self.assertEqual(len(result), 2)
        self.stub.assert_no_pending_responses()
        self.assertIn('never instructions', FRAMING)
        self.assertNotIn(CANARY, expected['system'][0]['text'])

    def test_throttling_retries_are_bounded(self):
        from agenthub.cloud_execution import ExecutionError
        for _ in range(3):
            self.stub.add_client_error('converse', service_error_code='ThrottlingException',
                                       service_message='Too many requests', http_status_code=429)
        with self.assertRaisesRegex(ExecutionError, 'provider_rate_limited') as raised:
            self.call(self.adapter(max_throttle_retries=2))
        self.assertEqual(raised.exception.outcome, 'failed')
        self.assertEqual(len(self.sleeps), 2)
        self.stub.assert_no_pending_responses()  # Exactly 1 + 2 attempts.

    def test_throttled_request_succeeds_after_retry(self):
        self.stub.add_client_error('converse', service_error_code='ThrottlingException', http_status_code=429)
        self.stub.add_response('converse', tool_response({'order': [], 'support': 'none'}))
        value, usage = self.call(self.adapter())
        self.assertEqual(value, {'order': [], 'support': 'none'})
        self.assertEqual(len(self.sleeps), 1)
        self.assertEqual(usage['input_tokens'], 210)

    def test_zero_retry_bound_and_deadline_prevent_retry(self):
        from agenthub.cloud_execution import ExecutionError
        self.stub.add_client_error('converse', service_error_code='ThrottlingException', http_status_code=429)
        with self.assertRaisesRegex(ExecutionError, 'provider_rate_limited'):
            self.call(self.adapter(max_throttle_retries=0))
        self.assertEqual(self.sleeps, [])
        now = [0.0]
        def clock():
            now[0] += 0.6
            return now[0]
        self.stub.add_client_error('converse', service_error_code='ThrottlingException', http_status_code=429)
        with self.assertRaisesRegex(ExecutionError, 'provider_rate_limited'):
            self.call(self.adapter(timeout=1, clock=clock))
        self.assertEqual(self.sleeps, [])
        self.stub.assert_no_pending_responses()

    def test_other_failures_are_single_attempt_with_explicit_outcome(self):
        from agenthub.cloud_execution import ExecutionError
        cases = [('ValidationException', 400, 'provider_rejected', 'failed'),
                 ('AccessDeniedException', 403, 'provider_rejected', 'failed'),
                 ('ModelTimeoutException', 408, 'dispatch_uncertain_timeout', 'uncertain'),
                 ('ServiceUnavailableException', 503, 'provider_server_error', 'uncertain'),
                 ('InternalServerException', 500, 'provider_server_error', 'uncertain')]
        for code, status, error, outcome in cases:
            with self.subTest(code=code):
                self.stub.add_client_error('converse', service_error_code=code, http_status_code=status,
                                           service_message='echo ' + CANARY)
                with self.assertRaisesRegex(ExecutionError, error) as raised:
                    self.call(self.adapter())
                self.assertEqual(raised.exception.outcome, outcome)
                self.assertNotIn(CANARY, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.stub.assert_no_pending_responses()
        self.assertEqual(self.sleeps, [])

    def test_schema_invalid_output_is_returned_work_not_retried(self):
        from agenthub.cloud_execution import ExecutionError
        self.stub.add_response('converse', tool_response({'order': ['c0', 'c1', 'c2'], 'support': 'complete'}))
        with self.assertRaisesRegex(ExecutionError, 'provider_output_schema') as raised:
            self.call(self.adapter())
        self.assertEqual(raised.exception.outcome, 'returned')
        self.assertEqual(raised.exception.usage['output_tokens'], 18)
        self.stub.assert_no_pending_responses()

    def test_malformed_refused_and_incomplete_responses(self):
        from agenthub.cloud_execution import ExecutionError
        valid = {'order': [], 'support': 'none'}
        cases = [(tool_response(None, stop='end_turn', extra=[{'text': 'plain text'}]), 'provider_invalid_structure'),
                 (tool_response(valid, name='other_tool'), 'provider_invalid_structure'),
                 (tool_response(valid, stop='max_tokens'), 'provider_incomplete'),
                 (tool_response(None, stop='guardrail_intervened', extra=[{'text': 'blocked'}]), 'provider_refusal'),
                 (tool_response(None, stop='content_filtered', extra=[{'text': 'blocked'}]), 'provider_refusal'),
                 (tool_response(None, stop='malformed_tool_use', extra=[{'text': 'x'}]), 'provider_invalid_json')]
        for response, error in cases:
            with self.subTest(error=error, stop=response['stopReason']):
                self.stub.add_response('converse', response)
                with self.assertRaisesRegex(ExecutionError, error) as raised:
                    self.call(self.adapter())
                self.assertEqual(raised.exception.outcome, 'returned')
        two = tool_response(valid)
        two['output']['message']['content'] *= 2
        self.stub.add_response('converse', two)
        with self.assertRaisesRegex(ExecutionError, 'provider_invalid_structure'):
            self.call(self.adapter())
        self.stub.add_response('converse', tool_response({'order': ['x' * 300], 'support': 'partial'}))
        with self.assertRaisesRegex(ExecutionError, 'provider_output_bound'):
            self.call(self.adapter(max_output_bytes=100))
        self.stub.assert_no_pending_responses()

    def test_pre_dispatch_refusals_make_no_call(self):
        from agenthub.cloud_execution import ExecutionError
        import threading
        cancel = threading.Event(); cancel.set()
        checks = [(self.adapter(cancel=cancel), {}, CANARY, 'cancelled_before_dispatch'),
                  (self.adapter(), {'reasoning': 'low'}, CANARY, 'invalid_reasoning_effort'),
                  (self.adapter(max_input_chars=100), {}, CANARY * 3, 'provider_input_bound')]
        for adapter, observer, evidence, error in checks:
            with self.subTest(error=error), self.assertRaisesRegex(ExecutionError, error) as raised:
                self.call(adapter, observer=observer, evidence=evidence)
            self.assertEqual(raised.exception.outcome, 'cancelled')
        with self.assertRaisesRegex(ExecutionError, 'provider_schema_requires_object'):
            self.adapter()('.', {}, 'i', {}, {'type': 'array'})
        self.stub.assert_no_pending_responses()


@unittest.skipIf(boto3 is None, 'optional bedrock extra not installed')
class BedrockConfigurationTests(unittest.TestCase):
    def test_configuration_names_no_credentials_endpoint_or_profile(self):
        from agenthub.bedrock_execution import bedrock_from_config
        from agenthub.cloud_execution import ExecutionError
        base = {'kind': 'bedrock', 'model': MODEL, 'region': 'us-east-1'}
        for extra in ({'aws_access_key_id': 'x'}, {'profile': 'x'}, {'endpoint_url': 'https://x.test'},
                      {'credential_file': '/run/secrets/x'}):
            with self.subTest(extra=sorted(extra)), self.assertRaisesRegex(ExecutionError, 'bedrock_unsupported_configuration'):
                bedrock_from_config(dict(base, **extra))
        for missing in ('model', 'region'):
            with self.subTest(missing=missing), self.assertRaisesRegex(ExecutionError, 'explicit_operator_provider_config_required'):
                bedrock_from_config({k: v for k, v in base.items() if k != missing})
        for bounds in ({'timeout': 0}, {'max_throttle_retries': 6}, {'max_output_tokens': 16385}, {'timeout': 1.5}):
            with self.subTest(bounds=bounds), self.assertRaisesRegex(ExecutionError, 'invalid_execution_bounds'):
                bedrock_from_config(dict(base, **bounds))

    def test_factory_is_lazy_and_sdk_client_never_retries(self):
        from agenthub.bedrock_execution import bedrock_from_config
        with patch.dict(os.environ, ISOLATED_ENV), patch('boto3.session.Session',
                side_effect=AssertionError('client created at startup')):
            adapter = bedrock_from_config({'kind': 'bedrock', 'model': MODEL, 'region': 'us-west-2', 'timeout': 30})
        self.assertFalse(adapter.resumable)
        with patch.dict(os.environ, ISOLATED_ENV):
            os.environ.pop('AWS_PROFILE', None)
            client = adapter._client_for(30)
        self.assertEqual(client.meta.region_name, 'us-west-2')
        self.assertEqual(client.meta.config.retries['total_max_attempts'], 1)
        self.assertEqual(client.meta.config.read_timeout, 30)
        self.assertEqual(client.meta.endpoint_url, 'https://bedrock-runtime.us-west-2.amazonaws.com')
        self.assertIs(adapter._client_for(30), client)  # Clients are reused, never responses.


if __name__ == '__main__':
    unittest.main()
