"""Installed API/worker runtime with explicit local configuration and no startup models."""
import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path
from agenthub.postgres import PostgresEnterpriseStore, TenantRegistry
from agenthub.cloud_ops import require_reconciled
from agenthub.data_lifecycle import DataLifecycleMixin

class CloudStore(DataLifecycleMixin, PostgresEnterpriseStore):
    curation_enabled=False
    def reconcile_indexes(self,*,max_pages=10):
        """Queue missing/stale vectors after an accepted change or owned restart.

        The scan is finite and provider-free. An operator can repeat it using
        the returned cursor when a larger corpus exceeds this local bound.
        """
        if type(max_pages) is not int or not 1<=max_pages<=10:
            raise ValueError('index_reconcile_page_bound')
        from agenthub.cloud_maintenance import IndexFreshness
        index=IndexFreshness(self)
        cursor='';queued=0;scanned=0;more=False
        for _ in range(max_pages):
            page=index.reconcile(max_documents=1000,after_document_id=cursor)
            queued+=page['queued'];scanned+=page['scanned'];more=page['more']
            if not more:break
            cursor=page['next_after_document_id']
        return {'queued':queued,'scanned':scanned,'more':more,
                'next_after_document_id':cursor if more else None,'provider_calls':0}

    def refresh_documents(self, *, db=None, document_id=None):
        registered=super().refresh_documents(db=db,document_id=document_id)
        if db is not None:
            if document_id is not None:
                from agenthub.cloud_maintenance import IndexFreshness
                IndexFreshness(self).queue_document_tx(db,document_id)
            return registered
        # No model work here. A separate repair sweep covers older writes and
        # paths that do not yet register a targeted job in their transaction.
        with self.open() as state:
            active=state.db.execute('SELECT 1 FROM cloud_vector_state WHERE singleton=1').fetchone()
        if active:self.reconcile_indexes()
        return registered

    def project_history(self,ctx,project,*,cursor=None,limit=8):
        from agenthub.project_history import project_overview
        return project_overview(self,ctx,project,cursor=cursor,page_limit=limit)

    def project_episode(self,ctx,project,episode_id,*,offset=0,limit=8,text_offset=0):
        from agenthub.project_history import project_episode_detail
        return project_episode_detail(self,ctx,project,episode_id,offset=offset,
            limit=limit,text_offset=text_offset)

    def require_ready(self):
        super().require_ready()
        require_reconciled(self)

    def _purge_deleted_source(self,db,source_id):
        super()._purge_deleted_source(db,source_id)
        segments=getattr(self,'conversation_segments',None)
        if segments is not None:segments.purge(db,source_id)


def read_settings(path):
    source=Path(path).expanduser().resolve()
    if source.stat().st_mode&0o077:raise ValueError('runtime_settings_permissions')
    settings=json.loads(source.read_text())
    if 'control_dsn_file' in settings:
        if 'control_dsn' in settings:raise ValueError('ambiguous_control_database_configuration')
        from agenthub.secret_files import read_secret
        settings['control_dsn']=read_secret(settings.pop('control_dsn_file'))
    if settings.get('provider_mode','off')!='off':raise ValueError('api_startup_cannot_enable_providers')
    return settings

LOOPBACK=('127.0.0.1','localhost','::1')


def deployment_settings(settings):
    """Validate the declared transport; production refuses to start without one.

    `deployment` is absent (development: plain HTTP, today's behavior) or:
      {"mode":"production","tls":"proxy","trusted_proxies":["10.0.0.0/16"]}
      {"mode":"production","tls":"direct","certfile":"/run/tls/tls.crt","keyfile":"/run/tls/tls.key"}
    Production also requires an explicit, exact `allowed_hosts` list.
    """
    value=settings.get('deployment',{})
    if not isinstance(value,dict) or set(value)-{'mode','tls','trusted_proxies','certfile','keyfile'}:
        raise ValueError('deployment_configuration')
    mode=value.get('mode','development');tls=value.get('tls')
    if mode not in ('development','production') or tls not in (None,'proxy','direct'):
        raise ValueError('deployment_configuration')
    if tls=='proxy':
        proxies=value.get('trusted_proxies')
        if not isinstance(proxies,list) or not 1<=len(proxies)<=64 or set(value)-{'mode','tls','trusted_proxies'}:
            raise ValueError('deployment_trusted_proxies_required')
        for item in proxies:
            # Exact addresses or networks only: never "*" or a catch-all network.
            try:network=ipaddress.ip_network(item,strict=True) if isinstance(item,str) else None
            except ValueError:network=None
            if network is None or network.prefixlen==0:raise ValueError('deployment_trusted_proxies_required')
    elif tls=='direct':
        if set(value)-{'mode','tls','certfile','keyfile'}:raise ValueError('deployment_configuration')
        for key in ('certfile','keyfile'):
            if not isinstance(value.get(key),str) or not Path(value[key]).is_absolute() or not Path(value[key]).is_file():
                raise ValueError('deployment_tls_files_required')
        if Path(value['keyfile']).stat().st_mode&0o077:raise ValueError('deployment_tls_key_permissions')
    elif set(value)-{'mode'}:raise ValueError('deployment_configuration')
    if mode=='production':
        if tls is None:raise ValueError('production_requires_tls_or_trusted_proxy')
        hosts=settings.get('allowed_hosts')
        if (not isinstance(hosts,list) or not hosts
                or any(not isinstance(h,str) or not h or '*' in h for h in hosts)):
            raise ValueError('production_requires_explicit_allowed_hosts')
    return {'mode':mode,'tls':tls,'trusted_proxies':value.get('trusted_proxies'),
            'certfile':value.get('certfile'),'keyfile':value.get('keyfile')}


