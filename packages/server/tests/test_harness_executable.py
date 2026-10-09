"""An app update may move the installed Codex CLI within ChatGPT.app."""
from pathlib import Path
import os
import tempfile
import unittest

from agenthub.processing.harness import resolve_executable
from agenthub.processing.harness_errors import HarnessError


class HarnessExecutableTests(unittest.TestCase):
    def test_relocated_chatgpt_cli_preserves_the_configured_app_boundary(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('AGENTNETWORK_TEST_EXECUTABLE_TMPDIR')) as root:
            resources=Path(root)/'ChatGPT.app'/'Contents'/'Resources'
            legacy=resources/'codex'
            current=resources/'codex-cli'/'CodexCLI.app'/'Contents'/'MacOS'/'codex'
            current.parent.mkdir(parents=True)
            current.write_text('#!/bin/sh\nexit 0\n')
            current.chmod(0o700)
            self.assertEqual(resolve_executable(str(legacy)),str(current))
            legacy.write_text('#!/bin/sh\nexit 0\n')
            legacy.chmod(0o700)
            self.assertEqual(resolve_executable(str(legacy)),str(legacy))

    def test_missing_other_executable_does_not_fall_back_to_path(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(HarnessError,'harness_unavailable'):
                resolve_executable(str(Path(root)/'other'/'codex'))


if __name__=='__main__': unittest.main()
