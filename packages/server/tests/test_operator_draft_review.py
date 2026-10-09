"""F5 freezes review-only visibility, privacy and rollback before implementation."""
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit PostgreSQL required')
class DraftReviewTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup(); self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'review',
            'session':'review','turn':'1','project':'maple','kind':'Stop',
            'body':'The Juniper dataset is saved at /synthetic/juniper.csv.',
            'visibility':'private','occurred_at':100})['source_id']
        self.doc=self.store.accept_reviewed_note(self.ctx,self.source,'Juniper dataset location',
            'The Juniper dataset is saved at /synthetic/juniper.csv.')['document_id']
        with self.store.open() as st,st.db:
            st.db.execute("INSERT INTO knowledge_generations(generation_id,curator_version,status,source_policy,config_hash,created) VALUES('draft-review','fixture','building','fixture','fixture',0)")
            st.db.execute("INSERT INTO knowledge_generation_documents VALUES('draft-review',?,0)",(self.doc,))
            self.revision=st.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(self.doc,)).fetchone()[0]
    def tearDown(self):self.postgres_teardown()
    def query(self,ctx=None):return self.store.search(ctx or self.ctx,{'version':VERSION,'project':'maple',
        'query':'Where is the Juniper dataset saved?','mode':'explicit'})
    def test_scoped_review_does_not_activate_and_rolls_back(self):
        from agenthub.draft_review import review_session
        self.assertFalse(self.query()['answerable'])
        with review_session(self.store,self.ctx,{self.doc:self.revision},source_ids=[self.source]):
            self.assertTrue(self.query()['answerable'])
            with self.store.open() as st:
                self.assertEqual(st.db.execute("SELECT status FROM knowledge_generations WHERE generation_id='draft-review'").fetchone()[0],'building')
        self.assertFalse(self.query()['answerable'])
    def test_review_cannot_bypass_private_owner_or_revision(self):
        from agenthub.draft_review import review_session
        with self.assertRaises(Exception):
            with review_session(self.store,self.ctx,{self.doc:'wrong'},source_ids=[self.source]):pass
        bob=self.store.authenticate(self.tokens['bob'])
        with self.assertRaises(Exception):
            with review_session(self.store,bob,{self.doc:self.revision},source_ids=[self.source]):self.query(bob)
