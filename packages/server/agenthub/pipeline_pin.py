"""Verify package-owned processing and the explicitly versioned client contract."""
import hashlib
from importlib import import_module
import json
from pathlib import Path

def verify():
    pin=json.loads((Path(__file__).parent/'CLIENT_PIPELINE_PIN.json').read_text())
    import agentclient
    if agentclient.__version__!=pin['client_version']:raise RuntimeError('client_package_pin_mismatch')
    for name,expected in pin['modules'].items():
        module=import_module(name)
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()!=expected:raise RuntimeError('canonical_pipeline_pin_mismatch:'+name)
    return pin
