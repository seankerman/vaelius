"""Invented topical/framing pairs frozen before lexical query cleanup."""
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
            source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'framing-'+str(i),
                'session':'framing','turn':str(i),'project':'maple','kind':'Stop',
                'body':body,'occurred_at':12345+i,'visibility':'private'})['source_id']
            self.ids.append(self.store.accept_reviewed_note(self.ctx,source,'Reference '+str(i),body)['document_id'])
    def tearDown(self):self.postgres_teardown()
    def test_report_framing_cannot_supply_lexical_relevance(self):
        rows=self.store.candidates(self.ctx,
            'What did the assistant report about voucher duration and usage limit?',
            project='maple',vector=False)
        self.assertEqual([row['document_id'] for row in rows],[self.ids[0]])
    def test_exact_original_title_and_withdrawal_still_use_current_policy(self):
        self.assertEqual([row['document_id'] for row in self.store.candidates(self.ctx,
            'Reference 0',project='maple',vector=False)],[self.ids[0]])
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE knowledge_documents SET lifecycle='withdrawn' WHERE document_id=?",(self.ids[0],))
        self.assertFalse(self.store.candidates(self.ctx,'voucher duration',project='maple',vector=False))
