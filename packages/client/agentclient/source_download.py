"""Explicit bounded original download. Never opens a client knowledge database."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request

MAX_FILE_BYTES=50*1024*1024

def download(backend,request,destination,*,max_bytes=MAX_FILE_BYTES):
    target=Path(destination).expanduser()
    if not target.is_absolute():raise ValueError('absolute_download_destination_required')
    target=target.resolve()
    if target.exists():raise ValueError('download_target_exists')
    descriptor=backend.request('/enterprise/v3/source-documents/describe',request)
    body=json.dumps(request).encode()
    req=urllib.request.Request(backend.url+'/enterprise/v3/source-documents/download',data=body,
        headers={'Authorization':'Bearer '+backend.token,'Content-Type':'application/json'})
    digest=hashlib.sha256();size=0;created=False
    try:
        with backend.opener.open(req,timeout=max(15,backend.timeout)) as response:
            with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as out:
                created=True
                while True:
                    chunk=response.read(65536)
                    if not chunk:break
                    size+=len(chunk)
                    if size>max_bytes:raise ValueError('original_download_bound')
                    digest.update(chunk);out.write(chunk)
                out.flush();os.fsync(out.fileno())
        checksum=digest.hexdigest()
        expected=descriptor.get('sha256',descriptor.get('checksum'))
        length=descriptor.get('bytes',descriptor.get('byte_length',descriptor.get('size')))
        if not expected or checksum!=expected or length is not None and size!=length:raise ValueError('original_download_checksum')
        return {'path':str(target),'sha256':checksum,'bytes':size,'original':descriptor.get('original',True)}
    except BaseException:
        if created:target.unlink(missing_ok=True)
        raise

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--profile',required=True)
    parser.add_argument('--source-id',required=True);parser.add_argument('--version');parser.add_argument('--output',required=True)
    args=parser.parse_args();cfg=json.loads((Path(args.profile)/'config.json').read_text())
    from agentclient.transport import enterprise_client
    request={'source_id':args.source_id}
    if args.version:request['version']=args.version
    print(json.dumps(download(enterprise_client(cfg,timeout=15),request,args.output)))
if __name__=='__main__':main()
