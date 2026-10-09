"""The frozen selector source bodies through real native ingestion and SQL."""
import io
import json
from pathlib import Path
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from agenthub.document_ingest import DocumentStore
from agenthub.source_objects import FileSourceObjects
from test_cloud_postgres import PostgresFixture,SERVICES


@unittest.skipUnless(SERVICES,'explicit isolated PostgreSQL services required')
class FrozenSelectionPostgres(PostgresFixture,unittest.TestCase):
    def setUp(self):
        from agenthub.pipeline_pin import verify
        self.canonical_pin=verify()
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.store.retrieval_authorization_shape='authorized_cte'
        self.store.retrieval_selection_policy='facets_v1';self.store.retrieval_numeric_mode='typed'
        self.ctxs={name:self.store.authenticate(self.tokens[name]) for name in ('alice','bob')}
        self.store.enroll_connection(self.ctxs['alice'],'selection-native','fixture','maple',['document'],visibility='team',reader_ids=['alice','bob'])
        self.service=DocumentStore(self.store,FileSourceObjects(Path(self.temp.name)/'objects'))

    def tearDown(self):self.postgres_teardown()

    def test_frozen_exact_source_native_cases_and_delivery_caps(self):
        fixture=json.loads((Path(__file__).parents[1]/'tools/fixtures/local_staging_readiness_v1/retrieval_lane.json').read_text())
        for i,case in enumerate(fixture['selection_cases']):
            source=self.service.ingest(self.ctxs['alice'],'selection-native',str(i),'1',str(i)+'.md',
                io.BytesIO(case['body'].encode()),title=case['title'])
            for question in case['questions']:
                with self.subTest(case=case['id'],query=question['query']):
                    result=self.store.search(self.ctxs[question['principal']],{'version':VERSION,'query':question['query'],'project':'maple','mode':'explicit'})
                    self.assertEqual(result['answerable'],question['answerable'])
                    self.assertLessEqual(len(json.dumps(result,ensure_ascii=True)),4000)
                    for facet in question['facets']:self.assertIn(facet['value'],str(result['results']))
                    for card in result['results']:
                        with self.store.open() as state:
                            deps={r[0] for r in state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(card['id'],))}
                        self.assertTrue(deps);self.assertIn(source['source_id'],deps)


if __name__=='__main__':unittest.main()
