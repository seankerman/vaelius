"""Validate public synthetic regressions; sealed labels require an explicit path.

This command never opens a private confirmation by default and never runs search.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT/'fixtures/local_staging_readiness_v1'


def validate_public(directory=FIXTURES):
    directory = Path(directory)
    manifest_path=directory/('fixture_manifest-v2.json' if (directory/'fixture_manifest-v2.json').exists() else 'fixture_manifest.json')
    manifest = json.loads(manifest_path.read_text())
    corpus_name=manifest.get('corpus_path','corpus.json')
    regressions = json.loads((directory/'regressions.json').read_text())
    corpus = json.loads((directory/corpus_name).read_text())
    for name,key in [('regressions.json','regressions_sha256'),(corpus_name,'corpus_sha256')]:
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != manifest[key]:
            raise ValueError('staging_fixture_changed:'+name)
    expected_cases = {'populated_episode','forbidden_neighbors','identifier_quantity','cross_principal_i',
                      'attributed_preference','missing_rationale','multiple_facets','original_ambiguity',
                      'native_history','return_revocation','continuous_correction','source_author'}
    if {c['id'] for c in regressions['cases']} != expected_cases:
        raise ValueError('staging_regression_coverage')
    for case in regressions['cases']:
        sources = {s['id']:s for s in case['sources']}
        for source in sources.values():
            if hashlib.sha256(source['text'].encode()).hexdigest() != source['sha256']:
                raise ValueError('staging_regression_source_hash')
        for facet in case['source_spans']:
            source = sources[facet['source_id']]
            if source['text'][facet['start']:facet['end']] != facet['quote']:
                raise ValueError('staging_regression_span')
    splits = {}
    for source in corpus['sources']:
        old = splits.setdefault(source['scenario'],source['split'])
        if old != source['split']:
            raise ValueError('staging_scenario_overlap')
    confirmation = [s for s in corpus['sources'] if s['split']=='confirmation']
    if len(confirmation)!=120 or len({s['scenario'] for s in confirmation})!=12:
        raise ValueError('staging_confirmation_source_shape')
    return {'pass':True,'regressions':len(regressions['cases']),
            'public_sources':len(corpus['sources']),'confirmation_sources':120,
            'sealed_questions_read':False,'model_calls':0,'queries':0,
            'confirmation_consumed':False,'classification':manifest['classification']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures',type=Path,default=FIXTURES)
    parser.add_argument('--sealed',type=Path,help='Explicit author-only validation; never use for tuning')
    args=parser.parse_args()
    result=validate_public(args.fixtures)
    if args.sealed:
        from build_retrieval_experiment_fixtures import validate,read,DEFAULT_FIXTURES
        manifest=read(args.fixtures/('fixture_manifest-v2.json' if (args.fixtures/'fixture_manifest-v2.json').exists() else 'fixture_manifest.json'))
        result['summary']=validate(read(args.fixtures/manifest.get('corpus_path','corpus.json')),read(DEFAULT_FIXTURES/'development.json'),read(args.sealed))
        result['sealed_questions_read']=True
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
