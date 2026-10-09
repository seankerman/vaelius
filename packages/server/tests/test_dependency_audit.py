"""Explicit operator audit over a synthetic provenance graph; no model calls."""
import importlib.util
from pathlib import Path
import unittest
from agentclient.enterprise_contract import VERSION
from test_cloud_postgres import PostgresFixture,SERVICES

spec=importlib.util.spec_from_file_location('dependency_audit',Path(__file__).parents[1]/'tools/audit_knowledge_dependencies.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@unittest.skipUnless(SERVICES,'explicit disposable PostgreSQL required')
class DependencyAuditTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.addCleanup(self.postgres_teardown)

    def test_scope_duplicates_and_processing_withdrawal_are_visible_not_relaxed(self):
        sources=[];docs=[]
        for index,visibility in enumerate(('private','private','team')):
            body='Maple report is saved at /data/maple/report.csv.'
            if index==1:body+=' Its independent checksum report is retained.'
            source=self.store.ingest(self.ctx,dict(version=VERSION,external_id='audit-'+str(index),
                session='audit',turn=str(index),project='maple',kind='Stop',body=body,
                visibility=visibility,occurred_at=100))['source_id']
            doc=self.store.accept_reviewed_note(self.ctx,source,'Maple report',body)['document_id']
            sources.append(source);docs.append(doc)
        # A comparison-input dependency is conservative even without a citation.
        with self.store.open() as state,state.db:
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?) ON CONFLICT DO NOTHING',(docs[1],sources[0]))
        before=module.audit(self.store)
        self.assertEqual(before['withdrawal_dependency_impact']['maximum_documents'],2)
        self.assertEqual(before['cross_scope_exact_text']['exact_text_groups'],1)
        self.assertEqual(before['cross_scope_exact_text']['documents'],2)
        self.store.lifecycle(self.ctx,dict(version=VERSION,target_id=sources[0],expected_revision='1',
            operation='withdraw',idempotency_key='audit-withdraw',reason='fixture'))
        with self.store.open() as state:
            self.assertFalse(self.store._document_allowed(state.db,self.ctx,docs[0]))
            self.assertFalse(self.store._document_allowed(state.db,self.ctx,docs[1]))
            self.assertTrue(self.store._document_allowed(state.db,self.ctx,docs[2]))
            # Withdrawal hides derived access; it does not erase revision history.
            self.assertEqual(state.db.execute('SELECT count(*) n FROM knowledge_revisions').fetchone()['n'],3)
