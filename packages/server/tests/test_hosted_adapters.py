"""Hosted seams with explicit operator configuration; no outbound requests."""
import unittest
from unittest.mock import patch
from agenthub.object_config import objects_from_settings
from agenthub.broker_config import broker_from_settings

class HostedAdapters(unittest.TestCase):
    def test_aws_uses_workload_credentials_without_static_key_or_custom_endpoint(self):
        with patch('boto3.client') as sdk:
            obj=objects_from_settings({'objects':{'kind':'s3_aws','bucket':'synthetic','region':'us-west-2'}})
        self.assertEqual(obj.bucket,'synthetic')
        args=sdk.call_args.kwargs
        self.assertNotIn('aws_secret_access_key',args);self.assertNotIn('endpoint_url',args)
        self.assertEqual(args['region_name'],'us-west-2')

    def test_generic_oidc_preserves_configured_issuer_and_endpoints(self):
        cfg={'kind':'oidc','issuer':'https://identity.example.test','client_id':'memory',
            'authorization_endpoint':'https://identity.example.test/authorize',
            'token_endpoint':'https://identity.example.test/token','jwks_uri':'https://identity.example.test/keys',
            'redirect_uri':'https://memory.example.test/callback'}
        broker=broker_from_settings(cfg)
        self.assertEqual(broker.issuer,cfg['issuer'])
        cfg['token_endpoint']='https://attacker.example.test/token'
        with self.assertRaises(ValueError):broker_from_settings(cfg)

    def test_runtime_secret_reference_is_explicit_private_and_unambiguous(self):
        from agenthub.cloud_runtime import read_settings
        import tempfile,json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);secret=root/'dsn';secret.write_text('dbname=fixture');secret.chmod(0o600)
            runtime=root/'runtime.json';runtime.write_text(json.dumps({'control_dsn_file':str(secret)}));runtime.chmod(0o600)
            self.assertEqual(read_settings(runtime)['control_dsn'],'dbname=fixture')
            runtime.write_text(json.dumps({'control_dsn_file':str(secret),'control_dsn':'other'}))
            with self.assertRaisesRegex(ValueError,'ambiguous'):read_settings(runtime)
            runtime.write_text(json.dumps({'control_dsn_file':str(secret)}));secret.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'credential_permissions'):read_settings(runtime)
