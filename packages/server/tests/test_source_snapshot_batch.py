"""Frozen controls: source snapshot checks must share indexed policy and bound SQL."""
import hashlib
import unittest
from unittest.mock import patch
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_runtime import CloudStore
from agenthub.postgres import PostgresConnection
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class SourceSnapshotBatch(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.ctx=self.store.authenticate(self.tokens['alice'])

    def tearDown(self):self.postgres_teardown()

    def source(self,i=0,visibility='private'):
        body='Cedar report is saved at /synthetic/cedar.csv. '+str(i)
        sid=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'snapshot-'+str(i),
            'session':'snapshot','turn':str(i),'project':'maple','kind':'Stop',
            'body':body,'occurred_at':100+i,'visibility':visibility})['source_id']
        return sid,{'version':1,'canonical_sha256':hashlib.sha256(body.encode()).hexdigest()}

    def check(self,expected,ctx=None):
        from agenthub.search_permissions import validate_source_snapshots
        with self.store.open() as state:
            validate_source_snapshots(self.store,state.db,ctx or self.ctx,expected)

    def test_query_count_is_constant_for_one_and_forty_sources(self):
        snapshots=dict(self.source(i) for i in range(40));counts=[]
        original=PostgresConnection.execute
        for expected in (dict(list(snapshots.items())[:1]),snapshots):
            statements=[]
            def counted(db,sql,params=None):
                statements.append(sql);return original(db,sql,params)
            with patch.object(PostgresConnection,'execute',counted):self.check(expected)
            counts.append(len(statements))
            self.assertTrue(any('search_source_policies' in s for s in statements))
        self.assertEqual(counts[0],counts[1]);self.assertLessEqual(counts[1],3)

    def test_large_requested_batch_is_filtered_before_policy_joins(self):
        sid,snapshot=self.source()
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO memories (id,project,session,kind,body,created,active)
                SELECT 'snapshot-noise-' || n::text,project,session,kind,body,created,active
                FROM memories CROSS JOIN generate_series(1,4000) n WHERE id=?''',(sid,))
            state.db.execute('''INSERT INTO enterprise_sources
                (id,tenant,owner,external_project,internal_project,external_id,enrollment,
                 payload_hash,visibility,raw_visibility,active,policy_version,source_version,
                 occurred_at,created,occurred_precision,occurred_timezone)
                SELECT 'snapshot-noise-' || n::text,tenant,owner,external_project,
                    internal_project,'noise-' || n::text,enrollment,payload_hash,
                    visibility,raw_visibility,active,policy_version,source_version,
                    occurred_at,created,occurred_precision,occurred_timezone
                FROM enterprise_sources CROSS JOIN generate_series(1,4000) n WHERE id=?''',(sid,))
        expected={'snapshot-noise-'+str(i):snapshot for i in range(1,1001)}
        original=PostgresConnection.execute;plans=[]
        def measured(db,sql,params=None):
            if 'body_sha256' in sql:
                plans.append(original(db,'EXPLAIN (ANALYZE, FORMAT JSON) '+sql,params).fetchone()[0][0]['Plan'])
            return original(db,sql,params)
        with patch.object(PostgresConnection,'execute',measured):self.check(expected)
        def nodes(plan):
            yield plan
            for child in plan.get('Plans',[]):yield from nodes(child)
        self.assertEqual(len(plans),1)
        # Do not pin an index choice: constrain actual provenance-row work.
        for node in nodes(plans[0]):
            if node.get('Relation Name')=='enterprise_sources':
                self.assertLessEqual(node['Actual Loops'],1000)
        self.assertTrue(any(node.get('CTE Name')=='requested_sources' for node in nodes(plans[0])))

    def test_current_mutation_and_missing_source_fail_closed(self):
        expected=dict([self.source()]);sid=next(iter(expected));self.check(expected)
        for sql in ('UPDATE memories SET body=body || ? WHERE id=?',
                    'UPDATE enterprise_sources SET source_version=source_version+1 WHERE id=?',
                    'UPDATE enterprise_sources SET active=0 WHERE id=?'):
            with self.store.open() as state:
                params=(' changed',sid) if 'body=' in sql else (sid,)
                state.db.execute(sql,params)
                from agenthub.search_permissions import validate_source_snapshots
                with self.assertRaises(PermissionError):validate_source_snapshots(self.store,state.db,self.ctx,expected)
                state.db.rollback()
        with self.assertRaises(PermissionError):self.check({'missing':next(iter(expected.values()))})

    def test_raw_owner_rule_acl_and_current_identity(self):
        expected=dict([self.source(visibility='organization')]);sid=next(iter(expected));self.check(expected)
        with self.assertRaises(PermissionError):self.check(expected,self.store.authenticate(self.tokens['bob']))
        self.store.set_source_acl_freshness(self.ctx,sid,state='stale')
        with self.assertRaises(PermissionError):self.check(expected)
        self.store.set_source_acl_freshness(self.ctx,sid,state='current');self.check(expected)
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE enterprise_credentials SET active=0 WHERE principal='alice'")
        from agenthub.enterprise import Denied
        with self.assertRaises(Denied):self.check(expected)

    def test_source_read_cannot_be_replaced_by_document_read(self):
        expected=dict([self.source()])
        token=self.store.enroll('acme','alice','read-only',['read'])
        from agenthub.enterprise import Denied
        with self.assertRaises(Denied):self.check(expected,self.store.authenticate(token))

    def test_documents_use_shared_predicate_and_recheck_revisions(self):
        from agenthub.search_permissions import validate_document_snapshots
        sid,_=self.source();doc=self.store.accept_reviewed_note(self.ctx,sid,'Cedar location',
            'Cedar report is saved at /synthetic/cedar.csv.')['document_id']
        with self.store.open() as state:
            revision=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(doc,)).fetchone()[0]
            expected=[{'id':doc,'revision':revision}]
            with patch.object(self.store,'_document_allowed',side_effect=AssertionError('per-ID query')):
                validate_document_snapshots(self.store,state.db,self.ctx,expected)
            with self.assertRaises(PermissionError):
                validate_document_snapshots(self.store,state.db,self.ctx,[{'id':doc,'revision':'old'}])
        with self.store.open() as state,state.db:
            state.db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?',(sid,))
        with self.store.open() as state,self.assertRaises(PermissionError):
            validate_document_snapshots(self.store,state.db,self.ctx,expected)

    def test_delegation_is_current(self):
        expected=dict([self.source()]);sid=next(iter(expected))
        self.store.set_delegation('acme','bob','alice',['read','source_read'],['maple'],True)
        token=self.store.enroll('acme','bob','delegated',['read','source_read'],acting_for='alice')
        ctx=self.store.authenticate(token);self.check(expected,ctx)
        self.store.create_project('acme','oak')
        self.store.set_delegation('acme','bob','alice',['read','source_read'],['oak'],True)
        with self.assertRaises(PermissionError):self.check(expected,ctx)

    def test_connection_readers_freshness_and_missing_projection(self):
        expected=dict([self.source()]);sid=next(iter(expected))
        bob=self.store.authenticate(self.tokens['bob'])
        self.store.enroll_connection(bob,'snapshot-connection','test','maple',['document'],reader_ids=['alice'])
        with self.store.open() as state,state.db:
            state.db.execute('''INSERT INTO backend_source_revisions
                (source_id,connection,external_id,revision,digest,payload,received,disposition,policy_version)
                VALUES(?,?,?,?,?,?,?,?,?)''',(sid,'snapshot-connection','original','1','digest','{}',100,'accepted',1))
        self.check(expected)
        for sql in ("UPDATE backend_connections SET reader_ids='[\"bob\"]' WHERE id='snapshot-connection'",
                    "UPDATE backend_connections SET permission_observed=0 WHERE id='snapshot-connection'",
                    'DELETE FROM search_source_policies WHERE source_id=?'):
            with self.store.open() as state:
                state.db.execute(sql,(sid,) if '?' in sql else ())
                from agenthub.search_permissions import validate_source_snapshots
                with self.assertRaises(PermissionError):validate_source_snapshots(self.store,state.db,self.ctx,expected)
                state.db.rollback()
