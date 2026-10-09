import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from agenthub.cloud_ops import Meter,AdmissionExhausted
from agenthub.enterprise import Conflict

FIXTURE_PATH=Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_metering_v1.json'
FIXTURE=json.loads(FIXTURE_PATH.read_text())


class MeterFixtureTests(unittest.TestCase):
    def test_frozen_usage_cache_and_price_invariants(self):
        manifest=json.loads(FIXTURE_PATH.with_name('cloud_metering_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),manifest['sha256'])
        usage=FIXTURE['usage'];price=FIXTURE['price']
        cost=((usage['input_tokens']-usage['cached_input_tokens']-usage['cache_write_input_tokens'])*price['input']+
            usage['cached_input_tokens']*price['cached_input']+usage['cache_write_input_tokens']*price['cache_write_input']+
            usage['output_tokens']*price['output'])/1_000_000
        self.assertAlmostEqual(cost,FIXTURE['expected_estimated_cost'])


@unittest.skipUnless(os.environ.get('CLOUD_TEST_DSN'),'real PostgreSQL fixture DSN required')
class MeterPostgresTests(unittest.TestCase):
    def setUp(self):
        from agenthub.postgres import PostgresEnterpriseStore
        self.temp=tempfile.TemporaryDirectory();self.tenant='meter-'+uuid.uuid4().hex
        self.store=PostgresEnterpriseStore(self.temp.name,os.environ['CLOUD_TEST_DSN'],os.environ.get('CLOUD_TEST_TENANT','acme'))
        # Meter-only records use a unique test namespace; knowledge and root's
        # in-progress tenant admission are neither reset nor reconfigured.
        self.meter=Meter(SimpleNamespace(tenant_id=self.tenant,open=self.store.open))
        self.meter.configure(max_parallel=1,max_attempts=3)

    def tearDown(self):self.temp.cleanup()

    def attempt(self,name):return self.tenant+':'+name

    def test_old_total_attempt_limit_does_not_gate_new_work(self):
        self.meter.configure(max_parallel=1,max_attempts=1)
        first=self.attempt('old-total-first')
        self.meter.reserve(first,'job1','observer')
        self.meter.finish(first,'failed',usage={'input_tokens':37})
        self.meter.reserve(self.attempt('old-total-second'),'job2','observer')
        self.assertEqual(self.meter.status()['attempts'],2)
        self.assertEqual(self.meter.status()['reported_tokens']['input_tokens'],37)

    def test_stable_attempt_retry_cache_usage_and_no_double_charge(self):
        first=self.attempt('first');second=self.attempt('retry')
        self.meter.reserve(first,'job1','observer');self.meter.dispatched(first)
        returned=self.meter.finish(first,'returned',usage=FIXTURE['usage'],result={'result_id':'synthetic'},price=FIXTURE['price'],latency=.1)
        self.assertAlmostEqual(returned['estimated_cost'],FIXTURE['expected_estimated_cost']);self.assertIsNone(returned['billed_cost'])
        duplicate=self.meter.finish(first,'returned',usage=FIXTURE['usage'],result={'result_id':'synthetic'},price=FIXTURE['price'],latency=.1)
        self.assertTrue(duplicate['duplicate'])
        self.meter.reserve(second,'job1','observer',retry=True);self.meter.dispatched(second)
        self.meter.finish(second,'failed')
        status=self.meter.status();self.assertEqual(status['attempts'],2);self.assertEqual(status['retry_attempts'],1)
        self.assertEqual(status['reported_tokens'],FIXTURE['usage']);self.assertEqual(status['without_reported_usage'],1)
        self.assertAlmostEqual(status['estimated_cost'],FIXTURE['expected_estimated_cost'])
        with self.assertRaises(Conflict):self.meter.finish(first,'returned',usage={'input_tokens':999},result={'result_id':'changed'})
        with self.assertRaises(Conflict):self.meter.reserve(first,'job1','observer')

    def test_uncertain_return_reconciles_one_attempt_without_inventing_usage(self):
        attempt=self.attempt('uncertain');self.meter.reserve(attempt,'job1','observer');self.meter.dispatched(attempt)
        self.meter.finish(attempt,'uncertain')
        status=self.meter.status();self.assertEqual(status['without_reported_usage'],1)
        self.assertEqual(status['reported_tokens'],{})
        receipt=self.meter.reconcile(attempt,'returned',usage=FIXTURE['usage'],result={'result_id':'recovered'},price=FIXTURE['price'])
        self.assertFalse(receipt['duplicate']);self.assertEqual(self.meter.status()['attempts'],1)
        self.assertEqual(self.meter.status()['statuses'],{'returned':1})
        self.assertAlmostEqual(self.meter.status()['estimated_cost'],FIXTURE['expected_estimated_cost'])
        self.assertTrue(self.meter.reconcile(attempt,'returned',usage=FIXTURE['usage'],result={'result_id':'recovered'},price=FIXTURE['price'])['duplicate'])

    def test_metrics_conflict_content_fields_and_price_validation(self):
        ident=self.attempt('bytes');self.meter.metric(ident,'input_bytes',123,details={'source_revision':'synthetic-v1'})
        self.meter.metric(ident,'input_bytes',123,details={'source_revision':'synthetic-v1'})
        with self.assertRaises(Conflict):self.meter.metric(ident,'input_bytes',124,details={'source_revision':'synthetic-v1'})
        with self.assertRaises(ValueError):self.meter.metric(self.attempt('private'),'input_bytes',2,details={'text':'private forbidden'})
        self.assertEqual(self.meter.status()['metrics']['input_bytes'],123)
        attempt=self.attempt('invalidprice');self.meter.reserve(attempt,'job1','observer')
        with self.assertRaises(ValueError):self.meter.finish(attempt,'returned',usage=FIXTURE['usage'],price=FIXTURE['price']|{'input':-1})
        with self.assertRaises(ValueError):self.meter.finish(attempt,'returned',usage={'input_tokens':True})

    def test_enabled_backend_quota_and_expired_reservation_dispatch(self):
        first=self.attempt('reserved');self.meter.reserve(first,'job1','observer')
        with self.assertRaises(AdmissionExhausted):self.meter.reserve(self.attempt('busy'),'job2','observer')
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE cloud_usage_attempts SET lease_until=0 WHERE id=%s',(first,))
        with self.assertRaises(Conflict):self.meter.dispatched(first)
        self.meter.configure(enabled=False)
        with self.assertRaises(AdmissionExhausted):self.meter.reserve(self.attempt('disabled'),'job2','observer')

    def test_two_independent_process_admission_is_atomic(self):
        # Both processes receive only the already configured synthetic test DSN.
        code="""
import os,sys,tempfile
from types import SimpleNamespace
from agenthub.postgres import PostgresEnterpriseStore
from agenthub.cloud_ops import Meter,AdmissionExhausted
with tempfile.TemporaryDirectory() as home:
 store=PostgresEnterpriseStore(home,os.environ['CLOUD_TEST_DSN'],os.environ.get('CLOUD_TEST_TENANT','acme'))
 meter=Meter(SimpleNamespace(tenant_id=os.environ['METER_TEST_NAMESPACE'],open=store.open))
 sys.stdin.readline()
 try:
  meter.reserve(os.environ['METER_TEST_ATTEMPT'],'race-job','observer');print('reserved',flush=True)
 except AdmissionExhausted: print('exhausted',flush=True)
"""
        processes=[]
        for number in range(2):
            environment=os.environ.copy();environment['METER_TEST_NAMESPACE']=self.tenant;environment['METER_TEST_ATTEMPT']=self.attempt(str(number))
            processes.append(subprocess.Popen([sys.executable,'-c',code],env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        for process in processes:process.stdin.write('\n');process.stdin.flush()
        outcomes=[]
        for process in processes:
            stdout,stderr=process.communicate(timeout=20)
            self.assertEqual(process.returncode,0,'independent reservation process failed')
            outcomes.append(stdout.strip())
        self.assertEqual(sorted(outcomes),['exhausted','reserved']);self.assertEqual(self.meter.status()['attempts'],1)


if __name__=='__main__':unittest.main()
