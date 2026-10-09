"""Canonicalize local Codex working directories without accepting remote URIs."""
from pathlib import Path
from urllib.parse import unquote, urlsplit


def local_cwd(value):
    if not isinstance(value,str) or not value:
        raise ValueError('command_cwd')
    if value.startswith('/'):
        return Path(value).resolve()
    uri=urlsplit(value)
    if uri.scheme!='file' or uri.netloc not in ('','localhost') or uri.query or uri.fragment:
        raise ValueError('command_cwd')
    path=Path(unquote(uri.path,errors='strict'))
    if not path.is_absolute():raise ValueError('command_cwd')
    return path.resolve()
