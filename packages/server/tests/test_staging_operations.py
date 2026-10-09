"""Frozen operator contracts for registered, non-fixture tenant identifiers."""
from contextlib import redirect_stdout
import io
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from agenthub.cloud_local import main, status
from agenthub.cloud_container import worker


class OperatorTenantTests(unittest.TestCase):
    def test_status_includes_registered_org_and_preserves_disabled_state(self):
        registry = MagicMock()
        connection = MagicMock()
        connection.execute.return_value = [
            {'id': 'org_72ce', 'active': 1}, {'id': 'org_archived', 'active': 0}]
        @contextmanager
        def control():
            yield connection
        registry.open_control = control
        store = registry.resolve.return_value
        state = store.open.return_value.__enter__.return_value
        state.db.execute.return_value.fetchone.return_value = [0]
        state.db.execute.return_value.__iter__.return_value = iter([])
        with patch('agenthub.cloud_local.runtime', return_value=({}, registry)), \
             patch('agenthub.backend_worker.Worker.status', return_value={}), \
             patch('agenthub.cloud_ops.Meter.status', return_value={}):
            result = status('/tmp/owned-profile')
        self.assertEqual(set(result['tenants']), {'org_72ce', 'org_archived'})
        self.assertFalse(result['tenants']['org_archived']['active'])
        registry.resolve.assert_called_once_with('org_72ce')

    def test_registered_third_tenant_reaches_readiness_route(self):
        registry = MagicMock()
        with patch('agenthub.cloud_local.runtime', return_value=({}, registry)), \
                patch('agenthub.cloud_local.receipt', side_effect=lambda p, c, v: v), \
                redirect_stdout(io.StringIO()) as out:
            main(['readiness', '--profile', '/tmp/synthetic-third-tenant',
                '--tenant', 'org_72ce'])
        registry.resolve.assert_called_once_with('org_72ce')
        registry.resolve.return_value.require_ready.assert_called_once()
        self.assertIn('org_72ce', out.getvalue())

    def test_installed_worker_command_routes_arbitrary_registered_tenant(self):
        with patch('agenthub.cloud_container._mounts', return_value=[]), \
                patch('agenthub.cloud_container._run', return_value='{"completed":0}') as run:
            result = worker('podman', 'synthetic-image', '/tmp/owned-profile',
                tenant='org_72ce', max_jobs=1, max_seconds=1)
        self.assertEqual(result['completed'], 0)
        self.assertIn('org_72ce', run.call_args.args[0])

    def test_invalid_tenant_refused_before_command_dispatch(self):
        with patch('agenthub.cloud_container._run') as run:
            for tenant in ('', '../org', 'org\n--live'):
                with self.assertRaises(ValueError):
                    worker('podman', 'synthetic-image', '/tmp/owned-profile', tenant=tenant)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
