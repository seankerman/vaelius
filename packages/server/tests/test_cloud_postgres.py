"""Current canonical pipeline on real PostgreSQL and separate application roles.

Requires an explicitly supplied private services manifest. Tests create synthetic
schemas, never inspect a founder database, and invoke no provider. Existing worker
fixtures run unchanged through the PostgreSQL store factory.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agentclient.enterprise_contract import VERSION
from agenthub.enterprise import Denied, Conflict
from agenthub.postgres import connect, migrate, PostgresEnterpriseStore, TenantRegistry, _bind


SERVICES = os.environ.get("AGENTNETWORK_PG_SERVICES")


class BindingTests(unittest.TestCase):
    def test_parameter_binding_preserves_literals_and_native_placeholders(self):
        self.assertEqual(_bind("SELECT '?' AS literal,? AS bound"), "SELECT '?' AS literal,%s AS bound")
        self.assertEqual(_bind("SELECT %s AS bound"), "SELECT %s AS bound")


class PostgresFixture:
    def postgres_setup(self, *, tenant_id="acme", bootstrap=True):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        self.services = json.loads(Path(SERVICES).read_text())
        item = self.services["tenants"]["acme"]
        self.schema = "test_storage_" + uuid.uuid4().hex[:12]
        self.temp = tempfile.TemporaryDirectory(prefix="agentnetwork-pg-")
        with connect(item["admin_dsn"]) as db:
            db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        options = "-c search_path=" + self.schema + ",public"
        self.admin_dsn = make_conninfo(item["admin_dsn"], options=options)
        self.dsn = make_conninfo(item["dsn"], options=options)
        migrate(self.admin_dsn)
        from agenthub.cloud_profile import _grant
        _grant(self.admin_dsn,item['role'])
        self.store = PostgresEnterpriseStore(Path(self.temp.name) / "hub", self.dsn, tenant_id)
        if not bootstrap:
            return
        self.store.create_organization("acme")
        for principal in ("alice", "bob", "admin"):
            self.store.create_principal("acme", principal, settings_admin=principal == "admin")
        self.store.create_project("acme", "maple")
        for principal in ("alice", "bob"):
            self.store.set_membership("acme", "maple", principal, True)
        self.tokens = {name: self.store.enroll("acme", name, name,
            ["read", "settings"] if name == "admin" else ["ingest", "read", "source_read", "correct", "withdraw", "policy"])
            for name in ("alice", "bob", "admin")}
        self.ctx = self.store.authenticate(self.tokens["alice"])

    def postgres_teardown(self):
        from psycopg import sql
        with connect(self.services["tenants"]["acme"]["admin_dsn"]) as db:
            from agenthub.search_reader import reader_role
            role=reader_role(db.execute('SELECT current_database()').fetchone()[0],self.schema,self.services['tenants']['acme']['role'])
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
            db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        self.temp.cleanup()


@unittest.skipUnless(SERVICES, "explicit real PostgreSQL services manifest required")
class CanonicalPostgresTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()

    def tearDown(self):
        self.postgres_teardown()

    def ingest(self, *, external_id="dataset", visibility="private", body=None):
        return self.store.ingest(self.ctx, {"version": VERSION, "external_id": external_id,
            "session": "chat", "turn": "1", "project": "maple", "kind": "Stop",
            "body": body or "The dataset is saved at /tmp/synthetic/maple.csv.",
            "occurred_at": 12345, "visibility": visibility})

    def note(self, source):
        return self.store.accept_reviewed_note(self.ctx, source, "Maple dataset location",
            "The dataset is saved at /tmp/synthetic/maple.csv.")["document_id"]

    def test_canonical_private_consolidation_and_no_sqlite_fallback(self):
        first = self.ingest()["source_id"]; document = self.note(first)
        second = self.ingest(external_id="dataset-copy")["source_id"]
        self.assertEqual(self.note(second), document)
        self.assertEqual(self.ingest()["disposition"], "duplicate")
        with self.assertRaises(Conflict):
            self.ingest(body="A conflicting immutable revision.")
        for name in ("bob", "admin"):
            ctx = self.store.authenticate(self.tokens[name])
            with self.assertRaises(Denied):
                self.store.detail(ctx, document)
            self.assertEqual(self.store.search(ctx, {"version": VERSION, "query": "Maple dataset location"})["results"], [])
        self.assertEqual(len(self.store.search(self.ctx, {"version": VERSION, "query": "Maple dataset location"})["results"]), 1)
        self.assertEqual(list(Path(self.temp.name).rglob("*.sqlite")), [])

    def test_failed_transaction_rolls_back_claim_and_index_together(self):
        from agenthub.processing.knowledge import accept_local_candidate
        source = self.ingest()["source_id"]
        with self.assertRaises(RuntimeError):
            with self.store.open() as state, state.db:
                row = state.db.execute("SELECT internal_project FROM enterprise_sources WHERE id=?", (source,)).fetchone()
                accept_local_candidate(state.db, source, row[0], "chat", "Maple dataset location",
                    "The dataset is saved at /tmp/synthetic/maple.csv.", [source])
                raise RuntimeError("fixture_abort")
        with self.store.open() as state:
            self.assertEqual(state.db.execute("SELECT count(*) FROM knowledge_documents").fetchone()[0], 0)
            self.assertEqual(state.db.execute("SELECT count(*) FROM knowledge_fts").fetchone()[0], 0)

    def test_correction_withdrawal_and_temporal_unknown_interval(self):
        source = self.ingest()["source_id"]; document = self.note(source)
        query = {"version": VERSION, "query": "Where was the Maple dataset as of 2026-09-01?"}
        self.assertFalse(self.store.search(self.ctx, query)["answerable"])
        value = {"version": VERSION, "target_id": source, "expected_revision": "1",
            "idempotency_key": "withdraw-dataset", "reason": "fixture owner withdrew source", "operation": "withdraw"}
        self.store.lifecycle(self.ctx, value)
        self.assertEqual(self.store.lifecycle(self.ctx, value)["operation"], "withdraw")
        with self.assertRaises(Denied):
            self.store.detail(self.ctx, document)
        self.assertEqual(self.store.search(self.ctx, {"version": VERSION, "query": "Maple dataset location"})["results"], [])

    def test_correction_reuses_document_and_invalidates_old_revision(self):
        source = self.ingest()["source_id"]; document = self.note(source)
        old = self.store.detail(self.ctx, document)
        replacement = {"version": VERSION,"external_id":"corrected-dataset","session":"chat",
            "turn":"1","project":"maple","kind":"Stop","body":"The Maple dataset moved to /tmp/synthetic/corrected.csv.",
            "occurred_at":12346,"visibility":"private"}
        result = self.store.lifecycle(self.ctx,{"version":VERSION,"target_id":source,"expected_revision":"1",
            "idempotency_key":"correct-dataset","reason":"synthetic owner corrected location","operation":"correct",
            "replacement":{"source":replacement,"title":"Maple dataset location",
                "lesson":"The Maple dataset moved to /tmp/synthetic/corrected.csv."}})
        latest = self.store.detail(self.ctx, document)
        self.assertNotEqual(latest['revision'],old['revision'])
        self.assertIn('corrected.csv',latest['claim']['lesson'])
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT active FROM enterprise_sources WHERE id=?',(source,)).fetchone()[0],0)
            self.assertEqual(state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(document,)).fetchone()[0],result['replacement_source_id'])

    def test_delete_purges_source_and_derived_content_with_constraints(self):
        source = self.ingest()["source_id"]; document = self.note(source)
        self.store.lifecycle(self.ctx,{"version":VERSION,"target_id":source,"expected_revision":"1",
            "idempotency_key":"delete-dataset","reason":"synthetic retention","operation":"delete"})
        with self.store.open() as state:
            self.assertEqual(state.db.execute('SELECT body FROM memories WHERE id=?',(source,)).fetchone()[0],'')
            self.assertEqual(state.db.execute('SELECT claim_json FROM knowledge_revisions WHERE document_id=?',(document,)).fetchone()[0],'{}')
            self.assertEqual(state.db.execute('SELECT count(*) FROM knowledge_fts WHERE document_id=?',(document,)).fetchone()[0],0)

    def test_team_revocation_and_delegation_are_current(self):
        source = self.ingest(visibility="team")["source_id"]; document = self.note(source)
        bob = self.store.authenticate(self.tokens["bob"])
        self.assertIsNotNone(self.store.detail(bob, document))
        self.store.set_membership("acme", "maple", "bob", False)
        with self.assertRaises(Denied):
            self.store.detail(bob, document)
        self.store.set_membership("acme", "maple", "bob", True)
        private = self.ingest(external_id="private-delegated")["source_id"]
        private_document = self.note(private)
        self.store.set_delegation("acme", "bob", "alice", ["read"], ["maple"], True)
        token = self.store.enroll("acme", "bob", "delegate", ["read"], acting_for="alice")
        self.assertIsNotNone(self.store.detail(self.store.authenticate(token), private_document))
        self.store.set_delegation("acme", "bob", "alice", ["read"], ["maple"], False)
        with self.assertRaises(Denied):
            self.store.authenticate(token)

    def test_application_role_cannot_open_other_tenant_or_create_schema(self):
        from psycopg.conninfo import make_conninfo
        import psycopg
        other = make_conninfo(self.services["tenants"]["acme"]["dsn"], dbname=self.services["tenants"]["bravo"]["database"])
        with self.assertRaises(psycopg.OperationalError):
            connect(other)
        with connect(self.dsn) as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("CREATE TABLE forbidden_ddl(id TEXT)")
        with self.assertRaises(Denied):
            self.store.create_organization("bravo")

    def test_server_owned_registry_route_and_disable(self):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        control_schema='control_'+self.schema
        with connect(self.services['admin_dsn']) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(control_schema)))
        try:
            control=make_conninfo(self.services['admin_dsn'],options='-c search_path='+control_schema+',public')
            migrate(control,control=True)
            registry = TenantRegistry(control, Path(self.temp.name) / "registry")
            registry.register("acme", self.dsn)
            registry.bind_credential(self.tokens["alice"], "acme")
            self.assertEqual(registry.store_for_token(self.tokens["alice"]).tenant_id, "acme")
            with self.assertRaises(Denied):registry.store_for_token("forged-tenant-acme")
            with self.assertRaises(Denied):registry.resolve("unknown")
            registry.set_active("acme", False)
            with self.assertRaises(Denied):
                registry.store_for_token(self.tokens["alice"])
        finally:
            with connect(self.services['admin_dsn']) as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(control_schema)))

    def test_schema_checksum_and_database_outage_fail_without_sqlite(self):
        from psycopg.conninfo import make_conninfo
        import psycopg
        with connect(self.admin_dsn) as db:
            db.execute("UPDATE cloud_schema SET sha256=%s WHERE version=1",('0'*64,))
        with self.assertRaisesRegex(RuntimeError,'postgres_schema_version_or_checksum_mismatch'):
            PostgresEnterpriseStore(Path(self.temp.name)/'mismatch',self.dsn,'acme')
        unavailable=make_conninfo(self.dsn,host='127.0.0.1',port=1,connect_timeout=1)
        with self.assertRaises(psycopg.OperationalError):
            PostgresEnterpriseStore(Path(self.temp.name)/'outage',unavailable,'acme')
        self.assertEqual(list(Path(self.temp.name).rglob('*.sqlite')),[])


if __name__ == "__main__":
    unittest.main()
