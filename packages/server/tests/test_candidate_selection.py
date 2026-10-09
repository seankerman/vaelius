"""Frozen pre-change regressions: selection sees candidates, not packed answers."""
import json
import unittest
from unittest.mock import patch
from agentclient.enterprise_contract import VERSION
from agenthub.postgres import PostgresConnection
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL required')
class CandidateSelectionTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.addCleanup(self.postgres_teardown)
        self.store.retrieval_selection_policy='facets_v2'
        self.documents=[]
        for i in range(20):
            ctx=self.ctx if i<19 else self.store.authenticate(self.tokens['bob'])
            body=f'Dataset project {i} retained its output in /data/project-{i}. '+('Relevant background. '*25)
            source=self.store.ingest(ctx,dict(version=VERSION,external_id='pool-'+str(i),
                session='pool',turn=str(i),project='maple',kind='Stop',body=body,
                visibility='private',occurred_at=100))['source_id']
            self.documents.append((source,self.store.accept_reviewed_note(ctx,source,'Dataset '+str(i),body)['document_id']))
        self.request=dict(version=VERSION,query='dataset',project='maple',mode='automatic',session='new')

    def test_pool_precedes_selector_limit_and_delivery_size(self):
        with patch('agenthub.cloud_retrieval.select_supported_cards',side_effect=AssertionError('premature selector')):
            result=self.store.search(self.ctx,self.request,candidate_pool=True)
        self.assertEqual(len(result['results']),19)
        self.assertNotIn(self.documents[-1][1],{r['id'] for r in result['results']})
        self.assertGreater(len(json.dumps(result)),1500)
        self.assertFalse(result['answerable'])

    def test_current_delivery_checks_have_constant_sql_for_one_or_nineteen(self):
        pool=self.store.search(self.ctx,self.request,candidate_pool=True)
        counts=[];original=PostgresConnection.execute
        for n in (1,19):
            calls=[]
            def counted(db,sql,params=None):
                calls.append(sql);return original(db,sql,params)
            with patch.object(PostgresConnection,'execute',counted):
                result=self.store.validate_search_delivery(self.ctx,self.request,dict(pool,results=pool['results'][:n]))
            self.assertEqual(len(result['results']),n);counts.append(len(calls))
        self.assertEqual(counts[0],counts[1],counts)

    def test_withdrawal_removes_only_changed_candidate_at_delivery(self):
        pool=self.store.search(self.ctx,self.request,candidate_pool=True)
        source,doc=self.documents[0]
        self.store.lifecycle(self.ctx,dict(version=VERSION,target_id=source,expected_revision='1',
            operation='withdraw',idempotency_key='pool-withdraw',reason='fixture'))
        result=self.store.validate_search_delivery(self.ctx,self.request,pool)
        self.assertEqual(len(result['results']),18)
        self.assertNotIn(doc,{r['id'] for r in result['results']})
        self.assertIn('authorization_changed_before_delivery',result['coverage_gaps'])

    def test_explicit_applicability_conflict_still_excluded(self):
        row={'document_id':'wrong','revision_id':'r','claim_json':json.dumps({
            'title':'dataset','lesson':'Saved data','applicability_constraints':{'platforms':['windows']}})}
        with patch.object(self.store,'candidates',return_value=[row]):
            result=self.store.search(self.ctx,dict(self.request,query='dataset on macos'),candidate_pool=True)
        self.assertEqual(result['results'],[])
