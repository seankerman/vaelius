"""Fail-closed checks for integration tests that truncate PostgreSQL data."""
import os
from pathlib import Path
import shlex



DATABASE_NAME = "agentnetwork_test"
DISPOSABLE_FLAG = "AGENTNETWORK_TEST_DB_DISPOSABLE"
SOCKET_PREFIX = "agentnetwork_test_socket_"


def validate_disposable_dsn(dsn, environ=None):
    env = os.environ if environ is None else environ
    if env.get(DISPOSABLE_FLAG) != "1":
        raise ValueError(f"Set {DISPOSABLE_FLAG}=1 only for a disposable test cluster")
    try:
        pairs=[part.split('=',1) for part in shlex.split(dsn)]
        if any(len(pair)!=2 or pair[0] not in {'host','port','dbname'} for pair in pairs):
            raise ValueError()
        options=dict(pairs)
        if len(options)!=len(pairs):raise ValueError()
    except (TypeError,ValueError) as exc:
        raise ValueError("Invalid test database DSN") from exc
    if options.get("dbname") != DATABASE_NAME:
        raise ValueError(f"Database must be named {DATABASE_NAME}")
    host = options.get("host", "")
    if not host or "," in host or not Path(host).is_absolute():
        raise ValueError("Tests require one isolated Unix socket under /tmp")
    socket = Path(host).resolve()
    if socket.parent != Path("/tmp").resolve() or not socket.name.startswith(SOCKET_PREFIX):
        raise ValueError(f"Test socket must be a dedicated {SOCKET_PREFIX}* directory under /tmp")
    return options


def assert_disposable_connection(connection):
    database, unix_socket = connection.execute(
        "SELECT current_database(), inet_server_addr() IS NULL"
    ).fetchone()
    if database != DATABASE_NAME or not unix_socket:
        raise ValueError("Connected PostgreSQL server is not the isolated local test database")


def check_disposable_database(dsn, environ=None):
    """Validate configuration and verify the actual server before destructive SQL."""
    validate_disposable_dsn(dsn, environ)
    import psycopg
    with psycopg.connect(dsn, connect_timeout=3) as connection:
        assert_disposable_connection(connection)
