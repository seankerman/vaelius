"""Finite synthetic volume rehearsal through actual authorized retrieval paths.

Fast fixture expansion populates canonical tables using canonical identity/index
functions and exact spans in a retained synthetic original. It is not an ingest or
model-quality benchmark. The isolated synthetic vector key never replaces Nomic.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import secrets
import selectors
import subprocess
import sys
import time
import uuid

from agenthub.cloud_ops import percentiles

SYNTHETIC_MODEL_KEY='synthetic-volume-onehot-512-v1'
CAPACITY_PER_TENANT=50000


def fixture_text(index):
    return (f'## Volume record {index:06d}\nDataset saved at /synthetic/volume/{index:06d}.tsv '
        'because deterministic headers remain stable.\n')


class SyntheticVolumeEmbedder:
    """Seeded one-hot vectors measure volume only; no semantic-quality claim."""
    model_key=SYNTHETIC_MODEL_KEY
    def embed_queries(self,texts):
        import re
        values=[]
        for text in texts:
            match=re.search(r'\b([0-9]{6})\b',text)
            index=int(match[1]) if match else int.from_bytes(hashlib.sha256(text.encode()).digest()[:4],'big')
            vector=[0.0]*512;vector[index%512]=1.0;values.append(vector)
        return values
    embed_documents=embed_queries


def _write_private(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'w') as output:
        json.dump(value,output,indent=2,sort_keys=True);output.write('\n')


def provision(profile,parent_profile):
    """Create two isolated test DBs/roles, never modify existing tenant data."""
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    from agenthub.postgres import connect, migrate
    root=Path(profile).expanduser().resolve();settings=root/'volume.json'
    if settings.exists():
        value=json.loads(settings.read_text())
        if value.get('kind')!='synthetic_volume_fixture_v1':raise ValueError('volume_profile_not_synthetic')
        return value
    if root.exists() and any(root.iterdir()):raise ValueError('volume_target_not_empty')
    parent=Path(parent_profile).expanduser().resolve();source=json.loads((parent/'services.json').read_text())
    administrator=conninfo_to_dict(source['admin_dsn']);suffix=uuid.uuid4().hex[:12]
    value={'kind':'synthetic_volume_fixture_v1','namespace':suffix,'root':str(root),
        'seeded_total':0,'model_key':SYNTHETIC_MODEL_KEY,'capacity_per_tenant':CAPACITY_PER_TENANT,
        'tenants':{},'parent_profile':str(parent)}
    for tenant in ('acme','bravo'):
        name='volume_'+suffix+'_'+tenant;role=name+'_app';password=secrets.token_urlsafe(24)
        with connect(source['admin_dsn'],autocommit=True) as db:
            db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            db.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
            db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(name),sql.Identifier(role)))
        admin_dsn=make_conninfo(**(administrator|{'dbname':name}))
        migrate(admin_dsn)
        from agenthub.cloud_profile import _grant
        _grant(admin_dsn,role)
        with connect(admin_dsn) as db:
            db.execute(sql.SQL('GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}').format(sql.Identifier(role)))
        value['tenants'][tenant]={'dsn':make_conninfo(**(administrator|{'dbname':name,'user':role,'password':password})),
            'admin_dsn':admin_dsn,'database':name,'role':role,'seeded':0}
    _write_private(settings,value)
    return value


def _store(config,tenant):
    from agenthub.cloud_runtime import CloudStore
    from psycopg.conninfo import conninfo_to_dict,make_conninfo
    tenant_config=config['tenants'][tenant]
    dsn=make_conninfo(**(conninfo_to_dict(tenant_config['dsn'])|{'options':'-c statement_timeout=10000'}))
    store=CloudStore(Path(config['root'])/'state'/tenant,dsn,tenant)
    store.semantic_embedder=SyntheticVolumeEmbedder();store.semantic_model_key=SYNTHETIC_MODEL_KEY
    store.hybrid_enabled=True
    return store


def _initialize_source(config,tenant):
    from agenthub.document_ingest import DocumentStore
    from agenthub.source_objects import FileSourceObjects
    store=_store(config,tenant);tenant_config=config['tenants'][tenant]
    store.create_organization(tenant);actor='volume-author';project='synthetic-volume'
    store.create_principal(tenant,actor);store.create_project(tenant,project);store.set_membership(tenant,project,actor,True)
    token=store.enroll(tenant,actor,'volume-fixture',['ingest','read','source_read','policy','withdraw','correct'])
    ctx=store.authenticate(token);store.enroll_connection(ctx,'volume-original','volume-fixture',project,['document'],visibility='private',reader_ids=[actor])
    raw=''.join(fixture_text(i) for i in range(1,CAPACITY_PER_TENANT+1)).encode()
    objects=FileSourceObjects(Path(config['root'])/'objects'/tenant)
    # Explicit synthetic fixture expands canonical exact passages in bulk below.
    # Keeping this original unsupported avoids timing 50k per-row curator writes.
    receipt=DocumentStore(store,objects).ingest(ctx,'volume-original','volume-original','1',
        'volume-fixture.dat',io.BytesIO(raw),title='Synthetic volume original')
    with store.open() as state:
        source=state.db.execute('SELECT * FROM enterprise_sources WHERE id=%s',(receipt['source_id'],)).fetchone()
        memory=state.db.execute('SELECT session FROM memories WHERE id=%s',(receipt['source_id'],)).fetchone()
    tenant_config.update({'token':token,'source_id':receipt['source_id'],'scope':source['internal_project'],
        'session':memory['session'],'project':project,'object_sha256':receipt['sha256']})
    return store


def _records(config,tenant,first,last):
    from agenthub.processing.knowledge import _claim,_identity,_identifier,_index_text,SCHEMA_VERSION
    tenant_config=config['tenants'][tenant];scope=tenant_config['scope'];session=tenant_config['session']
    source=tenant_config['source_id'];prefix=config['namespace']+':'+tenant+':'
    now=time.time();width=len(fixture_text(1));vectors={}
    for index in range(first,last+1):
        memory=prefix+'native:'+str(index);document=_identifier('doc_',memory);revision=_identifier('rev_',memory+':1')
        text=fixture_text(index);title=f'Volume record {index:06d}'
        claim=_claim({'title':title,'problem':'','lesson':text,'applicability':'',
            'applicability_constraints':{'versions':[],'platforms':[],'date_ranges':[],'units':[],'project_scope':[]},
            'knowledge_type':'observation','domain':'unknown','subjects':[],'tags':[],'outcome':'unknown',
            'evidence_status':'source_linked_unverified'})
        encoded=json.dumps(claim,sort_keys=True,ensure_ascii=False);identity_json,identity_hash=_identity(claim)
        if index%512 not in vectors:
            vectors[index%512]='['+','.join('1' if position==index%512 else '0' for position in range(512))+']'
        yield {'memory':(memory,session,scope,text,'NativeRepresentation',now,1),
            'observation_source':(memory,source),
            'document':(document,scope,session,memory,'active',revision,SCHEMA_VERSION,now,now),
            'member':(document,memory,'origin',now),
            'revision':(revision,document,1,encoded,identity_json,identity_hash,None,'synthetic_volume_native_fixture',SCHEMA_VERSION,now),
            'support':(revision,source,None,'supports','unknown',now),
            'fts':(document,revision,_index_text(claim)),'index':(document,revision),
            'enterprise_document':(document,tenant,scope,revision,1,1,now,''),
            'dependency':(document,source),'artifact':(document,source,'native_document','synthetic:volume'),
            'span':(document,source,(index-1)*width,index*width,title),
            'vector':(config['namespace']+':'+tenant+':vectors',document,revision,hashlib.sha256(text.encode()).hexdigest(),vectors[index%512])}


COPY_TABLES=[('memories','id,session,project,body,kind,created,active','memory'),
    ('observation_sources','memory_id,source_id','observation_source'),
    ('knowledge_documents','document_id,project,owner_session,origin_memory_id,lifecycle,active_revision_id,schema_version,created,updated','document'),
    ('knowledge_document_members','document_id,memory_id,relation,created','member'),
    ('knowledge_revisions','revision_id,document_id,revision_number,claim_json,identity_json,identity_hash,previous_revision_id,reason,schema_version,created','revision'),
    ('knowledge_support','revision_id,source_memory_id,source_segment_id,relation,independence,created','support'),
    ('knowledge_fts','document_id,revision_id,body','fts'),('knowledge_index_rows','document_id,revision_id','index'),
    ('enterprise_documents','id,tenant,internal_project,revision,active,policy_version,created,blocked_reason','enterprise_document'),
    ('enterprise_dependencies','document_id,source_id','dependency'),
    ('backend_native_artifacts','document_id,source_id,artifact_kind,source_url','artifact'),
    ('backend_native_spans','document_id,source_id,start,"end",heading','span'),
    ('cloud_document_vectors','generation_id,document_id,revision_id,body_sha256,embedding','vector')]


def seed_volume(config,total,*,batch_size=2000):
    if type(total) is not int or total<2 or total>100000 or total%2:
        raise ValueError('volume_count_requires_even_2_to_100000')
    if total<config['seeded_total']:raise ValueError('volume_seed_cannot_shrink')
    started=time.monotonic();per_tenant=total//2
    for tenant,tenant_config in config['tenants'].items():
        store=_store(config,tenant) if tenant_config.get('source_id') else _initialize_source(config,tenant)
        generation=config['namespace']+':'+tenant+':vectors'
        with store.open() as state,state.db:
            state.db.execute('''INSERT INTO cloud_vector_generations VALUES(%s,%s,512,'active',%s,%s,0)
                ON CONFLICT(id) DO NOTHING''',(generation,SYNTHETIC_MODEL_KEY,time.time(),time.time()))
        for first in range(tenant_config['seeded']+1,per_tenant+1,batch_size):
            last=min(per_tenant,first+batch_size-1);records=list(_records(config,tenant,first,last))
            with store.open() as state,state.db:
                # COPY is only a labeled synthetic fixture setup optimization.
                # Every measured read below uses the production service methods.
                for table,columns,key in COPY_TABLES:
                    state.db.copy_rows(table,columns,(record[key] for record in records))
                state.db.execute('UPDATE cloud_vector_generations SET document_count=%s WHERE id=%s',(last,generation))
            tenant_config['seeded']=last;_write_private(Path(config['root'])/'volume.json',config)
        with store.open() as state,state.db:
            state.db.execute('''INSERT INTO cloud_vector_state VALUES(1,%s,%s,%s) ON CONFLICT(singleton)
                DO UPDATE SET generation_id=excluded.generation_id,model_key=excluded.model_key,updated=excluded.updated''',
                (generation,SYNTHETIC_MODEL_KEY,time.time()))
            state.db.execute('ANALYZE')
    config['seeded_total']=total;_write_private(Path(config['root'])/'volume.json',config)
    return {'kind':'synthetic_canonical_table_fixture_expansion','total_records':total,
        'per_tenant':per_tenant,'seconds':time.monotonic()-started,'model_key':SYNTHETIC_MODEL_KEY,
        'provider_calls':0,'quality_evidence':False,'original_objects':2}


def compare_authorization(config,tenant,*,limit=20):
    store=_store(config,tenant);ctx=store.authenticate(config['tenants'][tenant]['token'])
    with store.open() as state:
        compiled=set(store._authorized_documents(state,ctx,'synthetic-volume'))
        sample=state.db.execute('SELECT id FROM enterprise_documents ORDER BY id LIMIT %s',(limit,)).fetchall()
        expected={row['id'] for row in sample if store._document_allowed(state.db,ctx,row['id'])}
        if expected != compiled.intersection({row['id'] for row in sample}):
            raise ValueError('compiled_authorization_disagrees_with_canonical_oracle')
    return {'sample':len(sample),'oracle_matches':True}


def _probe_worker(profile):
    config=json.loads((Path(profile)/'volume.json').read_text())
    stores={name:_store(config,name) for name in config['tenants']}
    contexts={name:store.authenticate(config['tenants'][name]['token']) for name,store in stores.items()}
    print(json.dumps({'ready':True}),flush=True)
    for line in sys.stdin:
        request=json.loads(line);number=request['query_index'];workload=request.get('workload','exact')
        tenant=tuple(stores)[number%len(stores)];store=stores[tenant]
        index=1+(number*7919)%config['tenants'][tenant]['seeded']
        query=f'Where is the dataset for Volume Record {index:06d} saved?' if workload=='hybrid' else f'Volume record {index:06d}'
        try:
            original_candidates=store.candidates;stage={}
            def timed_candidates(*args,**kwargs):
                first=time.monotonic();items=original_candidates(*args,**kwargs)
                stage['seconds']=time.monotonic()-first;stage['items']=items;return items
            store.candidates=timed_candidates
            first=time.monotonic();result=store.search(contexts[tenant],{'version':'enterprise-local-1',
                'query':query,'project':'synthetic-volume','mode':'explicit','limit':8})
            value={'candidates_seconds':stage.get('seconds',0),'search_seconds':time.monotonic()-first,
                'found':result.get('answerable',False) and any(f'/synthetic/volume/{index:06d}.tsv' in item.get('lesson','') for item in result.get('results',[])),
                'channels':sorted({channel for item in stage.get('items',[]) for channel in item.get('channels',[])})}
        except Exception as error:
            value={'error':type(error).__name__}
        finally:
            store.candidates=original_candidates
        print(json.dumps(value),flush=True)


def probe(config,*,queries=200,max_seconds=60,workload='exact'):
    if type(queries) is not int or not 1<=queries<=200 or not 1<=max_seconds<=60:
        raise ValueError('volume_probe_bound')
    if workload not in ('exact','hybrid'):raise ValueError('volume_workload')
    if workload=='hybrid':
        path=Path(__import__('agenthub').__file__).parent/'fixtures/cloud_volume_hybrid_v1.json'
        manifest=json.loads(path.with_name('cloud_volume_hybrid_v1_manifest.json').read_text())
        if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sha256']:raise ValueError('frozen_hybrid_volume_changed')
    start=time.monotonic();latencies={'candidates':[],'search':[]};completed=0;failures=[]
    process=subprocess.Popen([sys.executable,'-m','vaelius_test_support.hub.cloud_load','--probe-worker',config['root']],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
    def receive():
        remaining=max_seconds-(time.monotonic()-start)
        if remaining<=0 or not selector.select(remaining):return None
        line=process.stdout.readline()
        return json.loads(line) if line else {'error':'worker_exit'}
    try:
        ready=receive()
        if not ready or not ready.get('ready'):
            failures.append({'kind':'worker_startup_or_deadline'})
        else:
            for number in range(queries):
                if time.monotonic()-start>=max_seconds:
                    failures.append({'kind':'wall_time_bound','completed_queries':completed});break
                process.stdin.write(json.dumps({'query_index':number,'workload':workload})+'\n');process.stdin.flush()
                value=receive()
                if value is None:
                    failures.append({'kind':'hard_query_deadline','query_index':number,'completed_queries':completed});break
                if 'error' in value:
                    failures.append({'kind':'production_method_failure','exception':value['error'],'query_index':number});break
                latencies['candidates'].append(value['candidates_seconds']);latencies['search'].append(value['search_seconds'])
                if not value['found']:failures.append({'kind':'synthetic_exact_title_missing','query_index':number})
                if workload=='hybrid' and 'vector' not in value['channels']:
                    failures.append({'kind':'synthetic_vector_path_not_exercised','query_index':number})
                if workload=='exact' and 'exact' not in value['channels']:
                    failures.append({'kind':'exact_discovery_path_not_exercised','query_index':number})
                completed+=1
    finally:
        selector.close()
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
    elapsed=time.monotonic()-start
    return {'kind':'actual_candidate_and_search_methods','records':config['seeded_total'],
        'requested_queries':queries,'completed_queries':completed,'max_seconds':max_seconds,
        'elapsed_seconds':elapsed,'latency_seconds':{key:percentiles(value) for key,value in latencies.items()},
        'failures':failures,'status':'PASS' if not failures and completed==queries and elapsed<=max_seconds else 'FAIL',
        'model_key':SYNTHETIC_MODEL_KEY,'provider_calls':0,'semantic_quality_evidence':False,
        'hardware':{'machine':platform.machine(),'platform':platform.platform()},
        'memory_maxrss':__import__('resource').getrusage(__import__('resource').RUSAGE_SELF).ru_maxrss,
        'concurrency':1,'workload':workload,'vector_volume_evidence':workload=='hybrid',
        'interface':'one production search/query with timed candidate stage; HTTP transport measured separately'}


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else argv
    if len(arguments)==2 and arguments[0]=='--probe-worker':
        _probe_worker(arguments[1]);return 0
    parser=argparse.ArgumentParser(description='Finite isolated synthetic enterprise volume rehearsal')
    parser.add_argument('--profile',required=True);parser.add_argument('--parent-profile',required=True)
    parser.add_argument('--seed',type=int,choices=(10000,100000),required=True)
    parser.add_argument('--queries',type=int,default=200);parser.add_argument('--max-seconds',type=int,default=60)
    parser.add_argument('--workload',choices=('exact','hybrid'),default='exact')
    args=parser.parse_args(argv);config=provision(args.profile,args.parent_profile)
    result={'seed':seed_volume(config,args.seed),'authorization':{t:compare_authorization(config,t) for t in config['tenants']},
        'probe':probe(config,queries=args.queries,max_seconds=args.max_seconds,workload=args.workload)}
    _write_private(Path(args.profile)/f'receipt-{args.seed}-{args.workload}-{time.time_ns()}.json',result)
    _write_private(Path(args.profile)/f'receipt-{args.seed}-{args.workload}.json',result)
    print(json.dumps(result,sort_keys=True));return 0 if result['probe']['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
