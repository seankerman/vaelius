import unittest

from vaelius_test_support.hub.test_support import assert_disposable_connection, validate_disposable_dsn


class DisposableDatabaseTests(unittest.TestCase):
    def test_requires_explicit_marker_and_named_test_database(self):
        dsn = "host=/tmp/agentnetwork_test_socket_a port=47839 dbname=agentnetwork_test"
        with self.assertRaisesRegex(ValueError, "AGENTNETWORK_TEST_DB_DISPOSABLE"):
            validate_disposable_dsn(dsn, {})
        with self.assertRaisesRegex(ValueError, "must be named"):
            validate_disposable_dsn(dsn.replace("dbname=agentnetwork_test", "dbname=agentnetwork"),
                                    {"AGENTNETWORK_TEST_DB_DISPOSABLE": "1"})

    def test_rejects_live_socket_and_remote_host(self):
        marker = {"AGENTNETWORK_TEST_DB_DISPOSABLE": "1"}
        with self.assertRaisesRegex(ValueError, "dedicated"):
            validate_disposable_dsn(
                "host=/Users/example/agentnetwork/postgres/socket port=47832 dbname=agentnetwork_test", marker)
        with self.assertRaisesRegex(ValueError, "isolated Unix socket"):
            validate_disposable_dsn("host=localhost port=47839 dbname=agentnetwork_test", marker)

    def test_accepts_only_the_disposable_local_socket_shape(self):
        options = validate_disposable_dsn(
            "host=/tmp/agentnetwork_test_socket_abc123 port=47839 dbname=agentnetwork_test",
            {"AGENTNETWORK_TEST_DB_DISPOSABLE": "1"})
        self.assertEqual(options["dbname"], "agentnetwork_test")

    def test_checks_the_connected_database_and_transport(self):
        class Connection:
            def __init__(self, row): self.row = row
            def execute(self, query): return self
            def fetchone(self): return self.row

        assert_disposable_connection(Connection(("agentnetwork_test", True)))
        with self.assertRaisesRegex(ValueError, "isolated local test database"):
            assert_disposable_connection(Connection(("agentnetwork", True)))
        with self.assertRaisesRegex(ValueError, "isolated local test database"):
            assert_disposable_connection(Connection(("agentnetwork_test", False)))


if __name__ == "__main__":
    unittest.main()
