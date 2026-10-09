"""One finite, resumable historical import campaign for an isolated local profile."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import time

from agentclient.enterprise_backfill import write_private
from agenthub.historical_backfill import run


def campaign(profile, credential_file, manifest_file, home, *, max_hours=12, runner=run):
    if type(max_hours) is not int or not 1 <= max_hours <= 12:
        raise ValueError("invalid_campaign_time_bound")
    target = Path(home).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.stat().st_mode & 0o077:
        raise ValueError("backfill_home_permissions")
    fd = os.open(target / 'campaign.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('historical_campaign_already_running') from None
        deadline = time.monotonic() + max_hours * 3600
        totals = {'status': 'partial', 'rounds': 0, 'lines_advanced': 0,
                  'events_queued': 0, 'events_acknowledged': 0,
                  'remaining_sessions': None, 'provider_calls': 0,
                  'transient_retries': 0,
                  'private_source_text_in_report': False}
        for _ in range(24):
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                totals['status'] = 'time_bound'
                break
            try:
                result = runner(profile, credential_file, manifest_file, target,
                                max_slices=500, max_total_seconds=min(3600, remaining))
            except Exception as exc:
                totals['status'] = 'failed'
                totals['error_type'] = type(exc).__name__
                write_private(target / 'campaign-status.json', totals)
                raise
            totals['rounds'] += 1
            for key in ('lines_advanced', 'events_queued', 'events_acknowledged'):
                totals[key] += result.get(key, 0)
            totals['remaining_sessions'] = result.get('remaining_sessions')
            totals['status'] = result['status']
            totals['last_error_type'] = result.get('last_error_type')
            if result['status']=='transport_blocked':
                # The canonical outbox retains every unacknowledged event.
                # A short PostgreSQL lock collision is safe to retry in this
                # same finite campaign; credential/policy/other transport
                # failures remain explicit operator stops.
                transient = (result.get('last_error_type')=='LockNotAvailable' or
                             (totals['transient_retries']>0 and result.get('last_error_type') is None))
                if transient and totals['transient_retries']<8:
                    totals['transient_retries'] += 1
                    write_private(target / 'campaign-status.json', totals)
                    time.sleep(min(30,2**min(totals['transient_retries'],5)))
                    continue
                break
            if result['status']=='complete':
                break
            if not any(result.get(key, 0) for key in
                       ('lines_advanced', 'events_queued', 'events_acknowledged')):
                totals['status'] = 'no_progress'
                break
            write_private(target / 'campaign-status.json', totals)
        write_private(target / 'campaign-status.json', totals)
        return totals
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--credential-file', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--home', required=True)
    parser.add_argument('--max-hours', type=int, default=12)
    args = parser.parse_args(argv)
    print(json.dumps(campaign(args.profile, args.credential_file,
                              args.manifest, args.home, max_hours=args.max_hours), sort_keys=True))


if __name__ == '__main__':
    main()
