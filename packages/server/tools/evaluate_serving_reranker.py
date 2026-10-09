"""Small authored-case comparison through the installed serving ranker/harness.

No search adapter or alternative model client. Receipts stay outside the checkout;
one finite call per case, two concurrent requests, and no retry of pending work.
Use explicit disposable PostgreSQL services and put tests on PYTHONPATH.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import time


def grade(case, returned):
    if (not isinstance(returned, dict) or set(returned) != {'order','support'}
            or returned['support'] not in ('none','partial','complete')):
        return dict.fromkeys(('coverage','precision','top','ranking','support','passed'), False)
    order = returned['order']
    valid = (isinstance(order, list) and all(isinstance(x, str) for x in order)
             and len(order) == len(set(order)))
    selected = set(order) if valid else set()
    coverage = valid and all(selected.intersection(group) for group in case['required_groups'])
    precision = valid and selected.issubset(case['allowed'])
    top = valid and (order[0] in case['top'] if order else not case['top'])
    support = returned.get('support') == case['support']
    return dict(coverage=bool(coverage), precision=bool(precision), top=bool(top),
                ranking=bool(coverage and precision and top), support=support,
                passed=bool(coverage and precision and top and support))


def write_new(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2); stream.write('\n')


def main():
    from agenthub import serving_reranker as serving
    from agenthub.backend_ops import build_identity
    from agenthub.cloud_ops import Meter
    from agenthub.processing.harness import run_structured
    from test_cloud_postgres import PostgresFixture
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('fixture', 'output', 'worker-config', 'ledger'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--instruction')
    args = parser.parse_args(); os.umask(0o077)
    output = Path(args.output).resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('private_receipt_directory_required')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = Path(args.fixture).read_bytes(); cases = json.loads(raw)['cases']
    instruction = Path(args.instruction).read_text() if args.instruction else serving.INSTRUCTION
    definition = dict(fixture_sha256=hashlib.sha256(raw).hexdigest(), instruction=instruction,
                      build=build_identity(), model='gpt-6-luna', reasoning='low', concurrency=2,
                      script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    if (output/'definition.json').exists():
        if json.loads((output/'definition.json').read_text()) != definition:
            raise ValueError('frozen_run_changed')
    else: write_new(output/'definition.json', definition)
    config = json.loads(Path(args.worker_config).read_text())
    fixture = PostgresFixture(); fixture.postgres_setup()
    try:
        meter = Meter(fixture.store); meter.configure(max_parallel=2)
        def run(case):
            target = output/(case['name']+'.json')
            if target.exists(): return json.loads(target.read_text())
            pending = output/(case['name']+'.pending.json')
            write_new(pending, {'case': case['name'], 'started': time.time()})
            capture = {}
            def runner(home, cfg, ignored, payload, schema):
                # Expected labels never enter model input. Only the frozen prompt
                # differs in an experimental arm; projection/accounting stay canonical.
                result, usage = run_structured(home, cfg, instruction, payload, schema)
                capture.update(returned=result, usage=usage)
                return result, usage
            ranker = serving.ServingReranker(output/'work', config, ledger=args.ledger,
                meter=meter, runner=runner, timeout_seconds=30)
            start = time.monotonic()
            result = ranker.rerank(case['query'], case['cards'])
            with fixture.store.open() as state:
                row = state.db.execute('SELECT id,status,usage,latency FROM cloud_usage_attempts WHERE id=?',
                    (result['reranking']['attempt_id'],)).fetchone()
            record = dict(case=case['name'], seconds=time.monotonic()-start,
                returned=capture.get('returned', {}), diagnostics=result['reranking'],
                tenant_receipt=dict(row), grade=grade(case, capture.get('returned', {})),
                input_order_grade=grade(case, {'order':['c'+str(i) for i in range(len(case['cards']))],
                    'support':'partial'}))
            write_new(target, record)
            print(json.dumps({'case':case['name'], 'grade':record['grade'],
                              'returned':record['returned']}), flush=True)
            return record
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, cases))
        summary = dict(cases=len(results), metrics={key:sum(r['grade'][key] for r in results)
            for key in ('coverage','precision','top','ranking','support','passed')},
            median_seconds=statistics.median(r['seconds'] for r in results),
            input_top=sum(r['input_order_grade']['top'] for r in results),
            usage={key:sum(r['diagnostics'].get('usage',{}).get(key,0) for r in results)
                   for key in serving.USAGE_KEYS})
        if (output/'summary.json').exists():
            if json.loads((output/'summary.json').read_text()) != summary:
                raise ValueError('saved_summary_changed')
        else: write_new(output/'summary.json', summary)
        print(json.dumps(summary), flush=True)
    finally: fixture.postgres_teardown()


if __name__ == '__main__': main()
