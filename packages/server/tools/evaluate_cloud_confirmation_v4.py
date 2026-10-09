#!/usr/bin/env python3
"""One-use authored confirmation against installed runtime + existing originals.

Run a private copy from /tmp with the matching installed interpreter and no
PYTHONPATH. Never seeds, reindexes, installs providers, changes generations or
rewrites questions. Receipts and the build/fixture single-use marker stay private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

FIXTURE_SHA='d4c54f5683ba4371f2abbcc3e2fb6bfbc6bb162a424add45109aaff3ca3de340'
SOURCE_SHA='561c61b8dcfe0b727b17b14c64c7b213d1032bc5bd57aa9e353193041f9116fc'
MAX_ORIGINAL=50*1024*1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('confirmation_redirect_denied')


def private_write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink():raise ValueError('confirmation_receipt_symlink')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as output:json.dump(value,output,indent=2,sort_keys=True)
    path.chmod(0o600)


def load_frozen(fixture_path,manifest_path):
    raw=Path(fixture_path).read_bytes();manifest=json.loads(Path(manifest_path).read_text())
    if hashlib.sha256(raw).hexdigest()!=FIXTURE_SHA or manifest['fixture_sha256']!=FIXTURE_SHA:
        raise ValueError('confirmation_fixture_changed')
    if not manifest['frozen_before_evaluation'] or not manifest['frozen_before_followup_behavior_change']:
        raise ValueError('confirmation_not_frozen')
    fixture=json.loads(raw)
    if (len(fixture['queries'])!=30 or sum(not q['answerable'] for q in fixture['queries'])!=5 or
            len({q['family'] for q in fixture['queries']})!=6):raise ValueError('confirmation_shape_changed')
    return fixture,manifest


def evaluate(args):
    if os.environ.get('PYTHONPATH'):raise ValueError('confirmation_source_pythonpath_denied')
    if not Path.cwd().resolve().is_relative_to(Path('/tmp').resolve()):
        raise ValueError('confirmation_requires_tmp_working_directory')
    import agenthub,agentclient
    for module in (agenthub,agentclient):
        if 'site-packages' not in Path(module.__file__).parts:
            raise ValueError('confirmation_installed_packages_required')
    from agenthub.backend_ops import build_identity
    from agenthub.cloud_local import runtime
    from agentclient.enterprise_contract import VERSION
    fixture,manifest=load_frozen(args.fixture,args.manifest)
    identity=build_identity();build_ids=identity['build_ids']
    profile=Path(args.profile).expanduser().resolve(strict=True)
    # Rebuilding cannot make a touched question set fresh. Actual build IDs are
    # evidence in the marker/receipt, never part of the replay permission key.
    marker=profile/'confirmation-v4-runs'/(FIXTURE_SHA+'.json')
    if marker.exists():raise ValueError('confirmation_build_fixture_already_consumed')
    marker.parent.mkdir(exist_ok=True,mode=0o700)
    if marker.parent.stat().st_mode&0o077:raise ValueError('confirmation_marker_directory_permissions')
    api=args.api_url.rstrip('/');parsed=urlsplit(api)
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError('confirmation_explicit_loopback_api_required')
    opener=build_opener(ProxyHandler({}),NoRedirect())
    def request(path,payload=None,token=None,binary=False):
        headers={'Accept':'application/octet-stream' if binary else 'application/json'}
        body=None
        if payload is not None:body=json.dumps(payload).encode();headers['Content-Type']='application/json'
        if token:headers['Authorization']='Bearer '+token
        with opener.open(Request(api+path,data=body,headers=headers),timeout=15) as response:
            raw=response.read(MAX_ORIGINAL+1 if binary else 128*1024+1)
        if len(raw)>(MAX_ORIGINAL if binary else 128*1024):raise ValueError('confirmation_http_response_bound')
        return raw if binary else json.loads(raw)
    health=request('/health')
    if health.get('build_ids')!=build_ids:raise ValueError('confirmation_api_installed_build_mismatch')
    request('/ready')
    settings,registry=runtime(profile)
    if settings.get('provider_mode')!='off':raise ValueError('confirmation_provider_mode_required_off')
    semantic=settings.get('semantic',{})
    if not semantic.get('directory') or not Path(semantic['directory']).is_dir():
        raise ValueError('confirmation_cached_nomic_assets_required')
    store=registry.resolve(fixture['tenant'])
    if store.semantic_embedder is None or 'nomic' not in store.semantic_model_key.lower():
        raise ValueError('confirmation_real_nomic_required')
    if store.answerability_judge is not None:raise ValueError('confirmation_provider_judge_denied')
    store.hybrid_enabled=True # Instance-only evaluation setting; no persisted change.
    token_path=profile/'credentials'/(fixture['tenant']+'-'+fixture['actor']+'.token')
    if token_path.stat().st_mode&0o077:raise ValueError('confirmation_token_permissions')
    token=token_path.read_text().strip();ctx=store.authenticate(token)
    if ctx['actor']!=fixture['actor'] or ctx['tenant']!=fixture['tenant']:
        raise ValueError('confirmation_principal_mismatch')
    seed=json.loads((profile/'synthetic-seed.json').read_text())
    if seed['fixture_sha256']!=SOURCE_SHA or len(seed['sources'])!=60:
        raise ValueError('confirmation_existing_seed_mismatch')
    # Read only source rows from the frozen installed corpus; never old queries.
    original_fixture=Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_readiness_v1.json'
    raw=original_fixture.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SOURCE_SHA:raise ValueError('confirmation_installed_source_fixture_changed')
    source_texts={s['id']:s['text'] for s in json.loads(raw)['sources']}
    mapping=seed['sources']
    if set(mapping)!=set(source_texts):raise ValueError('confirmation_source_mapping_mismatch')
    originals=[]
    for key,source_id in sorted(mapping.items()):
        metadata=request('/enterprise/v3/source-documents/describe',{'source_id':source_id},token)
        downloaded=request('/enterprise/v3/source-documents/download',{'source_id':source_id},token,binary=True)
        expected=source_texts[key].encode()
        if downloaded!=expected:raise ValueError('confirmation_original_bytes_mismatch')
        sha=hashlib.sha256(downloaded).hexdigest()
        # Descriptor names are the public source-document contract.
        if metadata.get('sha256')!=sha:raise ValueError('confirmation_original_descriptor_checksum_mismatch')
        originals.append({'fixture_id':key,'source_id':source_id,'sha256':sha,'bytes':len(downloaded)})
    ids=list(mapping.values())
    def fingerprint():
        with store.open() as state:
            revisions=[dict(r) for r in state.db.execute('''SELECT source_id,revision,digest,policy_version
                FROM backend_source_revisions WHERE source_id=ANY(?::text[]) ORDER BY source_id''',(ids,))]
            sources=[dict(r) for r in state.db.execute('''SELECT id,source_version,policy_version,active
                FROM enterprise_sources WHERE id=ANY(?::text[]) ORDER BY id''',(ids,))]
            documents=[dict(r) for r in state.db.execute('''SELECT d.document_id,d.active_revision_id,d.lifecycle
                FROM knowledge_documents d JOIN enterprise_dependencies e ON e.document_id=d.document_id
                WHERE e.source_id=ANY(?::text[]) ORDER BY d.document_id,e.source_id''',(ids,))]
            generations=[dict(r) for r in state.db.execute('SELECT * FROM cloud_vector_generations ORDER BY id')]
        return hashlib.sha256(json.dumps([revisions,sources,documents,generations],sort_keys=True,default=str).encode()).hexdigest()
    before=fingerprint()
    if args.preflight_only:
        receipt={'status':'preflight_only_no_queries','fixture_sha256':FIXTURE_SHA,'build_ids':build_ids,
            'verified_originals':len(originals),'original_bytes':sum(r['bytes'] for r in originals),
            'source_fingerprint':before,'real_local_embedding_model':store.semantic_model_key,
            'installed_interpreter':sys.executable,'api_url':api,'provider_calls':0,
            'query_evaluations':0,'marker_consumed':False,'pythonpath_present':False}
        output=Path(args.output).expanduser().resolve()
        if output.exists():raise ValueError('confirmation_new_receipt_required')
        private_write(output,receipt)
        return receipt
    receipt_path=Path(args.output).expanduser().resolve()
    if receipt_path.exists():raise ValueError('confirmation_new_receipt_required')
    baseline={'fixture_sha256':FIXTURE_SHA,'source_fixture_sha256':SOURCE_SHA,'build_ids':build_ids,
        'status':'reserved','created':time.time(),'authored':True,'independent_held_out':False}
    fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as file:json.dump(baseline,file)
    rows=[];started=time.monotonic()
    def deadline(*unused):raise TimeoutError('confirmation_query_deadline')
    previous_handler=signal.signal(signal.SIGALRM,deadline)
    signal.setitimer(signal.ITIMER_REAL,args.max_seconds)
    def support(document_ids):
        if not document_ids:return set()
        with store.open() as state:
            return {r[0] for r in state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=ANY(?::text[])',(document_ids,))}
    try:
        for case in fixture['queries']:
            if time.monotonic()-started>args.max_seconds:raise TimeoutError('confirmation_query_deadline')
            trace=[];original_candidates=store.candidates
            def candidates(*pos,**kw):
                result=original_candidates(*pos,**kw)
                trace.extend(result)
                return result
            store.candidates=candidates
            query_started=time.monotonic()
            try:
                result=store.search(ctx,{'version':VERSION,'query':case['query'],
                    'project':fixture['project_scope'],'mode':'explicit','limit':8})
            finally:store.candidates=original_candidates
            expected={mapping[k] for k in case['expected_sources']}
            candidate_ids=list(dict.fromkeys(c['document_id'] for c in trace))
            candidate_sources=support(candidate_ids[:10])
            delivered=result['results'];returned=support([c['id'] for c in delivered])
            precise=sum(bool(support([c['id']])) and support([c['id']])<=expected for c in delivered)
            serialized=json.dumps(result,ensure_ascii=False)
            facts=[fact for fact in case['expected_facts'] if fact.casefold() not in serialized.casefold()]
            rows.append({'query_id':case['id'],'family':case['family'],'expected_answerable':case['answerable'],
                'answerable':result['answerable'],'recall_at_10':expected<=candidate_sources if expected else None,
                'supported_ids':expected<=returned if expected else not returned,
                'candidate_document_ids':candidate_ids[:10],'candidate_channels':sorted({c for item in trace for c in item.get('channels',[])}),
                'returned_source_ids':sorted(returned),'expected_source_ids':sorted(expected),
                'delivered_cards':len(delivered),'precise_cards':precise,'missing_literal_facts':facts,
                'seconds':time.monotonic()-query_started,'serialized_chars':len(serialized)})
        after=fingerprint()
        if after!=before:raise ValueError('confirmation_corpus_or_generation_changed')
        answerable=sum(r['expected_answerable'] for r in rows);answerless=len(rows)-answerable
        recall=sum(r['expected_answerable'] and r['recall_at_10'] for r in rows)
        correct=sum(r['expected_answerable'] and r['answerable'] and r['supported_ids'] for r in rows)
        abstains=sum(not r['expected_answerable'] and not r['answerable'] and r['delivered_cards']==0 for r in rows)
        delivered=sum(r['delivered_cards'] for r in rows);precise=sum(r['precise_cards'] for r in rows)
        rates={'recall_at_10':recall/answerable,'answerable_coverage':correct/answerable,
            'delivered_precision':precise/delivered if delivered else 0,'abstention':abstains/answerless}
        vector_queries=sum('vector' in r['candidate_channels'] for r in rows)
        receipt=baseline|{'status':'completed','classification':fixture['classification'],
            'source_manifest_verified':originals,'queries':len(rows),'answerable_queries':answerable,'answerless_queries':answerless,
            'rates':rates,'gates':fixture['gates'],'pass':all(rates[k]>=v for k,v in fixture['gates'].items()),
            'queries_with_observed_vector_channel':vector_queries,
            'real_vector_path_observed':bool(vector_queries),
            'literal_fact_coverage':sum(r['expected_answerable'] and not r['missing_literal_facts'] for r in rows)/answerable,
            'literal_fact_check':'supplementary exact-text presence; not semantic answer-quality proof',
            'families':{family:{'queries':sum(r['family']==family for r in rows),
                'supported':sum(r['family']==family and r['supported_ids'] for r in rows)} for family in manifest['families']},
            'rows':rows,'source_fingerprint_before':before,'source_fingerprint_after':after,
            'seconds':time.monotonic()-started,'provider_calls':0,'llm_calls':0,
            'real_local_embedding_model':store.semantic_model_key,'hybrid_enabled':True,
            'candidate_recall_semantics':'first10 unique documents observed across the candidate invocations inside one search; no separate candidate search request',
            'installed_interpreter':sys.executable,'api_url':api,'pythonpath_present':False}
        private_write(receipt_path,receipt)
        private_write(marker,{'status':'completed','receipt':str(receipt_path),'fixture_sha256':FIXTURE_SHA,
            'build_ids':build_ids,'pass':receipt['pass'],'created':baseline['created']})
        return {'receipt':str(receipt_path),'queries':len(rows),'rates':rates,'pass':receipt['pass'],'provider_calls':0}
    except Exception as error:
        private_write(receipt_path,baseline|{'status':'incomplete','error_type':type(error).__name__,
            'completed_queries':len(rows),'rows':rows,'seconds':time.monotonic()-started,'provider_calls':0})
        private_write(marker,baseline|{'status':'incomplete','receipt':str(receipt_path),
            'completed_queries':len(rows),'error_type':type(error).__name__})
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous_handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True);parser.add_argument('--api-url',required=True)
    parser.add_argument('--fixture',required=True);parser.add_argument('--manifest',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--max-seconds',type=int,default=60)
    parser.add_argument('--preflight-only',action='store_true',help='Verify installed API/source originals without querying or consuming confirmation')
    args=parser.parse_args()
    if not 1<=args.max_seconds<=60:parser.error('max-seconds must be 1 through 60')
    print(json.dumps(evaluate(args),sort_keys=True))


if __name__=='__main__':main()
