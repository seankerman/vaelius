from pathlib import Path
import json
import unittest
from vaelius_test_support.hub.cloud_rehearsal import fingerprint
from test_cloud_postgres import PostgresFixture,SERVICES

@unittest.skipUnless(SERVICES,'explicit real PostgreSQL fixture required')
class RollbackFingerprintTests(PostgresFixture,unittest.TestCase):
    def setUp(self):self.postgres_setup()
    def tearDown(self):self.postgres_teardown()
    def test_frozen_current_authority_fingerprint_and_post_baseline_withdrawal(self):
        fixture=json.loads((Path(__file__).resolve().parent/'fixtures/service').joinpath('cloud_rollback_v1.json').read_text())
        before=fingerprint(self.store)
        self.assertTrue(set(fixture['tables'])<=set(before))
        self.assertEqual(before,fingerprint(self.store))
        from agentclient.enterprise_contract import VERSION
        ctx=self.store.authenticate(self.tokens['alice'])
        source=self.store.ingest(ctx,{'version':VERSION,'external_id':'rollback-source','session':'earlier',
            'turn':'1','project':'maple','kind':'Stop','body':'The synthetic rollback file is at /synthetic/rollback.csv.',
            'visibility':'private','occurred_at':12345})['source_id']
        current=fingerprint(self.store)
        self.assertNotEqual(before,current)
        self.store.lifecycle(ctx,{'version':VERSION,'operation':'withdraw','target_id':source,
            'expected_revision':'1','idempotency_key':'rollback-withdraw','reason':'synthetic post-baseline withdrawal'})
        withdrawn=fingerprint(self.store)
        self.assertNotEqual(current['enterprise_sources'],withdrawn['enterprise_sources'])
        self.assertNotEqual(current['enterprise_lifecycle'],withdrawn['enterprise_lifecycle'])
        self.assertEqual(withdrawn,fingerprint(self.store))

    def test_rollback_includes_new_retained_conversation_object_metadata(self):
        fixture=json.loads((Path(__file__).resolve().parent/'fixtures/service').joinpath('cloud_rollback_segments_v1.json').read_text())
        self.assertTrue(set(fixture['required_tables'])<=set(fingerprint(self.store)))

    def test_operator_status_uses_real_original_version_table(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import patch
        from agenthub.cloud_local import status
        from agenthub.cloud_runtime import CloudStore
        self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.store.retrieval_corpus='sources'
        fixture=json.loads((Path(__file__).resolve().parent/'fixtures/service').joinpath('cloud_status_v1.json').read_text())
        @contextmanager
        def control():
            yield SimpleNamespace(execute=lambda _: [{'id':'acme','active':1},{'id':'bravo','active':1}])
        registry=type('FixtureRegistry',(),{'resolve':lambda _,tenant:self.store,
            'open_control':staticmethod(control)})()
        with patch('agenthub.cloud_local.runtime',return_value=({},registry)):
            result=status(self.temp.name)
        for tenant in result['tenants'].values():
            self.assertEqual(set(tenant['counts']),set(fixture['counts']))
        json.dumps(result)

class InstalledInterpreterTests(unittest.TestCase):
    def test_rollback_preserves_virtualenv_interpreter_instead_of_resolving_symlink(self):
        import subprocess,sys,tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from vaelius_test_support.hub.cloud_rehearsal import rollback_code
        json.loads((Path(__file__).resolve().parent/'fixtures/service').joinpath('cloud_rollback_interpreter_v1.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);environment=root/'previous-venv'
            subprocess.run([sys.executable,'-m','venv','--without-pip',str(environment)],check=True,capture_output=True)
            executable=environment/'bin/python';previous={'synthetic':'previous-build'}
            (root/'rollback-selection.json').write_text(json.dumps({'previous_interpreter':str(executable),'previous_build_ids':previous}))
            fingerprint={'current_authority':'unchanged'};run=subprocess.run
            def inspect(command,**kwargs):
                value=run([command[0],'-c','import sys;print(sys.prefix)'],capture_output=True,text=True,check=True)
                self.assertEqual(Path(value.stdout.strip()),environment)
                return SimpleNamespace(returncode=0,stdout=json.dumps({'runtime':{'build_ids':previous},'fingerprint':fingerprint}))
            with patch('vaelius_test_support.hub.cloud_rehearsal.fingerprint',return_value=fingerprint),patch('vaelius_test_support.hub.cloud_rehearsal.subprocess.run',side_effect=inspect):
                result=rollback_code(root,SimpleNamespace(tenant_id='acme'))
            self.assertTrue(result['state_preserved'])

if __name__=='__main__':unittest.main()
