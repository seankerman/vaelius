"""vaelius-client owner self-service: mine, forget, sharing (with a selection), share, unshare.

The service is replaced by a recorder; no network, no service state.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TOKEN = 'a' * 32


class Service:
    def __init__(self, preview_token=TOKEN):
        self.calls = []
        self.token = preview_token

    def request(self, path, value=None):
        self.calls.append((path, value))
        if path == '/enterprise/v3/my-data':
            return {'principal': 'alice', 'view': 'sources', 'items': [], 'more': False, 'next_cursor': None}
        if path == '/enterprise/v3/my-data/grants':
            return {'issued': [], 'received': []}
        if path == '/enterprise/v3/my-data/revoke':
            return {'operation': 'revoke', 'revoked': 1}
        dry = value.get('confirm') is False
        return {'operation': {'forget': 'forget', 'sharing': 'change_sharing', 'grant': 'grant'}[path.rsplit('/', 1)[1]],
                'dry_run': dry, 'confirm_token': self.token if dry else None}


class OwnerCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / 'config.json').write_text(json.dumps({'projects': {'/fictional/project': 'demo'},
            'knowledge_backend': {'mode': 'enterprise_local', 'capture_version': 'enterprise-local-2',
                                  'capture_owner': 'transcript', 'api_version': 'cloud-local-1'}}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, service=None):
        from agentclient import cli
        service = service or Service()
        with patch('agentclient.transport.enterprise_client', return_value=service), \
                patch('builtins.print') as printed:
            cli.main(['--home', str(self.home), *argv])
        return service.calls, json.loads(printed.call_args[0][0])

    def test_forget_previews_by_default_and_applies_only_with_the_token_or_yes(self):
        calls, output = self.run_cli('forget', '--session', 'current', '--project', 'demo')
        self.assertEqual(calls, [('/enterprise/v3/my-data/forget',
                                  {'session': 'current', 'project': 'demo', 'confirm': False})])
        self.assertTrue(output['dry_run'])
        self.assertIn('--confirm ' + TOKEN, output['next'])
        calls, _ = self.run_cli('forget', '--session', 'current', '--project', 'demo', '--confirm', TOKEN)
        self.assertEqual(calls[-1][1]['confirm'], TOKEN)
        calls, output = self.run_cli('forget', '--since', '2026-10-01', '--yes')
        self.assertEqual([value['confirm'] for _, value in calls], [False, TOKEN])
        self.assertFalse(output['applied']['dry_run'])
        # Nothing selected is refused before contacting the service.
        service = Service()
        with self.assertRaises(SystemExit):
            self.run_cli('forget', service=service)
        self.assertEqual(service.calls, [])

    def test_current_session_defaults_to_this_folders_project(self):
        with patch('agentclient.hooks.resolve_scope', return_value={'project': 'demo', 'capture': 'on'}):
            calls, _ = self.run_cli('forget', '--session', 'current')
        self.assertEqual(calls[0][1]['project'], 'demo')

    def test_sharing_with_a_selection_changes_sources_not_the_personal_setting(self):
        calls, _ = self.run_cli('sharing', '--set', 'team', '--originals', 'private', '--source', 's-1', '--yes')
        self.assertEqual([path for path, _ in calls], ['/enterprise/v3/my-data/sharing'] * 2)
        self.assertEqual(calls[0][1], {'source_ids': ['s-1'], 'sharing': 'team', 'originals': 'private',
                                       'confirm': False})

    def test_share_unshare_and_mine(self):
        calls, _ = self.run_cli('share', '--session', 'abc', '--with-project', 'birch', '--originals')
        self.assertEqual(calls, [('/enterprise/v3/my-data/grant', {'scope': 'session', 'ids': ['abc'],
            'grantee_kind': 'project', 'grantee': 'birch', 'originals': True, 'confirm': False})])
        calls, output = self.run_cli('unshare', '--source', 's-1', '--with-principal', 'bob')
        self.assertEqual(calls, [('/enterprise/v3/my-data/revoke', {'scope': 'source', 'ids': ['s-1'],
            'grantee_kind': 'principal', 'grantee': 'bob', 'confirm': True})])
        calls, _ = self.run_cli('share', '--list')
        self.assertEqual(calls, [('/enterprise/v3/my-data/grants', {})])
        calls, _ = self.run_cli('mine', '--sessions', '--sharing', 'team')
        self.assertEqual(calls[0][1], {'limit': 50, 'view': 'sessions', 'state': 'active', 'sharing': 'team'})
        with self.assertRaises(SystemExit):
            self.run_cli('share', '--session', 'abc')

    def test_owner_responses_are_validated_at_the_transport_boundary(self):
        from agentclient.cloud_contract import validate_response
        preview = {'operation': 'forget', 'dry_run': True, 'confirm_token': TOKEN, 'sources': 2}
        self.assertEqual(validate_response('/enterprise/v3/my-data/forget', preview), preview)
        for bad in (dict(preview, dry_run='yes'), dict(preview, operation='other'),
                    dict(preview, confirm_token='short'), {'operation': 'forget', 'dry_run': True}):
            with self.assertRaises(ValueError):
                validate_response('/enterprise/v3/my-data/forget', bad)
        listing = {'principal': 'alice', 'view': 'sources', 'items': [], 'more': False}
        self.assertEqual(validate_response('/enterprise/v3/my-data', listing), listing)
        with self.assertRaises(ValueError):
            validate_response('/enterprise/v3/my-data', dict(listing, items='none'))
        with self.assertRaises(ValueError):
            validate_response('/enterprise/v3/my-data/grants', {'issued': []})
        with self.assertRaises(ValueError):
            validate_response('/enterprise/v3/my-data/revoke', {'operation': 'revoke', 'revoked': 'all'})


if __name__ == '__main__':
    unittest.main()
