"""Frozen accepted-update freshness cases; SQLite is only a provider-free test double."""
import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from agenthub.cloud_maintenance import IndexFreshness, run_fair_index_workers
from agentclient.enterprise_contract import VERSION
from test_cloud_postgres import PostgresFixture, SERVICES


FIXTURE = Path(__file__).resolve().parent.parent / 'tests/fixtures/service/cloud_index_freshness_v1.json'
FIXTURE_SHA = '0028870b6d8bcc02070c053be7548dce5f86ad44b07fc683c488afa4c18066ef'


class _Database:
    def __init__(self, connection):
        self.connection = connection
        self.dialect = 'postgres'

    def execute(self, sql, args=()):
        return self.connection.execute(sql.replace('?::vector', '?'), args)

    def __enter__(self):
        self.connection.execute('BEGIN IMMEDIATE')
        return self

    def __exit__(self, error_type, error, traceback):
        self.connection.rollback() if error_type else self.connection.commit()


class _State:
    def __init__(self, connection):
        self.db = _Database(connection)


class _Store:
    def __init__(self, path, tenant='acme'):
        self.path = path
        self.tenant_id = tenant
        self.lock = threading.RLock()
        with self.open() as state, state.db:
            state.db.execute('CREATE TABLE outbox(id TEXT PRIMARY KEY,payload TEXT,status TEXT DEFAULT \'pending\', attempts INTEGER DEFAULT 0,last_error TEXT,response TEXT,next_attempt REAL DEFAULT 0)')
            state.db.execute('CREATE TABLE knowledge_documents(document_id TEXT PRIMARY KEY,active_revision_id TEXT,lifecycle TEXT)')
            state.db.execute('CREATE TABLE knowledge_revisions(revision_id TEXT PRIMARY KEY,claim_json TEXT)')
            state.db.execute('CREATE TABLE enterprise_documents(id TEXT PRIMARY KEY,tenant TEXT,internal_project TEXT,revision TEXT,active INTEGER,blocked_reason TEXT)')
            state.db.execute('CREATE TABLE enterprise_dependencies(document_id TEXT,source_id TEXT)')
            state.db.execute('CREATE TABLE enterprise_sources(id TEXT PRIMARY KEY,active INTEGER,tenant TEXT,internal_project TEXT)')
            state.db.execute('CREATE TABLE cloud_vector_state(singleton INTEGER PRIMARY KEY,generation_id TEXT,model_key TEXT,updated REAL)')
            state.db.execute('CREATE TABLE cloud_vector_generations(id TEXT PRIMARY KEY,model_key TEXT,status TEXT)')
            state.db.execute('CREATE TABLE cloud_document_vectors(generation_id TEXT,document_id TEXT,revision_id TEXT,body_sha256 TEXT,embedding TEXT,PRIMARY KEY(generation_id,document_id))')
            state.db.execute("INSERT INTO cloud_vector_generations VALUES('g1','test-model','active')")
            state.db.execute("INSERT INTO cloud_vector_state VALUES(1,'g1','test-model',0)")

    @contextmanager
    def open(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield _State(connection)
        finally:
            connection.close()

    @contextmanager
    def delivery_lock(self):
        with self.lock:
            yield

    def accept(self, document_id, revision_id, claim):
        with self.open() as state, state.db:
            source_id='source-'+document_id
            state.db.execute('INSERT INTO enterprise_sources VALUES(?,1,?,\'enterprise:test\') ON CONFLICT(id) DO NOTHING',(source_id,self.tenant_id))
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?)',(document_id,source_id))
            state.db.execute('INSERT INTO knowledge_revisions VALUES(?,?)',(revision_id,json.dumps(claim)))
            state.db.execute("INSERT INTO knowledge_documents VALUES(?,?,'active') ON CONFLICT(document_id) DO UPDATE SET active_revision_id=excluded.active_revision_id",(document_id,revision_id))
            state.db.execute("INSERT INTO enterprise_documents VALUES(?,?,'enterprise:test',?,1,'') ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1",(document_id,self.tenant_id,revision_id))


class _Embedder:
    def __init__(self, failure=False, callback=None):
        self.calls = 0
        self.failure = failure
        self.callback = callback

    def embed_documents(self, texts):
        self.calls += 1
        if self.callback:
            self.callback()
        if self.failure:
            raise RuntimeError('synthetic_embedding_failure')
        return [[1.0] + [0.0] * 511 for _ in texts]


class IndexFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = _Store(str(Path(self.temp.name) / 'index.sqlite'))
        self.index = IndexFreshness(self.store, model_key='test-model')
        self.fixture = json.loads(FIXTURE.read_text())

    def tearDown(self):
        self.temp.cleanup()

    def test_frozen_accepted_updates_reach_searchable_without_provider_calls(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA)
        for row in self.fixture['accepted_updates']:
            self.store.accept(row['document_id'],row['revision_id'],row['claim'])
        self.assertEqual(self.index.reconcile(max_documents=10, now=100)['queued'],2)
        self.assertEqual(self.index.status()['by_document'],self.fixture['expected']['after_reconcile'])
        embedder = _Embedder()
        self.assertEqual(self.index.run_once(embedder,now=101)['status'],'searchable')
        self.assertEqual(self.index.run_once(embedder,now=102)['status'],'searchable')
        self.assertEqual(self.index.status()['by_document'],self.fixture['expected']['after_worker'])
        self.assertEqual(embedder.calls,2)
        with self.store.open() as state:
            revisions={row['document_id']:row['revision_id'] for row in state.db.execute('SELECT document_id,revision_id FROM cloud_document_vectors')}
        self.assertEqual(revisions,{'doc-a':'revision-a2','doc-b':'revision-b1'})

    def test_expired_claim_recovers_and_poison_becomes_durable_failure(self):
        row=self.fixture['accepted_updates'][0]
        self.store.accept(row['document_id'],row['revision_id'],row['claim'])
        self.index.reconcile(now=100)
        first=self.index.claim(now=101,lease_seconds=2)
        self.assertIsNotNone(first)
        self.assertIsNone(self.index.claim(now=102,lease_seconds=2))
        self.assertEqual(self.index.run_once(_Embedder(failure=True),now=104)['status'],'pending')
        self.assertEqual(self.index.run_once(_Embedder(failure=True),now=106)['status'],'failed')
        self.assertEqual(self.index.status()['by_document']['doc-a'],'failed')
        self.assertEqual(self.index.retry('doc-a',now=111)['status'],'pending')
        self.assertEqual(self.index.run_once(_Embedder(),now=112)['status'],'searchable')

    def test_generation_or_revision_change_cannot_publish_stale_vector(self):
        row=self.fixture['accepted_updates'][0]
        self.store.accept(row['document_id'],row['revision_id'],row['claim'])
        self.index.reconcile(now=100)
        def replace_generation():
            with self.store.open() as state,state.db:
                state.db.execute("UPDATE cloud_vector_generations SET status='retired' WHERE id='g1'")
                state.db.execute("INSERT INTO cloud_vector_generations VALUES('g2','test-model','active')")
                state.db.execute("UPDATE cloud_vector_state SET generation_id='g2' WHERE singleton=1")
        self.assertEqual(self.index.run_once(_Embedder(callback=replace_generation),now=101)['status'],'pending')
        # Worker already requeued the current generation without a separate reconcile.
        self.assertEqual(self.index.reconcile(now=102)['queued'],0)
        self.assertEqual(self.index.run_once(_Embedder(),now=102)['status'],'searchable')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT generation_id FROM cloud_document_vectors').fetchone()[0],'g2')
        self.store.accept('doc-a','revision-a3',{'title':'A','lesson':'Newer synthetic location.','applicability':'team'})
        self.assertEqual(self.index.reconcile(now=103)['queued'],1)
        self.assertEqual(self.index.status()['by_document']['doc-a'],'pending')

    def test_source_denial_during_embedding_never_publishes(self):
        row=self.fixture['accepted_updates'][0]
        self.store.accept(row['document_id'],row['revision_id'],row['claim'])
        self.index.reconcile(now=100)
        def revoke_source():
            with self.store.open() as state,state.db:
                state.db.execute("UPDATE enterprise_sources SET active=0 WHERE id='source-doc-a'")
        self.assertEqual(self.index.run_once(_Embedder(callback=revoke_source),now=101)['status'],'no_learning')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_document_vectors').fetchone()[0],0)
        self.assertEqual(self.index.status()['by_document']['doc-a'],'no_learning')

    def test_readiness_rechecks_current_source_and_vector_generation(self):
        row=self.fixture['accepted_updates'][0]
        self.store.accept(row['document_id'],row['revision_id'],row['claim'])
        self.index.reconcile(now=100)
        self.index.run_once(_Embedder(),now=101)
        self.assertEqual(self.index.status()['by_document']['doc-a'],'searchable')
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE cloud_vector_generations SET status='retired' WHERE id='g1'")
            state.db.execute("INSERT INTO cloud_vector_generations VALUES('g2','test-model','active')")
            state.db.execute("UPDATE cloud_vector_state SET generation_id='g2' WHERE singleton=1")
        self.assertEqual(self.index.status()['by_document']['doc-a'],'pending')
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE enterprise_sources SET active=0 WHERE id='source-doc-a'")
        self.assertEqual(self.index.status()['by_document']['doc-a'],'no_learning')

    def test_reconciliation_cursor_progresses_past_poisoned_earlier_document(self):
        for name in ('a','b','c'):
            self.store.accept(name,'revision-'+name,{'title':name,'lesson':'Synthetic','applicability':'team'})
        first=self.index.reconcile(max_documents=1,now=100)
        self.assertEqual(first['queued'],1)
        self.assertTrue(first['more'])
        second=self.index.reconcile(max_documents=1,after_document_id=first['next_after_document_id'],now=100)
        self.assertEqual(second['queued'],1)
        third=self.index.reconcile(max_documents=1,after_document_id=second['next_after_document_id'],now=100)
        self.assertEqual(third['queued'],1)
        self.assertFalse(third['more'])
        self.assertEqual(self.index.status()['counts']['pending'],3)

    def test_expired_poison_claim_does_not_starve_next_document(self):
        for name in ('a','b'):
            self.store.accept(name,'revision-'+name,{'title':name,'lesson':'Synthetic','applicability':'team'})
        self.index.reconcile(now=100)
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE outbox SET status='running',attempts=3,next_attempt=0 WHERE id=?",
                (self.index._id('a'),))
        self.assertEqual(self.index.run_once(_Embedder(),now=101)['status'],'searchable')
        self.assertEqual(self.index.status()['by_document'],{'a':'failed','b':'searchable'})

    def test_round_robin_gives_each_tenant_one_turn(self):
        other=_Store(str(Path(self.temp.name)/'other.sqlite'),tenant='bravo')
        for document in ('a1','a2'):
            self.store.accept(document,'revision-'+document,{'title':document,'lesson':'Synthetic','applicability':'team'})
        other.accept('b1','revision-b1',{'title':'B','lesson':'Synthetic','applicability':'team'})
        first=IndexFreshness(self.store,model_key='test-model')
        second=IndexFreshness(other,model_key='test-model')
        first.reconcile(now=100);second.reconcile(now=100)
        result=run_fair_index_workers([first,second],_Embedder(),max_jobs=2,now=101)
        self.assertEqual([item['tenant'] for item in result['results']],['acme','bravo'])
        self.assertEqual(first.status()['counts']['pending'],1)


