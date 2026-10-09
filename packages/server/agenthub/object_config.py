"""Source object adapter configuration."""
def objects_from_settings(settings):
    from agenthub.source_objects import S3SourceObjects, FileSourceObjects
    obj=settings['objects']
    if obj['kind']=='file':return FileSourceObjects(obj['root'])
    if obj['kind']=='s3_aws':
        if set(obj)-{'kind','bucket','region'}:raise ValueError('aws_workload_configuration')
        return S3SourceObjects(None,obj['bucket'],None,None,region=obj['region'],workload_identity=True)
    if obj['kind']!='s3':raise ValueError('unsupported_object_adapter')
    return S3SourceObjects(obj['endpoint'],obj['bucket'],obj['access_key'],obj['secret_key'],local_hosts=obj.get('local_hosts',()))
