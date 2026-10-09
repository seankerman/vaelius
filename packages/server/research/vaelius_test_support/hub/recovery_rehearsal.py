from agenthub.cloud_recovery import *
from agenthub.cloud_recovery import _json, _write

def rehearse(profile,store,settings):
    """Run a provider-free isolated operator rehearsal without changing routes.

    Both databases are synthetic test fixtures. The supplied installed tenant is
    only checked for unchanged source counts; no raw production corpus is copied.
    Private operator credentials authorize administrative fixture restoration.
    """
    import io
    import uuid
    from vaelius_test_support.hub.cloud_load import provision
    from agenthub.cloud_runtime import CloudStore
    from agenthub.document_ingest import DocumentStore
    from agenthub.enterprise import Denied
    from agenthub.source_objects import FileSourceObjects, ObjectMissing
    profile=Path(profile).expanduser().resolve();operator_path=profile/'operator.json'
    if operator_path.stat().st_mode&0o077:raise ValueError('profile_operator_permissions')
    operator=json.loads(operator_path.read_text())
    root=profile/'recovery-rehearsals'/uuid.uuid4().hex
    _write(root/'bootstrap/services.json',_json({'admin_dsn':operator['control_admin_dsn']}))
    config=provision(root/'databases',root/'bootstrap')
    tenant=store.tenant_id
    source=CloudStore(root/'source-state',config['tenants']['acme']['dsn'],tenant)
    target=CloudStore(root/'target-state',config['tenants']['bravo']['dsn'],tenant)
    source.create_organization(tenant);source.create_project(tenant,'recovery-fixture')
    tokens={}
    for actor in ('alice','bob'):
        source.create_principal(tenant,actor);source.set_membership(tenant,'recovery-fixture',actor,True)
        tokens[actor]=source.enroll(tenant,actor,'recovery-fixture-'+actor,['ingest','read','source_read','policy','withdraw','correct'])
    ctx=source.authenticate(tokens['alice'])
    source.enroll_connection(ctx,'recovery-docs','recovery-fixture','recovery-fixture',['document'],
        visibility='team',reader_ids=['alice','bob'])
    source_objects=FileSourceObjects(root/'source-objects');target_objects=FileSourceObjects(root/'target-objects')
    documents=DocumentStore(source,source_objects)
    def ingest(external,raw,version=1):
        return documents.ingest(ctx,'recovery-docs',external,str(version),external+'.md',io.BytesIO(raw),title=external)
    with store.open() as state:before=state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0]
    original=ingest('guide',b'# Guide\nDataset saved at /synthetic/old.csv because headers remain stable.\n')
    obsolete=ingest('obsolete',b'# Obsolete\nThis old procedure is withdrawn later.\n')
    snapshot=root/'before';backup(source,source_objects,snapshot)
    current_bytes=b'# Guide\nDataset saved at /synthetic/current.tsv because TSV preserves headers.\n'
    corrected=ingest('guide',current_bytes,2)
    added=ingest('new',b'# New\nThis source arrived after backup.\n')
    source.lifecycle(ctx,{'version':'enterprise-local-1','operation':'delete','target_id':obsolete['source_id'],
        'expected_revision':'1','idempotency_key':'delete-obsolete','reason':'synthetic post-backup deletion'})
    source.connection_policy(ctx,'recovery-docs',reader_ids=['alice'])
    admin=config['tenants']['bravo']['admin_dsn']
    restore_snapshot(snapshot,target,target_objects,admin_dsn=admin)
    def held():
        try:target.require_ready()
        except ValueError:return True
        raise AssertionError('logical_restore_exposed_unreconciled_fixture')
    missing_deltas_denied=held()
    with source.open() as state:
        key=state.db.execute('SELECT object_key FROM backend_document_versions WHERE source_id=%s',(corrected['source_id'],)).fetchone()[0]
    source_objects.delete(key)
    try:
        reconcile_restore(snapshot,target,source,target_objects,source_objects,admin_dsn=admin,destination=root/'missing-original')
    except ObjectMissing:missing_object_denied=held()
    else:raise AssertionError('logical_restore_missing_original_not_denied')
    source_objects.put(key,io.BytesIO(current_bytes))
    result=reconcile_restore(snapshot,target,source,target_objects,source_objects,admin_dsn=admin,destination=root/'reconciled')
    target.require_ready();restored=DocumentStore(target,target_objects)
    alice=target.authenticate(tokens['alice']);bob=target.authenticate(tokens['bob'])
    stream,_=restored.fetch(alice,corrected['source_id'])
    with stream:correction_exact=stream.read()==current_bytes
    stream,_=restored.fetch(alice,added['source_id'])
    with stream:post_backup_write_preserved=b'after backup' in stream.read()
    try:restored.fetch(alice,obsolete['source_id'])
    except Denied:deletion_denied=True
    else:raise AssertionError('logical_restore_deleted_source_exposed')
    try:restored.fetch(bob,corrected['source_id'])
    except Denied:policy_narrowing_denied=True
    else:raise AssertionError('logical_restore_old_policy_exposed')
    with store.open() as state:after=state.db.execute('SELECT count(*) FROM enterprise_sources').fetchone()[0]
    if before!=after:raise AssertionError('operator_rehearsal_changed_live_sources')
    receipt=result|{'rehearsal_directory':str(root),'installed_authority_sources_unchanged':True,
        'fixture_only':True,'missing_deltas_denied':missing_deltas_denied,'missing_object_denied':missing_object_denied,
        'correction_exact':correction_exact,'post_backup_source_preserved':post_backup_write_preserved,
        'deletion_denied':deletion_denied,'policy_narrowing_denied':policy_narrowing_denied}
    _write(root/'receipt.json',_json(receipt))
    return receipt
