"""Frozen scoped rotation controls on disposable PostgreSQL."""
import secrets,unittest
from test_cloud_postgres import PostgresFixture,SERVICES
from agenthub.enterprise import Denied

@unittest.skipUnless(SERVICES,'explicit disposable PostgreSQL required')
class CredentialRenewal(PostgresFixture,unittest.TestCase):
    def setUp(self):self.postgres_setup();self.addCleanup(self.postgres_teardown)
    def test_new_token_preserves_identity_and_old_token_cannot_rotate_twice(self):
        token=secrets.token_urlsafe(48)
        self.store.rotate_credential(self.ctx,replacement_token=token)
        current=self.store.authenticate(token)
        for k in ('tenant','principal','actor','enrollment','actions'):self.assertEqual(current[k],self.ctx[k])
        with self.assertRaises(Denied):self.store.authenticate(self.tokens['alice'])
        with self.assertRaises(Denied):self.store.rotate_credential(self.ctx,replacement_token=secrets.token_urlsafe(48))
    def test_inactive_and_expired_credentials_cannot_renew(self):
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE enterprise_credentials SET expires_at=0')
        with self.assertRaises(Denied):self.store.rotate_credential(self.ctx,replacement_token=secrets.token_urlsafe(48))

    def test_http_client_rotation_and_parallel_stale_context(self):
        from concurrent.futures import ThreadPoolExecutor
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        store=self.store
        class Registry:
            def store_for_token(self,token):return store
        client=TestClient(create_app(Registry(),allowed_hosts=['testserver']))
        header={'Authorization':'Bearer '+self.tokens['alice']}
        before=client.get('/enterprise/v3/auth/credential',headers=header)
        self.assertEqual(before.status_code,200)
        token=secrets.token_urlsafe(48)
        after=client.post('/enterprise/v3/auth/renew',headers=header,json={'replacement_token':token})
        self.assertEqual(after.status_code,200)
        for k in ('tenant','principal','actor','enrollment','actions'):
            self.assertEqual(before.json()[k],after.json()[k])
        self.assertNotIn(token,after.text)
        self.assertNotEqual(client.get('/enterprise/v3/auth/credential',headers=header).status_code,200)
        current=store.authenticate(token)
        def rotate(_):
            try:store.rotate_credential(current,replacement_token=secrets.token_urlsafe(48));return True
            except Denied:return False
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sum(pool.map(rotate,range(2))),1)
