"""F5 regression for measured per-dependency final authorization cost."""
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit PostgreSQL required')
class DocumentAuthorizationTests(PostgresFixture, unittest.TestCase):
    def setUp(self):self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
    def tearDown(self):self.postgres_teardown()
    def test_many_dependencies_have_bounded_sql_and_current_withdrawal(self):
        sources=[]
        for i in range(40):
            sources.append(self.store.ingest(self.ctx,{'version':VERSION,'external_id':f'dep-{i}',
                'session':'dep','turn':str(i),'project':'maple','kind':'Stop',
                'body':'Juniper uses CSV with a stable header.','visibility':'private','occurred_at':100})['source_id'])
        doc=self.store.accept_reviewed_note(self.ctx,sources[0],'Juniper format',
            'Juniper uses CSV with a stable header.')['document_id']
        with self.store.open() as st,st.db:
            st.db.executemany('INSERT INTO enterprise_dependencies VALUES(?,?) ON CONFLICT DO NOTHING',[(doc,s) for s in sources])
        class Counter:
            def __init__(self,db):self.db=db;self.n=0
            def __getattr__(self,key):return getattr(self.db,key)
            def execute(self,*args):self.n+=1;return self.db.execute(*args)
        with self.store.open() as st:
            counter=Counter(st.db)
            self.assertIsNotNone(self.store._document_allowed(counter,self.ctx,doc))
            self.assertLessEqual(counter.n,16)
        with self.store.open() as st,st.db:st.db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?',(sources[-1],))
        with self.store.open() as st:self.assertIsNone(self.store._document_allowed(st.db,self.ctx,doc))
