"""Explicit mounted secret references; never discover or log credentials."""
from pathlib import Path
import stat
import os


def read_secret(path):
    path=Path(path).expanduser()
    if not path.is_absolute():raise ValueError('absolute_secret_file_required')
    with path.open() as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:
            raise ValueError('provider_credential_permissions')
        value=stream.read(16385).strip()
    if not value or len(value)>16384:raise ValueError('secret_file_bound')
    return value
