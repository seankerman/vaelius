"""`vaelius-client github link`: prove you own a GitHub account (docs/ACCESS_MODEL.md section 3).

The service runs the GitHub OAuth device flow for its GitHub App. This client only
shows the one-time code and the verification address, then asks the service to
poll. The device code and the GitHub token never reach this machine; the service
keeps only the numeric GitHub user id and current login.
"""
import time

ROUTE = '/enterprise/v3/identity/github'


def status(client):
    return client.request(ROUTE, {})


def unlink(client, principal=None):
    return client.request(ROUTE + '/unlink', {'principal': principal} if principal else {})


def link(client, *, notify, sleep=time.sleep, clock=time.monotonic, timeout=900):
    """Start the flow, show the code through `notify`, poll until it ends."""
    started = client.request(ROUTE + '/link/start', {})
    notify({'action': 'github_device_authorization', 'verification_uri': started['verification_uri'],
            'user_code': started['user_code'], 'expires_in': started['expires_in'],
            'message': 'Open %s and enter the code %s to link your GitHub account.'
                       % (started['verification_uri'], started['user_code'])})
    deadline = clock() + min(timeout, started['expires_in'])
    interval = started['interval']
    while clock() < deadline:
        sleep(interval)
        result = client.request(ROUTE + '/link/poll', {'flow': started['flow']})
        if result['status'] != 'pending':
            return result
        interval = result.get('interval', interval)
    return {'status': 'expired'}
