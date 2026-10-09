#!/usr/bin/env python3
"""Audit saved V5 measurements without repeating search; private receipts only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

FIXTURE_SHA = 'a0a0810b6bf61944a5b8505af553dfb232ca2a6dac1614a9f765a697f6ec81b9'
MANIFEST_SHA = '95505a6141c06afea669e1a3b6f5d462207b8b7394d9969359685f0e5e302770'
SOURCE_SHA = '561c61b8dcfe0b727b17b14c64c7b213d1032bc5bd57aa9e353193041f9116fc'


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)


def fingerprint(args):
    if os.environ.get('PYTHONPATH'):
        raise ValueError('installed_pythonpath_required_absent')
    import agenthub
    import agentclient
    from agenthub.backend_ops import build_identity
    from agenthub.cloud_local import runtime
    for module in (agenthub, agentclient):
        if 'site-packages' not in Path(module.__file__).parts:
            raise ValueError('installed_packages_required')
    profile = Path(args.profile)
    seed = read(profile / 'synthetic-seed.json')
    if seed['fixture_sha256'] != SOURCE_SHA or len(seed['sources']) != 60:
        raise ValueError('source_seed_mismatch')
    settings, registry = runtime(profile)
    if settings.get('provider_mode') != 'off':
        raise ValueError('provider_mode_off_required')
    store = registry.resolve('acme')
    ids = list(seed['sources'].values())
    with store.open() as state:
        revisions = [dict(r) for r in state.db.execute('''SELECT source_id,revision,digest,policy_version
            FROM backend_source_revisions WHERE source_id=ANY(?::text[]) ORDER BY source_id''', (ids,))]
        sources = [dict(r) for r in state.db.execute('''SELECT id,source_version,policy_version,active
            FROM enterprise_sources WHERE id=ANY(?::text[]) ORDER BY id''', (ids,))]
        documents = [dict(r) for r in state.db.execute('''SELECT d.document_id,d.active_revision_id,d.lifecycle
            FROM knowledge_documents d JOIN enterprise_dependencies e ON e.document_id=d.document_id
            WHERE e.source_id=ANY(?::text[]) ORDER BY d.document_id,e.source_id''', (ids,))]
        generations = [dict(r) for r in state.db.execute('SELECT * FROM cloud_vector_generations ORDER BY id')]
    result = hashlib.sha256(json.dumps([revisions, sources, documents, generations],
        sort_keys=True, default=str).encode()).hexdigest()
    value = {'status': 'read_only_fingerprint_after_harness_error', 'created_at_unix': time.time(),
        'build_ids': build_identity()['build_ids'], 'source_fingerprint': result,
        'source_count': len(ids), 'source_fixture_sha256': SOURCE_SHA, 'search_calls': 0,
        'embedding_calls': 0, 'provider_calls': 0, 'installed_interpreter': sys.executable,
        'timing_limit': 'A later read-only snapshot, not a persisted fingerprint at the exact final search instant.'}
    write(args.output, value)
    return value


def aggregate(args):
    if digest(args.fixture) != FIXTURE_SHA or digest(args.manifest) != MANIFEST_SHA:
        raise ValueError('frozen_inputs_changed')
    fixture, manifest = read(args.fixture), read(args.manifest)
    receipt, seed = read(args.receipt), read(args.seed)
    before, after = read(args.preflight), read(args.fingerprint)
    if receipt['status'] != 'incomplete' or receipt['error_type'] != 'TypeError':
        raise ValueError('original_harness_failure_required')
    if manifest['families'] != 6:
        raise ValueError('original_integer_family_metadata_changed')
    if seed['fixture_sha256'] != SOURCE_SHA or len(seed['sources']) != 60:
        raise ValueError('source_seed_mismatch')
    if before['build_ids'] != receipt['build_ids'] or after['build_ids'] != receipt['build_ids']:
        raise ValueError('build_mismatch')
    if before['source_fingerprint'] != after['source_fingerprint']:
        raise ValueError('corpus_or_vector_fingerprint_changed')
    expected = {q['id']: q for q in fixture['queries']}
    rows = receipt['rows']
    if len(rows) != 30 or len(expected) != 30 or len({r['query_id'] for r in rows}) != 30:
        raise ValueError('row_count_or_unique_ids')
    if {r['query_id'] for r in rows} != set(expected) or receipt['completed_queries'] != 30:
        raise ValueError('measured_ids_mismatch')
    families = {q['family'] for q in expected.values()}
    if len(families) != 6:
        raise ValueError('family_count_mismatch')
    for row in rows:
        case = expected[row['query_id']]
        if row['family'] != case['family'] or row['expected_answerable'] != case['answerable']:
            raise ValueError('row_semantics_mismatch')
        ids = {seed['sources'][key] for key in case['expected_sources']}
        if set(row['expected_source_ids']) != ids:
            raise ValueError('expected_source_mapping_mismatch')
        if row['supported_ids'] != (ids <= set(row['returned_source_ids']) if ids else not row['returned_source_ids']):
            raise ValueError('support_boolean_mismatch')
        if not set(row['missing_literal_facts']) <= set(case['expected_facts']):
            raise ValueError('literal_facts_mismatch')
        if not 0 <= row['precise_cards'] <= row['delivered_cards']:
            raise ValueError('card_counts_mismatch')
    positives = [r for r in rows if r['expected_answerable']]
    negatives = [r for r in rows if not r['expected_answerable']]
    if len(positives) != 25 or len(negatives) != 5:
        raise ValueError('expected_denominators_mismatch')
    family_counts = {family: sum(r['family'] == family for r in rows) for family in families}
    if any(n != 5 for n in family_counts.values()):
        raise ValueError('family_denominators_mismatch')
    recall = sum(r['recall_at_10'] for r in positives)
    coverage = sum(r['answerable'] and r['supported_ids'] for r in positives)
    abstention = sum(not r['answerable'] and r['delivered_cards'] == 0 for r in negatives)
    delivered = sum(r['delivered_cards'] for r in rows)
    precise = sum(r['precise_cards'] for r in rows)
    rates = {'recall_at_10': recall / 25, 'answerable_coverage': coverage / 25,
        'delivered_precision': precise / delivered if delivered else 0, 'abstention': abstention / 5}
    value = {'status': 'offline_aggregation_of_once_measured_rows', 'original_cli_status': 'incomplete',
        'original_cli_error': 'TypeError: integer manifest families is not iterable',
        'original_cli_intact_pass': False, 'fixture_sha256': FIXTURE_SHA,
        'manifest_sha256': MANIFEST_SHA, 'original_receipt_sha256': digest(args.receipt),
        'preflight_sha256': digest(args.preflight), 'later_fingerprint_receipt_sha256': digest(args.fingerprint),
        'created_at_unix': time.time(), 'build_ids': receipt['build_ids'], 'queries_measured_once': 30,
        'positive_queries': 25, 'answerless_queries': 5, 'rates': rates, 'gates': fixture['gates'],
        'gates_pass': {k: rates[k] >= v for k, v in fixture['gates'].items()},
        'saved_row_gate_pass': all(rates[k] >= v for k, v in fixture['gates'].items()),
        'counts': {'recall': recall, 'supported': coverage, 'precise_cards': precise,
            'delivered_cards': delivered, 'abstention': abstention,
            'literal_complete': sum(not r['missing_literal_facts'] for r in positives)},
        'families': {f: {'queries': family_counts[f], 'supported': sum(r['family'] == f and r['supported_ids'] for r in rows)} for f in sorted(families)},
        'vector_channels_observed': sum('vector' in r['candidate_channels'] for r in rows),
        'seconds_original_measurement': receipt['seconds'], 'max_serialized_chars': max(r['serialized_chars'] for r in rows),
        'unsatisfied_query_ids': [r['query_id'] for r in positives if not r['supported_ids'] or r['missing_literal_facts']],
        'source_fingerprint_before': before['source_fingerprint'], 'source_fingerprint_later': after['source_fingerprint'],
        'fingerprint_timing_limit': after['timing_limit'], 'provider_calls': 0, 'new_search_calls': 0,
        'new_embedding_calls': 0, 'independent_held_out': False,
        'classification': 'Authored non-independent wordings on reused original sources; original CLI failure preserved; saved results audited offline.'}
    write(args.output, value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('fingerprint'); p.add_argument('--profile', required=True); p.add_argument('--output', required=True)
    p = subs.add_parser('aggregate')
    for name in ('fixture', 'manifest', 'receipt', 'seed', 'preflight', 'fingerprint', 'output'):
        p.add_argument('--' + name, required=True)
    args = parser.parse_args()
    print(json.dumps(fingerprint(args) if args.command == 'fingerprint' else aggregate(args), sort_keys=True))


if __name__ == '__main__':
    main()
