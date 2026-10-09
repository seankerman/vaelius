"""Recovery must be reachable through the supported operator entry point."""
import contextlib,io,unittest
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
from agenthub.cloud_local import main

class RederivationCli(unittest.TestCase):
    def test_source_only_recovery_is_explicit_and_does_not_dispatch(self):
        store=SimpleNamespace(semantic_embedder=None)
        registry=SimpleNamespace(resolve=lambda _:store,_stores={})
        with patch('agenthub.cloud_local.runtime',return_value=({},registry)),\
                patch('agenthub.cloud_local.worker_config',return_value={}),\
                patch('agenthub.backend_worker.Worker') as worker,\
                patch('agenthub.cloud_local.receipt',side_effect=lambda p,c,v:{}),\
                contextlib.redirect_stdout(io.StringIO()):
            main(['retry','--profile','/synthetic','--job-id','held','--rederive'])
        worker.assert_called_once_with(store,{},live=False)
        worker.return_value.recover.assert_called_once_with('held',retain_validated=False,
            reviewed_stage_limit=None,rederive=True)
        worker.return_value.run.assert_not_called()

    def test_rederivation_flag_is_not_silently_ignored_by_another_command(self):
        with patch('agenthub.cloud_local.runtime') as runtime,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):main(['status','--profile','/synthetic','--rederive'])
        runtime.assert_not_called()
