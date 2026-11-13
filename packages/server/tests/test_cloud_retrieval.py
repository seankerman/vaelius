"""Small policy oracle over captured passages, before semantic promotion."""
import json
import unittest
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture,SERVICES

@unittest.skipUnless(SERVICES,'explicit real PostgreSQL required')
class HybridPolicyTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme');self.store.hybrid_enabled=True
        self.documents=[]
        # Captured chat passages: two owner-private, one shared with the workspace.
        for who,visibility in [('alice','private'),('bob','private'),('alice','team')]:
            ctx=self.store.authenticate(self.tokens[who])
            source=self.capture(who+' '+visibility+' keeps the dataset at /synthetic/'+who+'.csv.',ctx=ctx,
                key=who+visibility,conversation='old',sharing='private' if visibility=='private' else 'organization')
            self.documents.append((source,self.passages(source)[0]))
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
        # Passages are discovery, never a verified answer: bounded results, not `answerable`.
        self.assertTrue(result['results']);self.assertFalse(result['answerable'])
        self.assertLessEqual(len(json.dumps(result)),1500)
        result=self.store.search(ctx,{'version':VERSION,'query':'dataset','project':'maple','domain':'impossible'})
        self.assertEqual(result['results'],[]);self.assertFalse(result['answerable'])

    def test_long_source_identifiers_survive_generic_text_and_vector_dilution(self):
        source=self.capture('Volume Record 000123 keeps its dataset at /synthetic/000123.csv.',
            key='record-000123',conversation='record',sharing='private')
        doc=self.passages(source)[0]
        matches=self.store.candidates(self.ctx,'Where is the dataset for Volume Record 000123 saved?',project='maple',vector=False)
        self.assertEqual([m['document_id'] for m in matches],[doc])
        self.assertEqual(self.store.candidates(self.ctx,'Where is Volume Record 000999 saved?',project='maple',vector=False),[])
