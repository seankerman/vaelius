"""Small policy oracle and supported-answer fixtures, before semantic promotion."""
import json
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_retrieval import supported_answer
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture,SERVICES

class SupportedAnswers(unittest.TestCase):
    def test_identifiers_and_uncertainty_are_not_answers(self):
        self.assertFalse(supported_answer('Give me DOC-21',{'lesson':'DOC-22 is another original.'}))
        self.assertFalse(supported_answer('What is the approved spending budget?',{'lesson':'No approved budget exists.'}))
        self.assertFalse(supported_answer('Where is the dataset saved?',{'lesson':'The save failed.'}))
        self.assertTrue(supported_answer('Why CSV?',{'lesson':'CSV was chosen because the importer requires it.'}))

@unittest.skipUnless(SERVICES,'explicit real PostgreSQL required')
class HybridPolicyTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme');self.store.hybrid_enabled=True
        self.documents=[]
        for who,visibility in [('alice','private'),('bob','private'),('alice','team')]:
            ctx=self.store.authenticate(self.tokens[who]);source=self.store.ingest(ctx,{
                'version':VERSION,'external_id':who+visibility,'session':'old','turn':'1','project':'maple',
                'kind':'Stop','body':who+' '+visibility+' keeps the dataset at /synthetic/'+who+'.csv.',
                'visibility':visibility,'occurred_at':100})['source_id']
            doc=self.store.accept_reviewed_note(ctx,source,who+' '+visibility+' dataset',
                who+' '+visibility+' keeps the dataset at /synthetic/'+who+'.csv.')['document_id']
            self.documents.append((source,doc))
    def tearDown(self):self.postgres_teardown()
    def compare(self,who):
        ctx=self.store.authenticate(self.tokens[who])
        with self.store.open() as state:
            oracle={doc for _,doc in self.documents if self.store._document_allowed(state.db,ctx,doc)}
            actual=set(self.store._authorized_documents(state,ctx,'maple'))
        self.assertEqual(actual,oracle)
        candidates=self.store.candidates(ctx,'dataset',project='maple',vector=False)
        self.assertEqual({c['document_id'] for c in candidates},oracle)
    def test_scoped_plan_equals_canonical_current_policy_oracle(self):
        self.compare('alice');self.compare('bob');self.compare('admin')
        source,_=self.documents[-1]
        self.store.lifecycle(self.ctx,{'version':VERSION,'target_id':source,'expected_revision':'1',
            'operation':'withdraw','idempotency_key':'withdraw-team','reason':'fixture'})
        self.compare('alice');self.compare('bob')
    def test_filters_answer_bounds_and_current_identity(self):
        ctx=self.store.authenticate(self.tokens['bob'])
        result=self.store.search(ctx,{'version':VERSION,'query':'Where is the dataset saved?',
            'project':'maple','mode':'automatic','session':'new'})
        self.assertTrue(result['answerable']);self.assertLessEqual(len(json.dumps(result)),1500)
        result=self.store.search(ctx,{'version':VERSION,'query':'dataset','project':'maple','domain':'impossible'})
        self.assertFalse(result['answerable'])

    def test_long_source_identifiers_survive_generic_text_and_vector_dilution(self):
        source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'record-000123',
            'session':'record','turn':'1','project':'maple','kind':'Stop',
            'body':'Volume Record 000123 keeps its dataset at /synthetic/000123.csv.',
            'visibility':'private','occurred_at':100})['source_id']
        doc=self.store.accept_reviewed_note(self.ctx,source,'Volume record 000123',
            'Volume Record 000123 keeps its dataset at /synthetic/000123.csv.')['document_id']
        matches=self.store.candidates(self.ctx,'Where is the dataset for Volume Record 000123 saved?',project='maple',vector=False)
        self.assertEqual([m['document_id'] for m in matches],[doc])
        self.assertEqual(self.store.candidates(self.ctx,'Where is Volume Record 000999 saved?',project='maple',vector=False),[])

    def test_location_and_reason_require_both_facets_and_specific_subject(self):
        expected=set()
        for suffix,text in [('location','The Zephyr dataset is currently at /synthetic/zephyr.csv.'),
                            ('reason','Zephyr uses CSV because the importer requires a stable header.')]:
            source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'zephyr-'+suffix,
                'session':'zephyr','turn':suffix,'project':'maple','kind':'Stop',
                'body':text,'visibility':'private','occurred_at':100})['source_id']
            expected.add(self.store.accept_reviewed_note(self.ctx,source,'Zephyr '+suffix,text)['document_id'])
        query={'version':VERSION,'query':'Where is the Zephyr dataset currently, and why choose CSV?',
               'project':'maple','mode':'explicit'}
        result=self.store.search(self.ctx,query)
        self.assertTrue(result['answerable']);self.assertEqual({c['id'] for c in result['results']},expected)
