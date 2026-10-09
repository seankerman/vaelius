"""Private transport/configuration directories; no knowledge database."""
from pathlib import Path

def private_dir(path):
    path=Path(path).expanduser()
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.chmod(0o700)
    return path
