import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock


class CloudContainerTests(unittest.TestCase):
    def test_private_runtime_has_explicit_endpoints_no_operator_or_source_copy(self):
        from agenthub.cloud_container import prepare
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.mkdir();model=source/'model';model.mkdir()
            runtime={'provider_mode':'off','control_dsn':'host=127.0.0.1 port=55483 dbname=control user=router password=synthetic',
                'objects':{'kind':'s3','endpoint':'http://127.0.0.1:55484','bucket':'synthetic','access_key':'synthetic','secret_key':'synthetic'},
                'semantic':{'enabled':False,'directory':str(model)}}
            (source/'runtime.json').write_text(json.dumps(runtime));(source/'runtime.json').chmod(0o600)
            (source/'operator.json').write_text('PRIVATE DO NOT COPY')
            destination=Path(directory)/'container'
            connection=MagicMock();connection.__enter__.return_value.execute.return_value.fetchone.return_value=(False,False,False)
            with patch('agenthub.postgres.connect',return_value=connection):result=prepare(source,destination)
            out=json.loads((destination/'runtime.json').read_text())
            self.assertEqual(out['postgres_endpoint_override'],{'host':'agentnetwork-cloud-v1-postgres','port':5432})
            self.assertEqual(out['objects']['endpoint'],'http://agentnetwork-cloud-v1-moto:5000')
            self.assertEqual(out['semantic']['directory'],'/models/nomic')
            self.assertFalse((destination/'operator.json').exists())
            self.assertEqual((destination/'runtime.json').stat().st_mode&0o777,0o600)
            self.assertEqual(result['model_directory'],str(model.resolve()))

    def test_context_only_installed_wheels_and_dockerfile(self):
        from agenthub.cloud_container import build_context
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wheels=root/'wheels';wheels.mkdir()
            for name in ('vaelius_client-0.2.0-py3-none-any.whl','vaelius_server-0.2.0-py3-none-any.whl'):
                (wheels/name).write_bytes(b'fixture wheel')
            (wheels/'credential').write_text('DO NOT COPY');dockerfile=root/'Dockerfile';dockerfile.write_text('FROM pinned')
            result=build_context(wheels,root/'context',dockerfile)
            self.assertEqual(set(Path(result['context']).iterdir()),{root.resolve()/'context'/'wheels',root.resolve()/'context'/'Dockerfile'})
            self.assertEqual(len(list((root/'context'/'wheels').iterdir())),2)


if __name__=='__main__':unittest.main()
