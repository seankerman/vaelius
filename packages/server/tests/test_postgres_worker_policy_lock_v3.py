"""Independent observers may read policy together; revocation still excludes them."""
import threading
import unittest
from agentclient.enterprise_capture import normalize_capture
from agenthub.backend_worker import Worker
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit real PostgreSQL services manifest required')
class WorkerPolicyLockTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        self.postgres_setup()
        self.store.curation_enabled=True  # Explicit optional-enrichment lock fixture.
        self.store.enroll_connection(self.ctx, 'agent', 'codex', 'maple', ['agent'])
        self.config = {'paused': False, 'knowledge_backend': {'mode': 'enterprise_local'},
            'observer': {'enabled': True, 'model': 'fixture'},
            'episode_curation': {'enabled': True, 'policy': 'durable_memory',
                                'generation_id': 'shared-policy-fixture', 'settle_seconds': 0}}
        for chat in ('one', 'two'):
            for kind in ('UserPromptSubmit', 'Stop'):
                self.store.ingest_general(self.ctx, normalize_capture({
                    'hook_event_name': kind, 'event_id': chat + kind,
                    'session_id': chat, 'turn_id': '1',
                    'prompt': 'Use CSV because its header is stable.',
                    'last_assistant_message': 'CSV preserves the stable header.',
                }, 'maple', 'agent'))
        self.worker = Worker(self.store, self.config, live=False)
        self.worker.prepare()
        self.jobs = [self.worker.claim(), self.worker.claim()]
        self.assertTrue(all(self.jobs))

    def tearDown(self):
        self.postgres_teardown()

    def test_readers_do_not_serialize_but_policy_writer_waits(self):
        ready = threading.Event()
        release = threading.Event()
        writer_entered = threading.Event()
        errors = []

        def held_guard():
            try:
                with self.store.open() as state, state.db:
                    self.worker.guard(state.db, self.jobs[0])
                    ready.set()
                    release.wait(4)
            except Exception as exc:
                errors.append(type(exc).__name__)

        def policy_writer():
            try:
                with self.store.delivery_lock():
                    writer_entered.set()
            except Exception as exc:
                errors.append(type(exc).__name__)

        first = threading.Thread(target=held_guard)
        first.start()
        try:
            self.assertTrue(ready.wait(3))
            with self.store.open() as state, state.db:
                state.db.execute("SET LOCAL lock_timeout='500ms'")
                self.worker.guard(state.db, self.jobs[1])
            writer = threading.Thread(target=policy_writer)
            writer.start()
            self.assertFalse(writer_entered.wait(.2))
        finally:
            release.set()
            first.join(5)
            if 'writer' in locals():
                writer.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(writer_entered.is_set())


if __name__ == '__main__':
    unittest.main()
