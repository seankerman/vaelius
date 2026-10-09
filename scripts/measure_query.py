#!/usr/bin/env python3
"""Synthetic installed source-search latency and SQL counts; no model execution."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not json.loads(args.services.read_text()).get('disposable'):
        raise ValueError('explicit_disposable_manifest_required')
    os.environ['AGENTNETWORK_PG_SERVICES'] = str(args.services.resolve())
    sys.path.insert(0, str(ROOT / 'packages/server/tests'))
    from test_cloud_postgres import PostgresFixture
    from agentclient.enterprise_capture import normalize_capture
    from agenthub.cloud_runtime import CloudStore
    from agenthub.postgres import PostgresConnection
    from agenthub.source_index import SourceIndex
    from agenthub.backend_ops import build_identity
    fixture = PostgresFixture()
    fixture.postgres_setup()
    try:
        store = CloudStore(fixture.store.home, fixture.dsn, 'acme')
        store.retrieval_corpus = 'sources'
        store.enroll_connection(fixture.ctx, 'measure', 'synthetic', 'maple', ['agent'], visibility='team')
        for number in range(40):
            text = ('The Maple importer retains the stable CSV header. '
                    f'Diagnostic batch {number} is saved at /data/maple/batch-{number}.csv. '
                    'The header was retained because downstream reports require the column names.')
            event = normalize_capture({'hook_event_name': 'Stop', 'event_id': 'measure-' + str(number),
                                       'session_id': 'synthetic', 'turn_id': str(number),
                                       'timestamp': '2026-09-01T12:00:00Z', 'last_assistant_message': text},
                                      'maple', 'measure')
            store.ingest_general(fixture.ctx, event)
        SourceIndex(store).run(max_sources=60, max_seconds=30, max_passages=128)
        queries = ['Maple stable CSV header', 'Maple diagnostic batch', 'downstream reports column names']
        samples, counts = [], []
        original = PostgresConnection.execute
        counter = [0]

        def execute(connection, *args, **kwargs):
            counter[0] += 1
            return original(connection, *args, **kwargs)

        for query in queries:
            store.search(fixture.ctx, {'version': 'enterprise-local-1', 'query': query, 'project': 'maple', 'limit': 3})
        with patch.object(PostgresConnection, 'execute', execute):
            for _ in range(10):
                for query in queries:
                    counter[0] = 0
                    begin = time.perf_counter()
                    response = store.search(fixture.ctx, {'version': 'enterprise-local-1', 'query': query, 'project': 'maple', 'limit': 3})
                    samples.append((time.perf_counter() - begin) * 1000)
                    counts.append(counter[0])
                    if not response['results']:
                        raise AssertionError('synthetic_source_not_retrieved')
        denied = store.search(store.authenticate(fixture.tokens['bob']),
                              {'version': 'enterprise-local-1', 'query': queries[0], 'project': 'maple'})
        if denied['results']:
            raise AssertionError('raw_source_owner_boundary_failed')
        identity = build_identity()
        record = {'definition': '40 synthetic source records, three natural lexical queries, 30 warm searches',
                  'build_ids': identity['build_ids'], 'samples': len(samples), 'median_query_ms': round(statistics.median(samples), 3),
                  'p95_query_ms': round(sorted(samples)[int(.95 * (len(samples) - 1))], 3),
                  'sql_calls_min': min(counts), 'sql_calls_max': max(counts), 'nonowner_results': len(denied['results']),
                  'provider_calls': 0, 'scope': 'small synthetic timing regression; not a scale or usefulness benchmark'}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
        print(json.dumps(record, sort_keys=True))
    finally:
        fixture.postgres_teardown()


if __name__ == '__main__':
    main()
