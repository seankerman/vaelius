import unittest
from unittest.mock import patch
from test_cloud_postgres import PostgresFixture,SERVICES
from test_source_evidence import SourceEvidenceTests

@unittest.skipUnless(SERVICES,'explicit local PostgreSQL required')
class SourceEvidencePostgres(PostgresFixture,SourceEvidenceTests):
    def setUp(self):
        self.postgres_setup(tenant_id='orchard',bootstrap=False)
        store=self.store
        with patch('test_source_evidence.EnterpriseStore',return_value=store):
            super().setUp()
    def tearDown(self):
        super().tearDown();self.postgres_teardown()

    def test_real_http_endpoint_and_revision_guard(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        token=self.store.enroll('orchard','alice','http',['read','source_read'])
        store=self.store
        class Registry:
            def store_for_token(self,token):return store
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver']))
        headers={'Authorization':'Bearer '+token}
        data={'id':self.doc['document_id'],'revision':self.doc['revision_id']}
        result=client.post('/enterprise/v3/document-evidence',headers=headers,json=data)
        self.assertEqual(result.status_code,200);self.assertTrue(result.json()['sources'])
        self.assertLessEqual(len(result.text),4000)
        self.assertEqual(client.post('/enterprise/v3/document-evidence',headers=headers,
            json=dict(data,offset=-1)).status_code,400)