@unittest.skipUnless(SERVICES,'explicit synthetic PostgreSQL services manifest required')
class IndexFreshnessPostgresTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        # Source-only fixture: the installed build/pin gate is checked in H9.
        pin_context = nullcontext() if os.environ.get('AGENTNETWORK_INSTALLED_TEST') == '1' else patch('agenthub.pipeline_pin.verify',return_value={})
        with pin_context:
            self.postgres_setup()
        self.source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'h8-accepted-update',
            'session':'synthetic','turn':'1','project':'maple','kind':'Stop',
            'body':'Synthetic accepted dataset location is /synthetic/h8.csv.',
            'occurred_at':100,'visibility':'team'})['source_id']
        self.document=self.store.accept_reviewed_note(self.ctx,self.source,'Synthetic H8 location',
            'The synthetic accepted dataset location is /synthetic/h8.csv.')['document_id']
        with self.store.open() as state,state.db:
            state.db.execute("INSERT INTO cloud_vector_generations(id,model_key,dimension,status,created) VALUES('h8-fixture-g1','test-model',512,'active',0)")
            state.db.execute("INSERT INTO cloud_vector_state VALUES(1,'h8-fixture-g1','test-model',0)")
        self.index=IndexFreshness(self.store,model_key='test-model')

    def tearDown(self):self.postgres_teardown()

    def test_accepted_note_reconciles_and_publishes_current_vector(self):
        self.assertEqual(self.index.reconcile(max_documents=10,now=100)['queued'],1)
        self.assertEqual(self.index.status()['by_document'][self.document],'pending')
        embedder=_Embedder()
        self.assertEqual(self.index.run_once(embedder,now=101)['status'],'searchable')
        self.assertEqual(embedder.calls,1)
        self.assertEqual(self.index.status()['by_document'][self.document],'searchable')
        with self.store.open() as state:
            row=state.db.execute('''SELECT v.revision_id,d.active_revision_id,v.generation_id
                FROM cloud_document_vectors v JOIN knowledge_documents d ON d.document_id=v.document_id
                WHERE v.document_id=?''',(self.document,)).fetchone()
            self.assertEqual(row['revision_id'],row['active_revision_id'])
            self.assertEqual(row['generation_id'],'h8-fixture-g1')

    def test_source_revocation_during_embedding_denies_publication(self):
        self.index.reconcile(now=100)
        def revoke():
            with self.store.open() as state,state.db:
                state.db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?',(self.source,))
        self.assertEqual(self.index.run_once(_Embedder(callback=revoke),now=101)['status'],'no_learning')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM cloud_document_vectors').fetchone()[0],0)

    def test_generation_replacement_requeues_to_current_generation(self):
        self.index.reconcile(now=100)
        def replace():
            with self.store.open() as state,state.db:
                state.db.execute("UPDATE cloud_vector_generations SET status='retired' WHERE id='h8-fixture-g1'")
                state.db.execute("INSERT INTO cloud_vector_generations(id,model_key,dimension,status,created) VALUES('h8-fixture-g2','test-model',512,'active',101)")
                state.db.execute("UPDATE cloud_vector_state SET generation_id='h8-fixture-g2' WHERE singleton=1")
        self.assertEqual(self.index.run_once(_Embedder(callback=replace),now=101)['status'],'pending')
        # Worker already requeued the current generation without a separate reconcile.
        self.assertEqual(self.index.reconcile(now=102)['queued'],0)
        self.assertEqual(self.index.run_once(_Embedder(),now=102)['status'],'searchable')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT generation_id FROM cloud_document_vectors WHERE document_id=?',
                (self.document,)).fetchone()[0],'h8-fixture-g2')

    def test_accepted_note_survives_post_commit_refresh_failure_with_durable_index_job(self):
        """A crash at the refresh boundary cannot strand an accepted claim."""
        from agenthub.cloud_runtime import CloudStore
        self.store=CloudStore(self.store.home,self.dsn,'acme')
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE cloud_vector_generations SET model_key=? WHERE id='h8-fixture-g1'",
                (self.store.semantic_model_key,))
            state.db.execute("UPDATE cloud_vector_state SET model_key=? WHERE singleton=1",
                (self.store.semantic_model_key,))
        source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'h8-crash-after-accept',
            'session':'synthetic','turn':'2','project':'maple','kind':'Stop',
            'body':'Synthetic crash-boundary location is /synthetic/crash.csv.',
            'occurred_at':101,'visibility':'team'})['source_id']
        with patch.object(self.store,'reconcile_indexes',side_effect=RuntimeError('synthetic_post_commit_crash')):
            with self.assertRaisesRegex(RuntimeError,'synthetic_post_commit_crash'):
                self.store.accept_reviewed_note(self.ctx,source,'Synthetic crash-boundary location',
                    'The synthetic crash-boundary location is /synthetic/crash.csv.')
        with self.store.open() as state:
            row=state.db.execute('''SELECT d.document_id,d.active_revision_id,e.revision
                FROM knowledge_documents d LEFT JOIN enterprise_documents e ON e.id=d.document_id
                WHERE d.origin_memory_id=?''',(source,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['active_revision_id'],row['revision'])
            job=state.db.execute('SELECT payload,status FROM outbox WHERE id=?',
                (IndexFreshness(self.store)._id(row['document_id']),)).fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job['status'],'pending')
            self.assertEqual(json.loads(job['payload'])['revision_id'],row['active_revision_id'])

    def test_withdrawal_racing_review_cannot_leave_a_new_active_note(self):
        """Withdrawal after its dependency scan must fence an in-flight review."""
        source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'h8-race-source',
            'session':'synthetic','turn':'3','project':'maple','kind':'Stop',
            'body':'Synthetic race marker is stored at /synthetic/race.csv.',
            'occurred_at':102,'visibility':'team'})['source_id']
        withdrawing=threading.Event();release=threading.Event();outcomes={}
        original=self.store._invalidate_processing_source
        def held_invalidation(db,source_id,reason):
            if source_id==source:
                withdrawing.set()
                if not release.wait(5):raise TimeoutError('synthetic_race_release_timeout')
            return original(db,source_id,reason)
        def withdraw():
            try:
                self.store.lifecycle(self.ctx,{'version':VERSION,'target_id':source,
                    'expected_revision':'1','idempotency_key':'h8-race-withdraw',
                    'reason':'synthetic source withdrawal','operation':'withdraw'})
                outcomes['withdraw']='applied'
            except Exception as error:outcomes['withdraw']=error
        def review():
            try:
                self.store.accept_reviewed_note(self.ctx,source,'Synthetic race location',
                    'The synthetic race marker is stored at /synthetic/race.csv.')
                outcomes['review']='accepted'
            except Exception as error:outcomes['review']=error
        with patch.object(self.store,'_invalidate_processing_source',side_effect=held_invalidation):
            first=threading.Thread(target=withdraw,daemon=True);second=threading.Thread(target=review,daemon=True)
            first.start()
            try:
                self.assertTrue(withdrawing.wait(5))
                second.start()
                second.join(timeout=1)
            finally:
                release.set();first.join(timeout=6);second.join(timeout=6)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(outcomes.get('withdraw'),'applied')
        self.assertNotEqual(outcomes.get('review'),'accepted')
        with self.store.open() as state:
            self.assertEqual(state.db.execute('''SELECT count(*) FROM enterprise_dependencies x
                JOIN enterprise_documents d ON d.id=x.document_id
                WHERE x.source_id=? AND d.active=1''',(source,)).fetchone()[0],0)


if __name__=='__main__':unittest.main()
