"""Frozen synthetic identity cases. No provider/customer account calls."""
import json
from pathlib import Path
import unittest

from joserfc import jwt
from joserfc.jwk import RSAKey

from agenthub.cloud_identity import IdentityError, OIDCBroker, normalize_scim


FIXTURE = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_identity_v1.json').read_text())


class OIDCContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSAKey.generate_key(parameters={'kid': 'first'})
        cls.second = RSAKey.generate_key(parameters={'kid': 'second'})

    def setUp(self):
        self.now = 1800000000
        self.calls = 0
        self.keys = [self.key.as_dict(private=False)]
        self.issuer = FIXTURE['issuers'][0]
        def fetch(url):
            self.calls += 1
            return {'keys': self.keys}
        self.broker = OIDCBroker(issuer=self.issuer, client_id='knowledge-plugin',
            authorization_endpoint=self.issuer + '/protocol/openid-connect/auth',
            token_endpoint=self.issuer + '/protocol/openid-connect/token',
            jwks_uri=self.issuer + '/protocol/openid-connect/certs',
            redirect_uri='http://127.0.0.1:59001/callback', fetch_json=fetch,
            clock=lambda: self.now)

    def token(self, key=None, **values):
        claims = {'iss': self.issuer, 'sub': 'same-external-id', 'aud': 'knowledge-plugin',
                  'exp': self.now + 120, 'iat': self.now, 'nonce': 'expected-nonce'}
        claims.update(values)
        key = key or self.key
        return jwt.encode({'alg': 'RS256', 'kid': key.kid}, claims, key)

    def test_verified_subject_not_email_and_federation_claim(self):
        value = self.broker.verify(self.token(email='alice@example.invalid',
            identity_provider='company'), nonce='expected-nonce')
        self.assertEqual(value['subject'], 'same-external-id')
        self.assertEqual(value['provider'], 'company')
        self.assertNotIn('email', value)

    def test_wrong_claims_and_nonce_fail_closed(self):
        for values in ({'iss': FIXTURE['issuers'][1]}, {'aud': 'other'},
                       {'exp': self.now - 1}, {'nbf': self.now + 100},
                       {'iat': self.now + 100}, {'sub': ''}, {'nonce': 'wrong'}):
            with self.subTest(values=values), self.assertRaises(IdentityError):
                self.broker.verify(self.token(**values), nonce='expected-nonce')

    def test_unknown_key_refresh_is_bounded_and_rotated_key_works(self):
        self.broker.verify(self.token())
        self.keys.append(self.second.as_dict(private=False))
        self.now += 6
        self.broker.verify(self.token(self.second))
        self.assertEqual(self.calls, 2)
        unknown = RSAKey.generate_key(parameters={'kid': 'attacker'})
        for _ in range(8):
            with self.assertRaises(IdentityError):
                self.broker.verify(self.token(unknown))
        self.assertEqual(self.calls, 2)

    def test_key_endpoint_failure_does_not_amplify_refresh_requests(self):
        calls=[]
        def unavailable(url):
            calls.append(url)
            raise RuntimeError('synthetic endpoint outage')
        self.broker.fetch_json=unavailable
        for _ in range(10):
            with self.assertRaises(IdentityError):self.broker.verify(self.token())
        self.assertEqual(len(calls),1)
        self.now+=6
        with self.assertRaises(IdentityError):self.broker.verify(self.token())
        self.assertEqual(len(calls),2)

    def test_pkce_and_exact_callback(self):
        request = self.broker.begin()
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(request['url']).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertGreaterEqual(len(request['verifier']), 43)
        self.assertEqual(query['redirect_uri'], [self.broker.redirect_uri])
        self.assertEqual(query['nonce'], [request['nonce']])
        self.assertEqual(query['state'], [request['state']])
        with self.assertRaises(IdentityError):
            self.broker.exchange('code', request, callback_uri='http://127.0.0.1:59001/other')

    def test_unsigned_invalid_or_oversized_tokens_denied(self):
        for token in ('eyJhbGciOiJub25lIn0.eyJzdWIiOiJhbGljZSJ9.', 'bad', 'x' * 17000):  # gitleaks:allow -- unsigned rejection input
            with self.assertRaises(IdentityError):
                self.broker.verify(token)

    def test_remote_insecure_or_token_directed_endpoints_denied(self):
        with self.assertRaises(ValueError):
            OIDCBroker(issuer='http://example.invalid', client_id='x',
                authorization_endpoint='http://example.invalid/auth',
                token_endpoint='http://example.invalid/token', jwks_uri='http://example.invalid/keys',
                redirect_uri='http://127.0.0.1:9001/callback')


class SCIMNormalizationTests(unittest.TestCase):
    def test_minimum_user_group_and_explicit_unsupported_fields(self):
        user = normalize_scim('Users', {'id': 'directory-alice', 'userName': 'alice', 'active': True})
        self.assertEqual(user['kind'], 'user')
        self.assertEqual(user['external_id'], 'directory-alice')
        group = normalize_scim('Groups', {'id': 'team', 'displayName': 'Team',
            'members': [{'value': 'directory-alice'}]})
        self.assertEqual(group['members'], ['directory-alice'])
        for value in ({'id': 'alice', 'active': 'true'},
                      {'id': 'alice', 'password': 'never'}, {'id': 'alice', 'roles': ['admin']}):
            with self.assertRaises(ValueError): normalize_scim('Users', value)
        with self.assertRaises(ValueError): normalize_scim('Groups', {'id':'g','members':[{'value':'u','$ref':'evil'}]})


if __name__ == '__main__': unittest.main()