def server_options(deployment,bind):
    """uvicorn transport options for a validated deployment."""
    options={'proxy_headers':False,'server_header':False}
    if deployment['tls']=='proxy':
        # Only the declared proxies may set the client scheme via X-Forwarded-Proto.
        options.update(proxy_headers=True,forwarded_allow_ips=list(deployment['trusted_proxies']))
    elif deployment['tls']=='direct':
        options.update(ssl_certfile=deployment['certfile'],ssl_keyfile=deployment['keyfile'])
    elif bind not in LOOPBACK:
        print('warning: serving plain HTTP on a non-loopback address; declare deployment.mode='
              '"production" with a TLS proxy or certificate (docs/SELF_HOSTING.md)',file=sys.stderr)
    return options


def registry_from_settings(settings,home):
    configuration = settings.get('retrieval', {})
    allowed = {'candidate_limit', 'numeric_mode', 'lexical_weight', 'vector_weight',
               'vector', 'require_prose', 'authorization_shape', 'selection_policy', 'corpus'}
    if (not isinstance(configuration, dict) or set(configuration)-allowed
            or configuration.get('candidate_limit', 20) not in (20, 50)
            or configuration.get('numeric_mode', 'legacy') not in ('legacy', 'typed')
            or configuration.get('authorization_shape', 'compiled') not in ('legacy', 'anti_join', 'authorized_cte', 'compiled')
            or configuration.get('selection_policy', 'baseline') not in ('baseline','facets_v1','facets_v2')
            or configuration.get('corpus','sources') not in ('sources','all')
            or type(configuration.get('vector', True)) is not bool
            or type(configuration.get('require_prose', False)) is not bool
            or any(type(configuration.get(key, 1)) not in (int, float)
                   or not 0 < configuration.get(key, 1) <= 3 for key in ('lexical_weight', 'vector_weight'))):
        raise ValueError('runtime_retrieval_configuration')
    processing=settings.get('processing',{})
    if (not isinstance(processing,dict) or set(processing)-{'curation_enabled'}
            or type(processing.get('curation_enabled',False)) is not bool):
        raise ValueError('runtime_processing_configuration')
    from agenthub.cloud_identity import validate_credential_lifetimes
    lifetimes=validate_credential_lifetimes(settings.get('credentials',{}))
    reranking=settings.get('reranking',{})
    if (not isinstance(reranking,dict) or set(reranking)-{'enabled','worker_config','ledger','timeout_seconds'}
            or type(reranking.get('enabled',False)) is not bool):
        raise ValueError('runtime_reranking_configuration')
    from agenthub.model_policy import policy_from_settings, require_stage_allowed
    model_policy=policy_from_settings(settings)
    reranker_config=None
    if reranking.get('enabled'):
        source=Path(reranking['worker_config']).expanduser()
        if not source.is_absolute() or source.stat().st_mode&0o077:raise ValueError('reranker_private_configuration')
        reranker_config=json.loads(source.read_text())
        if reranking.get('ledger') is not None and not Path(reranking['ledger']).expanduser().is_absolute():raise ValueError('reranker_shared_ledger_required')
        # Startup refuses an enabled stage whose provider/model is not allowlisted.
        require_stage_allowed(model_policy,reranker_config,'reranking')
    from agenthub.write_limits import limits_from_settings
    from agenthub.governance import notice_from_settings
    ingest_limits=limits_from_settings(settings.get('ingest_limits'))
    capture_notice=notice_from_settings(settings.get('capture_notice'))
    model=None
    objects=None
    if settings.get('objects'):
        from agenthub.object_config import objects_from_settings
        objects=objects_from_settings(settings)
    if settings.get('semantic',{}).get('directory'):
        from agenthub.processing.semantic import SemanticModel
        model=SemanticModel(Path(settings['semantic']['directory']))
        accounting = settings.get('semantic', {}).get('accounting_root')
        if accounting is not None:
            from agenthub.retrieval_embedding_cache import CampaignEmbedder
            model = CampaignEmbedder(model, home, accounting_root=accounting,
                max_embedding_seconds=settings['semantic'].get('max_embedding_seconds', 3600))
    def factory(path,dsn,tenant,registry=None):
        override=settings.get('postgres_endpoint_override')
        if override:
            from psycopg.conninfo import conninfo_to_dict,make_conninfo
            selected=conninfo_to_dict(dsn)
            if selected.get('host') not in ('127.0.0.1','localhost','::1'):raise ValueError('unexpected_local_tenant_route')
            dsn=make_conninfo(dsn,host=override['host'],port=override['port'])
        store=CloudStore(path,dsn,tenant,registry=registry)
        store.credential_lifetimes=lifetimes
        store.ingest_limits=ingest_limits;store.capture_notice_config=capture_notice
        store.curation_enabled=processing.get('curation_enabled',False)
        store.retrieval_corpus=configuration.get('corpus','sources')
        if objects is not None:
            from agenthub.conversation_segments import ConversationSegments
            store.conversation_segments=ConversationSegments(store,objects)
            store.source_objects=objects
        store.hybrid_enabled=bool(settings.get('semantic',{}).get('enabled',False))
        store.hybrid_enabled = store.hybrid_enabled and configuration.get('vector', True)
        store.semantic_embedder=model
        if reranker_config is not None:
            from agenthub.serving_reranker import ServingReranker
            from agenthub.cloud_ops import Meter
            store.serving_reranker=ServingReranker(path/'reranking',reranker_config,
                ledger=Path(reranking['ledger']).expanduser() if reranking.get('ledger') else None,meter=Meter(store),
                timeout_seconds=reranking.get('timeout_seconds',20),model_policy=model_policy)
        for key, default in [('candidate_limit', 20), ('numeric_mode', 'legacy'),
                             ('lexical_weight', 1), ('vector_weight', 1), ('require_prose', False),
                             ('authorization_shape', 'compiled'), ('selection_policy', 'baseline')]:
            # Old profiles remain readable, but serving cannot select diagnostic
            # predicates that bypass the restricted PostgreSQL reader role.
            setattr(store, 'retrieval_'+key, 'compiled' if key=='authorization_shape' else configuration.get(key, default))
        return store
    arguments={'max_stores':settings.get('max_stores',16),'store_factory':factory}
    if settings.get('authorization'):
        from agenthub.oauth_provider import OAuthRegistry
        return OAuthRegistry(settings['control_dsn'],home,authorization=settings['authorization'],**arguments)
    return TenantRegistry(settings['control_dsn'],home,**arguments)

