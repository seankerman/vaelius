"""Source object adapter configuration."""
def objects_from_settings(settings):
    from agenthub.source_objects import S3SourceObjects, FileSourceObjects
    obj=settings['objects']
    if obj['kind']=='file':return FileSourceObjects(obj['root'])
    # kms_key_id is optional SSE-KMS; when set, writes fail closed unless the key
    # is usable and S3 confirms each object was encrypted with it.
    kms={'kms_key_id':obj['kms_key_id']} if 'kms_key_id' in obj else {}
    if obj['kind']=='s3_aws':
        if set(obj)-{'kind','bucket','region','kms_key_id'}:raise ValueError('aws_workload_configuration')
        return S3SourceObjects(None,obj['bucket'],None,None,region=obj['region'],workload_identity=True,**kms)
    if obj['kind']!='s3':raise ValueError('unsupported_object_adapter')
    return S3SourceObjects(obj['endpoint'],obj['bucket'],obj['access_key'],obj['secret_key'],local_hosts=obj.get('local_hosts',()),**kms)
