"""Real local provisioning with separate request/operator credentials."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from agenthub.cloud_profile import setup_profile
from agenthub.postgres import connect, TenantRegistry, Denied

SERVICES=os.environ.get('AGENTNETWORK_PG_SERVICES')


@unittest.skipUnless(SERVICES,'explicit local PostgreSQL services manifest required')
class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='cloud-profile-test-')
        cls.profile=Path(cls.temp.name)/'profile'
        cls.namespace='cloud_test_'+uuid.uuid4().hex[:10]
        cls.bootstrap=json.loads(Path(SERVICES).read_text())['admin_dsn']
        cls.receipt=setup_profile(cls.profile,SERVICES,namespace=cls.namespace)
        cls.operator=json.loads((cls.profile/'operator.json').read_text())
        cls.runtime=json.loads((cls.profile/'runtime.json').read_text())

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql
        with connect(cls.bootstrap,autocommit=True) as db:
            for name in cls.receipt['databases']:
                db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in [cls.operator['control_role']]+[t['role'] for t in cls.operator['tenants'].values()]:
                db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        cls.temp.cleanup()

    def test_application_roles_and_router_cannot_administer(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        acme=self.operator['tenants']['acme'];bravo=self.operator['tenants']['bravo']
        with self.assertRaises(psycopg.OperationalError):
            connect(make_conninfo(acme['dsn'],dbname=bravo['database']))
        with connect(acme['dsn']) as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):db.execute('CREATE TABLE forbidden(id INT)')
        with connect(self.runtime['control_dsn']) as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):db.execute("UPDATE cloud_tenants SET active=0 WHERE id='acme'")
        with connect(self.runtime['control_dsn']) as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):db.execute('CREATE TABLE forbidden(id INT)')
        registry=TenantRegistry(self.runtime['control_dsn'],self.profile/'server-state')
        token=(self.profile/'credentials/acme-alice.token').read_text().strip()
        self.assertEqual(registry.store_for_token(token).authenticate(token)['actor'],'alice')
        with self.assertRaises(Denied):registry.store_for_token('unknown-token')

    def test_repeat_setup_preserves_credentials_and_never_seeds_sources(self):
        before={path.name:path.read_bytes() for path in (self.profile/'credentials').glob('*.token')}
        again=setup_profile(self.profile,SERVICES,namespace=self.namespace)
        self.assertEqual(again,self.receipt)
        self.assertEqual(before,{path.name:path.read_bytes() for path in (self.profile/'credentials').glob('*.token')})
        for item in self.operator['tenants'].values():
            with connect(item['dsn']) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM backend_connections').fetchone()[0],1)
        self.assertEqual(self.runtime['provider_mode'],'off')
        for path in [self.profile/'runtime.json',self.profile/'operator.json',*(self.profile/'credentials').glob('*.token')]:
            self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(list(self.profile.rglob('*.sqlite')),[])


if __name__=='__main__':unittest.main()
