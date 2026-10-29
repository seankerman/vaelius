"""`vaelius-client github link`: the client shows the code and polls; it never sees a GitHub token."""
import unittest

from agentclient import github_link
from agentclient.cloud_contract import validate_response


class FakeService:
    def __init__(self, polls):
        self.polls = list(polls)
        self.requests = []

    def request(self, path, data):
        self.requests.append((path, data))
        if path.endswith('/link/start'):
            value = {'flow': 'a' * 32, 'user_code': 'WXYZ-0001', 'verification_uri': 'https://github.test/login/device',
                     'expires_in': 900, 'interval': 5}
        elif path.endswith('/link/poll'):
            value = self.polls.pop(0)
        elif path.endswith('/unlink'):
            value = {'provider': 'github', 'principal': 'bob', 'unlinked': True}
        else:
            value = {'provider': 'github', 'linked': False}
        return validate_response(path, value)


class GitHubLinkTests(unittest.TestCase):
    def test_link_shows_the_code_and_polls_until_linked(self):
        service = FakeService([{'status': 'pending', 'interval': 10},
                               {'status': 'linked', 'provider': 'github', 'linked': True, 'user_id': 103,
                                'login': 'bob-gh', 'linked_at': 1.0, 'github_groups': 2}])
        shown, slept = [], []
        result = github_link.link(service, notify=shown.append, sleep=slept.append, clock=lambda: 0)
        self.assertEqual((result['status'], result['user_id']), ('linked', 103))
        self.assertIn('WXYZ-0001', shown[0]['message'])
        self.assertEqual(slept, [5, 10])
        self.assertEqual([path for path, _ in service.requests],
                         ['/enterprise/v3/identity/github/link/start'] + ['/enterprise/v3/identity/github/link/poll'] * 2)
        # Only the opaque flow id is sent back; never a login.
        self.assertEqual(service.requests[1][1], {'flow': 'a' * 32})

    def test_denied_or_expired_flow_ends_the_loop(self):
        for status in ('denied', 'expired'):
            result = github_link.link(FakeService([{'status': status}]), notify=lambda _: None,
                                      sleep=lambda _: None, clock=lambda: 0)
            self.assertEqual(result, {'status': status})

    def test_responses_are_validated(self):
        with self.assertRaises(ValueError):
            validate_response('/enterprise/v3/identity/github/link/poll', {'status': 'linked', 'token': 'x'})
        with self.assertRaises(ValueError):
            validate_response('/enterprise/v3/identity/github/link/start', {'flow': 'x', 'user_code': 'y',
                'verification_uri': 'http://github.test', 'expires_in': 900, 'interval': 5})
        self.assertEqual(github_link.unlink(FakeService([]))['unlinked'], True)


if __name__ == '__main__':
    unittest.main()
