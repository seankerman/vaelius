"""Invented topical/framing pairs frozen before lexical query cleanup."""
import json
import unittest
from agenthub.cloud_runtime import CloudStore
from agentclient.enterprise_contract import VERSION
from test_cloud_postgres import PostgresFixture,SERVICES


class ConversationalTerms(unittest.TestCase):
    def test_framing_removed_content_and_quoted_identifiers_preserved(self):
        from agenthub.cloud_retrieval import lexical_terms
        self.assertEqual(lexical_terms('What did the assistant report about voucher duration and usage limit?'),
            ['voucher','duration','usage','limit'])
        self.assertIn('report',lexical_terms('Find the annual report.'))
        self.assertIn('recovery',lexical_terms('Which recovery steps failed?'))
        self.assertIn('failed',lexical_terms('Which recovery steps failed?'))
        self.assertIn('version',lexical_terms('What version uses TLS?'))
        self.assertIn('using',lexical_terms('Find "Using".'))
        self.assertEqual(lexical_terms('What did the assistant report?'),[])


@unittest.skipUnless(SERVICES,'explicit isolated PostgreSQL required')
class ConversationalRanking(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.store.retrieval_authorization_shape='compiled'
        self.ids=[]
        for i,body in enumerate([
            'Voucher duration: 21 days. Usage limit: one redemption.',
            ('The assistant reported what the assistant did and the assistant reported the result. '*5)+
            'The result concerned an unrelated gardening schedule.']):
            # Each a captured chat message of its own, indexed as one passage.
            source=self.capture(body,key='framing-'+str(i),conversation='framing-'+str(i),sharing='private')
            self.ids.append(self.passages(source)[0])
    def tearDown(self):self.postgres_teardown()
    def test_report_framing_cannot_supply_lexical_relevance(self):
        rows=self.store.candidates(self.ctx,
            'What did the assistant report about voucher duration and usage limit?',
            project='maple',vector=False)
        self.assertEqual([row['document_id'] for row in rows],[self.ids[0]])
    def test_exact_original_title_and_withdrawal_still_use_current_policy(self):
        with self.store.open() as state:
            title=json.loads(state.db.execute('SELECT r.claim_json FROM knowledge_revisions r JOIN knowledge_documents d '
                'ON d.active_revision_id=r.revision_id WHERE d.document_id=?',(self.ids[0],)).fetchone()[0])['title']
        # Both passages carry the same title (workspace, speaker, time); the exact title
        # and an exact id each resolve without ranking, under the current policy.
        self.assertEqual({row['document_id'] for row in self.store.candidates(self.ctx,
            title,project='maple',vector=False)},set(self.ids))
        self.assertEqual([row['document_id'] for row in self.store.candidates(self.ctx,
            self.ids[0],project='maple',vector=False)],[self.ids[0]])
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE knowledge_documents SET lifecycle='withdrawn' WHERE document_id=?",(self.ids[0],))
        self.assertFalse(self.store.candidates(self.ctx,'voucher duration',project='maple',vector=False))
