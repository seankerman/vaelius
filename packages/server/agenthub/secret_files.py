"""Explicit mounted secret references; never discover or log credentials."""
from pathlib import Path
import stat
import os
import threading
import time


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


class MountedSecret:
    """A mounted secret file that picks up rotation without a restart.

    Orchestrators rotate a mounted secret by replacing the file (Kubernetes swaps
    the `..data` symlink; the Secrets Store CSI driver rewrites the file), so the
    resolved inode, size or mtime changes. The file is stat()ed at most once per
    `check_seconds` and only re-read when that signature changes. A failed or
    partial re-read keeps the last valid value; the next check retries. The value
    is never logged and never appears in repr().
    """
    def __init__(self,path,*,check_seconds=30.0,clock=time.monotonic):
        if type(check_seconds) not in (int,float) or not 0<check_seconds<=3600:raise ValueError('secret_check_interval')
        self.path=Path(path).expanduser();self.check_seconds=check_seconds;self._clock=clock
        self._lock=threading.Lock()
        self._signature=self._stat()
        self._value=read_secret(self.path) # Startup fails closed on a missing or unsafe file.
        self._checked=clock()

    def _stat(self):
        info=os.stat(self.path)
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)

    def value(self):
        if self._clock()-self._checked<self.check_seconds:return self._value
        with self._lock:
            if self._clock()-self._checked<self.check_seconds:return self._value
            self._checked=self._clock()
            try:
                signature=self._stat()
                if signature!=self._signature:
                    self._value=read_secret(self.path);self._signature=signature
            except (OSError,ValueError):
                pass # Rotation in progress or unsafe replacement: keep serving the last valid value.
            return self._value

    def __repr__(self):
        return 'MountedSecret(path=%r)'%str(self.path)
