import ipaddress
import json
from pathlib import Path
import stat
import urllib.parse
import urllib.request


def backend_endpoint(backend):
    """Explicit operator endpoint. Default stays loopback; hosted requires HTTPS."""
    url=backend['url'];p=urllib.parse.urlsplit(url)
    if p.username or p.password or p.path not in ('','/') or p.query or p.fragment or not p.hostname:
        raise ValueError('invalid_backend_endpoint')
    transport=backend.get('transport','loopback')
    if transport=='https':
        if p.scheme!='https':raise ValueError('hosted_backend_requires_https')
    elif transport=='loopback':
        try:local=ipaddress.ip_address(p.hostname).is_loopback
        except ValueError:local=False
        if p.scheme!='http' or not local:raise ValueError('explicit_loopback_endpoint_required')
    else:raise ValueError('unknown_backend_transport')
    return url.rstrip('/')


class LoopbackTransport:
    """No redirects/proxy fallback; HTTPS is an explicit operator selection."""
    def __init__(self, url: str, token: str, timeout=1.5, *, transport='loopback'):
        url=backend_endpoint({'url':url,'transport':transport})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("Redirects are forbidden")
        self.url, self.token = url.rstrip("/"), token
        self.timeout=timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())



class EnterpriseLocal(LoopbackTransport):
    """Private loopback transport; no legacy publication route or proxy fallback."""
    def __init__(self, url, credential_file, timeout=1.5, *, transport='loopback'):
        credential=Path(credential_file).expanduser().resolve()
        mode=stat.S_IMODE(credential.stat().st_mode)
        if mode & 0o077:
            raise ValueError("enterprise_credential_permissions")
        token=credential.read_text().strip()
        if not token or len(token)>256:raise ValueError("invalid_enterprise_credential")
        super().__init__(url,token,timeout,transport=transport)

    def request(self, path: str, data=None):
        from agentclient.enterprise_contract import validate_response
        if not path.startswith(("/enterprise/v1/", "/enterprise/v2/", "/enterprise/v3/")) or ".." in path or "?" in path or "#" in path:
            raise ValueError("invalid_enterprise_path")
        if path.startswith('/enterprise/v2/'):
            from agentclient.general_contract import validate_response
        if path.startswith('/enterprise/v3/'):
            from agentclient.cloud_contract import validate_response
        payload=json.dumps(data).encode() if data is not None else None
        req=urllib.request.Request(self.url+path,data=payload,headers={
            "Authorization":"Bearer "+self.token,"Content-Type":"application/json"})
        with self.opener.open(req,timeout=self.timeout) as response:
            return validate_response(path,json.load(response))


def enterprise_client(config, *, timeout=1.5):
    backend=require_backend(config)
    if backend.get("mode")!="enterprise_local":
        raise ValueError("enterprise_backend_not_selected")
    if backend.get('credential_renewal',False):
        from agentclient.credentials import renew
        renew(backend)
    return EnterpriseLocal(backend["url"],backend["credential_file"],timeout,transport=backend.get("transport","loopback"))


def require_backend(config):
    backend=config.get('knowledge_backend',{})
    if (backend.get('mode')!='enterprise_local' or backend.get('api_version')!='cloud-local-1'):
        raise ValueError('PostgreSQL backend profile required; legacy knowledge stores are not opened')
    return backend


def capture_connection(config, project):
    """Choose an explicitly configured source connection; server still authorizes it."""
    backend=require_backend(config)
    if 'connection_ids' in backend:
        connections=backend['connection_ids']
        if not isinstance(connections,dict):raise ValueError('capture_connection_mapping')
        connection=connections.get(project)
    else:connection=backend.get('connection_id')
    if not isinstance(connection,str) or not connection or len(connection)>256:
        raise ValueError('project_capture_connection_required')
    return connection