def app_from_profile(profile):
    from agenthub.backend_ops import build_identity
    identity=build_identity() # Refuse files that do not match the built artifact.
    from agenthub.cloud_api import create_app
    from agenthub.document_ingest import DocumentStore
    from agenthub.object_config import objects_from_settings
    profile=Path(profile).resolve();settings=read_settings(profile/'runtime.json')
    deployment=deployment_settings(settings)
    registry=registry_from_settings(settings,profile/'server-state')
    objects=objects_from_settings(settings)
    from agenthub.broker_config import broker_from_settings
    brokers={name:broker_from_settings(value) for name,value in settings.get('identity_brokers',{}).items()}
    return create_app(registry,build_ids=identity['build_ids'],brokers=brokers,allowed_hosts=settings.get('allowed_hosts',['127.0.0.1','localhost']),
        allowed_origins=settings.get('allowed_origins',[]),document_factory=lambda store:DocumentStore(store,objects),
        https_only=deployment['mode']=='production')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--profile',required=True)
    parser.add_argument('--bind',default='127.0.0.1');parser.add_argument('--port',type=int,default=55486)
    args=parser.parse_args();os.umask(0o077)
    import uvicorn
    # Validate before building the app so a production profile without a declared
    # TLS/proxy transport never opens a listener.
    options=server_options(deployment_settings(read_settings(Path(args.profile).resolve()/'runtime.json')),args.bind)
    uvicorn.run(app_from_profile(args.profile),host=args.bind,port=args.port,
        access_log=False,timeout_graceful_shutdown=10,limit_concurrency=32,**options)
if __name__=='__main__':main()
