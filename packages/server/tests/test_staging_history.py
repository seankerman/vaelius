"""Authored exact-source native history and original workflow regressions.

Frozen before native assertion/projection changes. Uses fresh synthetic schemas,
no models, no founder profile, and no quality corpus mutation.
"""
import hashlib
import io
import json
from pathlib import Path
import unittest

from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from agenthub.document_ingest import DocumentStore
from agenthub.enterprise import Denied
from agenthub.source_objects import FileSourceObjects
from test_cloud_postgres import PostgresFixture,SERVICES

FIXTURE=Path(__file__).parent/'fixtures/local_staging_readiness_v1/retrieval_lane.json'


@unittest.skipUnless(SERVICES,'explicit isolated real PostgreSQL required')
class NativeHistoryTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        from agenthub.pipeline_pin import verify
        self.canonical_pin=verify()
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.alice=self.store.authenticate(self.tokens['alice']);self.bob=self.store.authenticate(self.tokens['bob'])
        from test_cloud_postgres import share_at_visibility
        share_at_visibility(self.store,(self.alice,self.bob))
        self.store.enroll_connection(self.alice,'native-history','fixture','maple',['document'],visibility='team',reader_ids=['alice','bob'])
        self.service=DocumentStore(self.store,FileSourceObjects(Path(self.temp.name)/'objects'))
        self.fixture=json.loads(FIXTURE.read_text())

    def tearDown(self):self.postgres_teardown()

    def upload_history(self):
        history=self.fixture['history'];results=[]
        for item in history['versions']:
            results.append(self.service.ingest(self.alice,'native-history',history['external_id'],item['version'],
                item['filename'],io.BytesIO(item['bytes'].encode()),title=history['title'],occurred_at=item['captured_at']))
        return results

    def search(self,query,actor=None):
        return self.store.search(actor or self.bob,{'version':VERSION,'query':query,'project':'maple','mode':'explicit'})

    def test_current_grant_revocation_denies_search_detail_and_bytes(self):
        sources=self.upload_history();query='What is the Leaf dryer moisture threshold?'
        before=self.search(query);self.assertTrue(before['answerable'])
        self.store.connection_policy(self.alice,'native-history',reader_ids=['alice'])
        self.assertFalse(self.search(query)['answerable'])
        with self.assertRaises(Denied):self.store.detail(self.bob,before['results'][0]['id'])
        with self.assertRaises(Denied):self.service.fetch(self.bob,sources[0]['source_id'])

    def test_explicit_withdrawal_denies_asof_and_original(self):
        sources=self.upload_history();current=sources[-1]
        self.store.lifecycle(self.alice,{'version':VERSION,'target_id':current['source_id'],'expected_revision':'1',
            'idempotency_key':'withdraw-current-dryer','reason':'synthetic explicit withdrawal','operation':'withdraw'})
        self.assertFalse(self.search('What was the Leaf dryer moisture threshold as of 2026-09-15?')['answerable'])
        for source in sources:
            with self.assertRaises(Denied):self.service.fetch(self.bob,source['source_id'])

    def test_same_title_originals_remain_distinct_and_explicit_selection_exact(self):
        original=self.fixture['originals'];sources=[]
        for item in original['versions']:
            sources.append(self.service.ingest(self.alice,'native-history',item['external_id'],item['version'],
                item['filename'],io.BytesIO(item['bytes'].encode()),title=original['title']))
        choices=self.service.list(self.bob,title=original['title'],limit=20)
        self.assertEqual(len(choices),2);self.assertEqual(len({c['source_id'] for c in choices}),2)
        # Team visibility lists originals; downloading them needs an owner grant.
        with self.assertRaises(Denied):self.service.fetch(self.bob,sources[0]['source_id'],version=original['versions'][0]['version'])
        from agenthub import raw_access
        raw_access.grant(self.store,self.alice,'connection','native-history','group','maple-members')
        for item,source in zip(original['versions'],sources):
            stream,headers=self.service.fetch(self.bob,source['source_id'],version=item['version'])
            with stream:actual=stream.read()
            self.assertEqual(actual,item['bytes'].encode());self.assertEqual(headers['X-Source-SHA256'],hashlib.sha256(actual).hexdigest())
        with self.assertRaises(Denied):self.service.fetch(self.bob,sources[0]['source_id'],version='9')

if __name__=='__main__':unittest.main()
