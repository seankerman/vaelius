"""Frozen synthetic reader isolation and concurrency contract; no model calls."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_staging_retrieval_sql import CompletePolicyEquivalence


class SearchReader(CompletePolicyEquivalence):
    def test_search_role_cannot_read_noncurrent_revision_or_fts_by_omission(self):
        _, document=self.note('revision-isolation')
        with self.store.open() as state,state.db:
            old=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(document,)).fetchone()[0]
            new='synthetic-current-revision'
            state.db.execute('''INSERT INTO knowledge_revisions
                SELECT ?,document_id,revision_number+1,claim_json,identity_json,identity_hash,
                    revision_id,'synthetic correction',schema_version,created+1
                FROM knowledge_revisions WHERE revision_id=?''',(new,old))
            state.db.execute('UPDATE knowledge_documents SET active_revision_id=? WHERE document_id=?',(new,document))
            state.db.execute('UPDATE enterprise_documents SET revision=? WHERE id=?',(new,document))
            state.db.execute('INSERT INTO knowledge_fts SELECT document_id,?,body FROM knowledge_fts WHERE revision_id=?',(new,old))
        with self.store.search_reader(self.contexts['alice']) as state:
            self.assertEqual({r[0] for r in state.db.execute('SELECT revision_id FROM knowledge_revisions')},{new})
            self.assertEqual({r[0] for r in state.db.execute('SELECT revision_id FROM knowledge_fts')},{new})
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_revisions WHERE document_id=?',(document,)).fetchone()[0],2)

    def test_database_filters_private_content_without_application_where(self):
        _, private = self.note('private-reader')
        _, team = self.note('team-reader', visibility='team')
        for actor, expected in [('alice', {private, team}), ('bob', {team}), ('admin', set())]:
            with self.store.search_reader(self.contexts[actor]) as state:
                self.assertEqual({r[0] for r in state.db.execute('SELECT document_id FROM knowledge_documents')}, expected)
                self.assertEqual({r[0] for r in state.db.execute('SELECT document_id FROM knowledge_revisions')}, expected)
                self.assertEqual({r[0] for r in state.db.execute('SELECT document_id FROM knowledge_fts')}, expected)
                from psycopg.errors import InsufficientPrivilege
                with self.assertRaises(InsufficientPrivilege):
                    state.db.execute("UPDATE knowledge_documents SET lifecycle='active'")

    def test_absent_context_denies_and_reader_cannot_read_raw_sources(self):
        self.note('reader-context')
        with self.store.search_reader(self.contexts['alice']) as state:
            state.db.execute("SELECT set_config('agenthub.search_context','',true)")
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_revisions').fetchone()[0], 0)
            from psycopg.errors import InsufficientPrivilege
            with self.assertRaises(InsufficientPrivilege):
                state.db.execute('SELECT * FROM enterprise_sources')
        with self.store.search_reader(self.contexts['bob']) as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_revisions').fetchone()[0], 0)
        with self.store.search_reader(self.contexts['alice']) as state:
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_revisions').fetchone()[0], 1)

    def test_forged_identity_cannot_install_reader_context(self):
        from agenthub.enterprise import Denied
        self.note('reader-identity')
        for field, value in [('actor', 'bob'), ('tenant', 'other-tenant')]:
            ctx = dict(self.contexts['alice'], **{field: value})
            with self.assertRaises(Denied), self.store.search_reader(ctx):
                pass

    def test_operator_review_cannot_expand_to_unselected_documents(self):
        source, selected=self.note('review-selected')
        self.note('review-unselected')
        from agenthub.draft_review import review_session
        with self.store.open() as state:
            revision=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(selected,)).fetchone()[0]
        with review_session(self.store,self.contexts['alice'],{selected:revision},source_ids=[source]):
            with self.store.search_reader(self.contexts['alice']) as state:
                self.assertEqual({r[0] for r in state.db.execute('SELECT document_id FROM knowledge_documents')},{selected})

    def test_context_is_removed_on_same_connection_and_rls_cannot_be_disabled(self):
        from agenthub.search_reader import restrict
        from psycopg.errors import InsufficientPrivilege
        self.note('context-reset')
        with self.store.open() as state:
            worker=state.db.execute('SELECT current_user').fetchone()[0]
            with restrict(self.store,state,self.contexts['alice']):
                self.assertNotEqual(state.db.execute('SELECT current_user').fetchone()[0],worker)
            self.assertEqual(state.db.execute('SELECT current_user').fetchone()[0],worker)
            self.assertEqual(state.db.execute("SELECT current_setting('agenthub.search_context')").fetchone()[0],'')
        with self.store.search_reader(self.contexts['bob']) as state:
            state.db.execute('SET LOCAL row_security=off')
            with self.assertRaises(InsufficientPrivilege):
                state.db.execute('SELECT * FROM knowledge_revisions')

    def test_readers_overlap_and_policy_writer_waits_for_delivery(self):
        barrier = threading.Barrier(2)
        def reader():
            with self.store.delivery_read_lock():
                barrier.wait(timeout=3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reader) for _ in range(2)]
            for future in futures:
                future.result(timeout=5)
        entered = threading.Event()
        def writer():
            with self.store.delivery_lock():
                entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.delivery_read_lock():
                future = pool.submit(writer)
                self.assertFalse(entered.wait(.15))
            future.result(timeout=5)
            self.assertTrue(entered.is_set())

    def test_unrelated_source_policy_writes_do_not_serialize(self):
        self.note('existing-team-policy', visibility='team')
        first, _ = self.note('first-policy')
        second, _ = self.note('second-policy')
        barrier = threading.Barrier(2)
        def change(source):
            with self.store.open() as state, state.db:
                state.db.execute("UPDATE enterprise_sources SET visibility='team' WHERE id=?", (source,))
                barrier.wait(timeout=3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, source) for source in [first, second]]
            for future in futures:
                future.result(timeout=5)

    def test_http_ranks_without_policy_lock_and_rechecks_after_revocation(self):
        from starlette.testclient import TestClient
        from agenthub.cloud_api import create_app
        from agentclient.enterprise_contract import VERSION
        source, _=self.note('http-delivery')
        store=self.store
        class Registry:
            def store_for_token(self,token):return store
        original=store.search
        def rank_then_revoke(ctx,value):
            result=original(ctx,value)
            self.assertTrue(result['results'])
            store.lifecycle(ctx,{'version':VERSION,'target_id':source,'expected_revision':'1',
                'idempotency_key':'http-revoke','reason':'Synthetic concurrent revocation','operation':'withdraw'})
            return result
        with patch.object(store,'search',side_effect=rank_then_revoke):
            with TestClient(create_app(Registry(),allowed_hosts=['testserver'])) as client:
                response=client.post('/enterprise/v3/search',json={'version':VERSION,'query':'http-delivery gauge reference'},
                    headers={'Authorization':'Bearer '+self.tokens['alice']})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['results'],[])
        self.assertFalse(response.json()['answerable'])
