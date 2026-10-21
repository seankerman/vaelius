"""Per-principal and per-tenant write limits for content-creating requests.

Counters live in the tenant's PostgreSQL schema, so every API process enforces the
same fixed window. A rejected write is not counted and is answered with HTTP 429
and Retry-After; clients keep the item and retry later. The first rejection in a
window is audited once, so an abusive client cannot amplify audit writes.
Withdrawal, deletion and other removal paths are never limited.
"""
import math
import time

DEFAULT_LIMITS = {
    'enabled': True,
    'window_seconds': 60,
    # One person's capture plus a historical backfill stays well below this.
    'principal_writes_per_window': 600,
    # Above the 50 MiB original-document upload bound, so one upload always fits.
    'principal_bytes_per_window': 64 * 1024 * 1024,
    'tenant_writes_per_window': 6000,
    'tenant_bytes_per_window': 512 * 1024 * 1024,
}
# Upper bounds for operator-configured limits: high enough for any real deployment,
# low enough that a typo (an extra few zeros) is refused rather than silently
# disabling the limit.
MAX_WRITES_PER_WINDOW = 1_000_000
MAX_BYTES_PER_WINDOW = 64 * 1024 ** 3
_RETENTION_SECONDS = 3600


class RateLimited(Exception):
    """Not a ValueError: the API maps it to 429, never to an invalid request."""

    def __init__(self, scope, retry_after):
        super().__init__(scope)
        self.scope = scope
        self.retry_after = retry_after


class WriteTooLarge(Exception):
    """One write larger than a whole window's byte budget can never succeed."""


def limits_from_settings(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
        raise ValueError('runtime_ingest_limits_configuration')
    limits = dict(DEFAULT_LIMITS, **value)
    if type(limits['enabled']) is not bool:
        raise ValueError('runtime_ingest_limits_configuration')
    if type(limits['window_seconds']) is not int or not 1 <= limits['window_seconds'] <= 3600:
        raise ValueError('runtime_ingest_limits_configuration')
    for key in DEFAULT_LIMITS:
        if key.endswith('_window'):
            ceiling = MAX_WRITES_PER_WINDOW if '_writes_' in key else MAX_BYTES_PER_WINDOW
            # type() rather than isinstance(): booleans are not limits.
            if type(limits[key]) is not int or not 1 <= limits[key] <= ceiling:
                raise ValueError('runtime_ingest_limits_configuration')
    for kind in ('writes', 'bytes'):
        if limits['principal_' + kind + '_per_window'] > limits['tenant_' + kind + '_per_window']:
            raise ValueError('runtime_ingest_limits_principal_exceeds_tenant')
    return limits


class _Rejected(Exception):
    pass


def _count(db, scopes, window, size):
    for kind, scope, write_limit, byte_limit in scopes:
        # The conditional upsert both checks and counts in one statement, so
        # concurrent API processes cannot overshoot the window together.
        row = db.execute('''INSERT INTO ingest_rate_windows(scope,window_start,writes,bytes)
            VALUES(?,?,1,?) ON CONFLICT(scope,window_start) DO UPDATE
            SET writes=ingest_rate_windows.writes+1,bytes=ingest_rate_windows.bytes+excluded.bytes
            WHERE ingest_rate_windows.writes+1<=? AND ingest_rate_windows.bytes+excluded.bytes<=?
            RETURNING (xmax=0) AS inserted''', (scope, window, size, write_limit, byte_limit)).fetchone()
        if row is None:
            return kind, scope
        if row['inserted']:
            db.execute('DELETE FROM ingest_rate_windows WHERE scope=? AND window_start<?',
                       (scope, window - _RETENTION_SECONDS))
    return None


def check(store, ctx, size, *, now=None):
    """Count one content-creating write of `size` bytes, or raise RateLimited."""
    limits = getattr(store, 'ingest_limits', None)
    if not limits or not limits.get('enabled') or not getattr(store, 'dsn', None):
        return None
    if type(size) is not int or size < 0:
        raise ValueError('write_size')
    now = time.time() if now is None else now
    width = limits['window_seconds']
    window = int(now // width) * width
    retry_after = max(1, math.ceil(window + width - now))
    scopes = (('principal', 'principal:' + ctx['tenant'] + ':' + ctx['actor'],
               limits['principal_writes_per_window'], limits['principal_bytes_per_window']),
              ('tenant', 'tenant:' + ctx['tenant'],
               limits['tenant_writes_per_window'], limits['tenant_bytes_per_window']))
    for _, _, _, byte_limit in scopes:
        if size > byte_limit:
            raise WriteTooLarge()
    limited = None
    try:
        with store.open() as state, state.db:
            limited = _count(state.db, scopes, window, size)
            if limited:
                # Leave no partial count from an earlier scope of this request.
                raise _Rejected()
    except _Rejected:
        pass
    if limited:
        kind, scope = limited
        with store.open() as state, state.db:
            first = state.db.execute('''UPDATE ingest_rate_windows SET limited=1
                WHERE scope=? AND window_start=? AND limited=0 RETURNING 1''', (scope, window)).fetchone()
            if first:
                state.db.execute('''INSERT INTO enterprise_audit(tenant,actor,operation,disposition,target_id,
                    request_id,policy_version,created) VALUES(?,?,?,?,?,?,?,?)''',
                    (ctx['tenant'], ctx['actor'], 'ingest_rate_limit', 'rejected_' + kind,
                     None, ctx.get('request_id'), None, now))
        raise RateLimited(kind, retry_after)
    return {'window_start': window, 'retry_after': retry_after}
