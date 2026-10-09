from pathlib import Path
import contextlib, hashlib, io, json, unittest
from unittest.mock import patch
from test_cloud_postgres import PostgresFixture, SERVICES

@unittest.skipUnless(SERVICES, 'explicit real PostgreSQL fixture required')
class OperatorVersionsTests(PostgresFixture, unittest.TestCase):
    def setUp(self): self.postgres_setup()
    def tearDown(self): self.postgres_teardown()
    def test_actual_operator_lists_both_immutable_original_revisions(self):
        from agenthub.cloud_local import main
        from agenthub.document_ingest import DocumentStore
        from agenthub.source_objects import FileSourceObjects
        fixture=json.loads((Path(__file__).resolve().parent/'fixtures/service').joinpath('cloud_versions_v1.json').read_text())
        self.store.enroll_connection(self.ctx,'versions','synthetic','maple',['document'])
        service=DocumentStore(self.store,FileSourceObjects(self.temp.name+'/objects'))
        expected={}
        for item in fixture['revisions']:
            raw=item['body'].encode()
            accepted=service.ingest(self.ctx,'versions',fixture['external_id'],item['version'],fixture['filename'],io.BytesIO(raw))
            expected[item['version']]={'source_id':accepted['source_id'],'sha256':hashlib.sha256(raw).hexdigest(),'byte_length':len(raw)}
        self.store.semantic_embedder=None  # Mirrors an idle runtime; no model loading.
        registry=type('FixtureRegistry',(),{'resolve':lambda _,tenant:self.store,'_stores':{'acme':self.store}})()
        output=io.StringIO()
        with patch('agenthub.cloud_local.runtime',return_value=({},registry)),patch('agenthub.cloud_local.receipt',side_effect=lambda profile,command,value:value),contextlib.redirect_stdout(output):
            main(['versions','--profile',self.temp.name])
        rows=json.loads(output.getvalue())['versions']
        self.assertEqual(len(rows),2)
        for row in rows:
            self.assertEqual(set(row),set(fixture['columns']))
            self.assertEqual(row['parser_status'],'indexed')
            for key,value in expected[row['version']].items():self.assertEqual(row[key],value)

if __name__=='__main__':unittest.main()
