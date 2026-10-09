"""Installed architecture regressions: one authority and no client processing."""
import ast
from pathlib import Path
import unittest
import agentclient
import agenthub

class PackageBoundary(unittest.TestCase):
    def test_runtime_packages_do_not_ship_evaluation_fixtures(self):
        for module in (agentclient, agenthub):
            self.assertFalse(list(Path(module.__file__).parent.rglob('fixtures')))

    def test_no_client_processing_or_research_dependency(self):
        root=Path(agentclient.__file__).parent
        forbidden={'observer','state','harness','semantic','episode_pipeline','knowledge','control','viewer'}
        self.assertFalse(forbidden & {p.stem for p in root.glob('*.py')})
        for p in root.rglob('*.py'):
            for n in ast.walk(ast.parse(p.read_text())):
                if isinstance(n,ast.ImportFrom):self.assertFalse((n.module or '').startswith(('agenthub','vaelius_test_support')),str(p))
                elif isinstance(n,ast.Import):self.assertFalse(any(a.name.startswith(('agenthub','vaelius_test_support')) for a in n.names),str(p))

    def test_backend_has_no_research_runtime_dependency_or_legacy_entrypoints(self):
        root=Path(agenthub.__file__).parent
        for name in ('server','store','enterprise_server','enterprise_cli','cloud_seed','retrieval_experiments'):
            self.assertFalse((root/(name+'.py')).exists(),name)
        for p in root.rglob('*.py'):
            self.assertNotIn('vaelius_test_support',p.read_text(),str(p))

    def test_backend_state_cannot_open_sqlite_knowledge(self):
        from agenthub.processing.state import State
        from agenthub.enterprise import EnterpriseStore
        with self.assertRaisesRegex(RuntimeError,'authoritative PostgreSQL'):State('/must-not-create')
        with self.assertRaisesRegex(RuntimeError,'PostgreSQL authority'):EnterpriseStore('/must-not-create')

    def test_only_indexed_search_authorization_is_present(self):
        import inspect
        from agenthub.cloud_retrieval import HybridRetrievalMixin
        body=inspect.getsource(HybridRetrievalMixin._authorization_predicate)
        self.assertIn('agenthub.search_permissions',body)
        self.assertNotIn('permitted_sources',body)
        self.assertNotIn('permission_sql',body)
        self.assertNotIn("retrieval_authorization_shape",body)

    def test_canonical_processing_is_owned_by_backend(self):
        from agenthub.pipeline_pin import verify
        value=verify()
        self.assertTrue(any(name.startswith('agenthub.processing.') for name in value['modules']))
        self.assertTrue(set(n for n in value['modules'] if n.startswith('agentclient.'))<=
            {'agentclient.enterprise_contract','agentclient.general_contract','agentclient.cloud_contract','agentclient.cleaning'})
