import os
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from agenthub.cloud_ops import Meter
from agenthub.enterprise import Conflict


@unittest.skipUnless(os.environ.get('CLOUD_TEST_DSN'),'real PostgreSQL fixture DSN required')
class MeterPostgresTests(unittest.TestCase):
    def setUp(self):
        from agenthub.postgres import PostgresEnterpriseStore
        self.temp=tempfile.TemporaryDirectory();self.tenant='meter-'+uuid.uuid4().hex
        self.store=PostgresEnterpriseStore(self.temp.name,os.environ['CLOUD_TEST_DSN'],os.environ.get('CLOUD_TEST_TENANT','acme'))
        # Meter-only records use a unique test namespace; knowledge is neither reset nor reconfigured.
        self.meter=Meter(SimpleNamespace(tenant_id=self.tenant,open=self.store.open))

    def tearDown(self):self.temp.cleanup()

    def attempt(self,name):return self.tenant+':'+name

    def test_metrics_conflict_and_content_fields(self):
        ident=self.attempt('bytes');self.meter.metric(ident,'input_bytes',123,details={'source_revision':'synthetic-v1'})
        self.meter.metric(ident,'input_bytes',123,details={'source_revision':'synthetic-v1'})
        with self.assertRaises(Conflict):self.meter.metric(ident,'input_bytes',124,details={'source_revision':'synthetic-v1'})
        with self.assertRaises(ValueError):self.meter.metric(self.attempt('private'),'input_bytes',2,details={'text':'private forbidden'})
        self.assertEqual(self.meter.status()['metrics']['input_bytes'],123)


if __name__=='__main__':unittest.main()
