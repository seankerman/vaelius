"""A real previous installed writer must respect current PostgreSQL denials."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from agenthub.enterprise import Denied
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES and os.environ.get('CLOUD_PREVIOUS_PYTHON'),
                     'explicit dedicated PostgreSQL fixture and previous installed build required')
class PreviousWriter(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store = CloudStore(self.store.home, self.dsn, 'acme')

    def tearDown(self):
        self.postgres_teardown()

    def test_actual_previous_writer_keeps_withdrawal_and_new_writes_on_one_authority(self):
        ctx = self.store.authenticate(self.tokens['alice'])
        source = self.store.ingest(ctx, {'version': VERSION, 'external_id': 'before-rollback',
            'session': 'rollback', 'turn': '1', 'project': 'maple', 'kind': 'Stop',
            'body': 'The synthetic Maple dataset is saved at /tmp/maple-before.csv.',
            'occurred_at': 12345, 'visibility': 'private'})['source_id']
        document = self.store.accept_reviewed_note(ctx, source, 'Before rollback',
            'The synthetic Maple dataset is saved at /tmp/maple-before.csv.')['document_id']
        self.store.lifecycle(ctx, {'version': VERSION, 'target_id': source,
            'expected_revision': '1', 'idempotency_key': 'withdraw-before-rollback',
            'operation': 'withdraw', 'reason': 'Synthetic owner withdrawal before code rollback'})
        previous = Path(os.environ['CLOUD_PREVIOUS_PYTHON']).absolute()
        code = '''import json,sys
from agenthub.cloud_runtime import CloudStore
from agenthub.backend_ops import build_identity
from agenthub.enterprise import Denied
from agentclient.enterprise_contract import VERSION
v=json.load(sys.stdin);s=CloudStore(v['home'],v['dsn'],'acme');ctx=s.authenticate(v['token'])
denied=False
try:s.detail(ctx,v['withdrawn_document'])
except Denied:denied=True
if not denied:raise AssertionError('old writer resurrected withdrawn evidence')
source=s.ingest(ctx,{'version':VERSION,'external_id':'during-real-code-rollback',
 'session':'rollback','turn':'2','project':'maple','kind':'Stop',
 'body':'The synthetic Maple rollback dataset is saved at /tmp/maple-rollback.csv.',
 'occurred_at':12346,'visibility':'private'})['source_id']
document=s.accept_reviewed_note(ctx,source,'During real code rollback',
 'The synthetic Maple rollback dataset is saved at /tmp/maple-rollback.csv.')['document_id']
print(json.dumps({'build':build_identity()['build_ids'],'denied':denied,
 'source':source,'document':document,'detail':s.detail(ctx,document)}))
'''
        result = subprocess.run([str(previous), '-I', '-c', code], cwd='/tmp', text=True,
            input=json.dumps({'home': str(self.store.home), 'dsn': self.dsn,
                'token': self.tokens['alice'], 'withdrawn_document': document}),
            capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        proof = json.loads(result.stdout)
        from agenthub.backend_ops import build_identity
        self.assertNotEqual(proof['build']['agenthub'], build_identity()['build_ids']['agenthub'])
        self.assertTrue(proof['denied'])
        self.assertEqual(self.store.detail(ctx, proof['document'])['revision'], proof['detail']['revision'])
        self.store.lifecycle(ctx, {'version': VERSION, 'target_id': proof['source'],
            'expected_revision': '1', 'idempotency_key': 'withdraw-after-rollback',
            'operation': 'withdraw', 'reason': 'Synthetic withdrawal after rolling forward'})
        with self.assertRaises(Denied):
            self.store.detail(ctx, proof['document'])
        with self.assertRaises(Denied):
            self.store.detail(ctx, document)
        self.assertFalse(list(self.store.home.rglob('*.sqlite')))


if __name__ == '__main__':
    unittest.main()
