"""Server-side re-redaction of captured events before any persistence.

Clients redact before hashing and transport. The service does not trust that:
every typed capture is re-redacted with the same pinned detector before its
canonical payload, PostgreSQL rows or retained object are written. Differences
are recorded in the event's redaction manifest as field paths and rule ids only.
Identity fields cannot be rewritten, so a credential inside one is rejected.
"""
from agentclient.enterprise_capture import redact
from agentclient.general_contract import validate_event

MANIFEST_LIMIT = 1000
IDENTITY_FIELDS = ('version', 'connection', 'external_id', 'revision', 'source_type', 'project')


def redact_capture(value):
    """Return (stored payload, added manifest entries). Never returns values."""
    validate_event(value)
    safe, found = redact(value)
    if safe == value:
        return value, []
    if any(safe[key] != value[key] for key in IDENTITY_FIELDS):
        raise ValueError('credential_in_source_identity')
    added = [{'field': item['field'], 'reason': ('server_' + item['reason'])[:80]} for item in found]
    manifest = safe['redactions'] + added
    if len(manifest) > MANIFEST_LIMIT:
        manifest = manifest[:MANIFEST_LIMIT - 1] + [{'field': '$', 'reason': 'server_redactions_truncated'}]
    safe['redactions'] = manifest
    if safe['disposition'] == 'accepted':
        safe['disposition'] = 'redacted'
    validate_event(safe)
    # The stored representation must be a fixed point of the shared detector.
    if redact(safe)[0] != safe:
        raise ValueError('server_redaction_not_stable')
    return safe, added
