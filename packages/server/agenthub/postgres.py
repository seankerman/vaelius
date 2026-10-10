"""PostgreSQL persistence for the existing canonical enterprise algorithms.

Only DB-API parameter binding is adapted. SQL dialect operations are selected
explicitly by the shared storage helpers; schema migrations are static SQL.
No SQLite corpus is opened, staged or silently used when PostgreSQL fails.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from agenthub.processing.state import State
from agenthub.enterprise import EnterpriseStore, Denied, _digest
from agenthub.cloud_identity import CloudIdentityMixin, IdentityBindings
from agenthub.cloud_preferences import CloudPreferencesMixin
from agenthub.cloud_retrieval import HybridRetrievalMixin


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


def _bind(sql):
    """Compile the shared qmark bind notation, preserving quoted literals.

    This does not rewrite SQL operations, DDL, conflict clauses or functions.
    Native psycopg %s notation remains supported for PostgreSQL repositories.
    """
    if "%s" in sql:
        return sql
    output = []; quote = None; position = 0
    while position < len(sql):
        char = sql[position]
        if quote:
            output.append(char)
            if char == quote:
                if position + 1 < len(sql) and sql[position + 1] == quote:
                    output.append(quote); position += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char; output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append("%%" if char == "%" else char)
        position += 1
    return "".join(output)


class PostgresConnection:
    dialect = "postgres"

    def __init__(self, connection):
        self.connection = connection
        self._transactions = []
        self._schema_checked = False

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.connection.info.transaction_status != TransactionStatus.IDLE

    def execute(self, sql, params=None):
        if re.search(r"\b(PRAGMA|sqlite_master|INSERT\s+OR\s+(?:IGNORE|REPLACE)|BEGIN\s+IMMEDIATE|json_extract|json_each|bm25)\b", sql, re.I):
            raise ValueError("postgres_requires_explicit_storage_operation")
        return self.connection.execute(_bind(sql) if params is not None else sql, params)

    def executemany(self, sql, params):
        cursor = self.connection.cursor()
        cursor.executemany(_bind(sql), params)
        return cursor

    def copy_rows(self,table,columns,rows):
        """Bulk ingest through INSERT so RLS is enforced, without bypass roles.

        COPY FROM does not support RLS tables. A transaction-local staging table
        keeps the fast wire protocol and a single statement's policy/trigger
        semantics. It is dropped immediately or by rollback/commit.
        """
        import csv
        import uuid
        from psycopg import sql
        names=sql.SQL(',').join(sql.Identifier(name) for name in next(csv.reader([columns],skipinitialspace=True)))
        target=sql.Identifier(table);stage=sql.Identifier('ingest_'+uuid.uuid4().hex)
        self.connection.execute(sql.SQL('CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WITH NO DATA').format(stage,names,target))
        with self.connection.cursor().copy(sql.SQL('COPY {} ({}) FROM STDIN').format(stage,names)) as copied:
            for row in rows:copied.write_row(row)
        self.connection.execute(sql.SQL('INSERT INTO {} ({}) SELECT {} FROM {}').format(target,names,names,stage))
        self.connection.execute(sql.SQL('DROP TABLE {}').format(stage))

    def executescript(self, script):
        # Only explicit PostgreSQL schema callers use this; canonical SQLite
        # initializers check the dialect and require our migrations instead.
        return self.execute(script)

    def require_schema(self):
        if self._schema_checked:
            return
        applied = {row["version"]: row for row in self.execute("SELECT version,name,sha256 FROM cloud_schema")}
        required = [path for path in sorted((Path(__file__).parent / "migrations").glob("*.sql"))
            if "_control" not in path.name]
        if not required:
            raise RuntimeError("postgres_schema_unavailable")
        for path in required:
            version = int(path.name.split("_", 1)[0])
            row = applied.get(version)
            if not row or row["name"] != path.name or row["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
                raise RuntimeError("postgres_schema_version_or_checksum_mismatch:" + path.name)
        self._schema_checked = True

    def table_exists(self, name):
        from agenthub.processing.storage import table_exists
        return table_exists(self, name)

    def columns(self, table):
        from agenthub.processing.storage import columns
        return columns(self, table)

    def begin_write(self):
        from agenthub.processing.storage import begin_write
        begin_write(self)

    def transaction(self):
        return self.connection.transaction()

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self._transactions.append(transaction)
        return self

    def __exit__(self, *exception):
        result = self._transactions.pop().__exit__(*exception)
        # transaction() inside an implicit read transaction is a savepoint.
        # The canonical `with db` boundary owns the complete operation.
        if not self._transactions:
            if exception[0] is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        return result

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def connect(dsn, *, autocommit=False):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    inherited_options = conninfo_to_dict(dsn).get("options", "")
    return psycopg.connect(dsn, row_factory=row_factory, autocommit=autocommit,
        connect_timeout=5, options=inherited_options + " -c statement_timeout=15000 -c lock_timeout=5000")


def migrate(dsn, *, control=False, statement_timeout_ms=15000):
    """Apply checksum-pinned, ordered static migrations using admin credentials.

    Serving constructors never migrate. A failed migration rolls back its schema
    and receipt together; an edited applied migration fails closed.
    """
    if type(statement_timeout_ms) is not int or not 1000 <= statement_timeout_ms <= 600000:
        raise ValueError('migration_statement_timeout_bound')
    selected = [path for path in sorted((Path(__file__).parent / "migrations").glob("*.sql"))
        if ("_control" in path.name) == control]
    receipts = []
    with connect(dsn) as connection:
        connection.execute("SELECT set_config('statement_timeout',%s,true)", (str(statement_timeout_ms),))
        connection.execute("SELECT pg_advisory_xact_lock(184730112)")
        connection.execute("CREATE TABLE IF NOT EXISTS cloud_schema(version INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL,applied DOUBLE PRECISION NOT NULL)")
        for path in selected:
            version = int(path.name.split("_", 1)[0]); content = path.read_bytes(); digest = hashlib.sha256(content).hexdigest()
            prior = connection.execute("SELECT sha256 FROM cloud_schema WHERE version=%s", (version,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise RuntimeError("applied_migration_checksum_changed:" + path.name)
                receipts.append({"version": version, "status": "already_applied", "sha256": digest})
                continue
            connection.execute(content.decode())
            connection.execute("INSERT INTO cloud_schema VALUES(%s,%s,%s,%s)", (version, path.name, digest, time.time()))
            receipts.append({"version": version, "status": "applied", "sha256": digest})
    return receipts


class PostgresEnterpriseStore(HybridRetrievalMixin, CloudIdentityMixin, CloudPreferencesMixin, EnterpriseStore):
    def __init__(self, home, dsn, tenant_id, *, registry=None):
        from agenthub.pipeline_pin import verify
        verify()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tenant_id):
            raise ValueError("invalid_tenant")
        self.home = Path(home).expanduser().resolve(); self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.dsn = dsn; self.tenant_id = tenant_id; self.registry = registry
        self.identity_bindings = IdentityBindings(registry.open_control) if registry else None
        self.database = None
        self.max_connections = 4
        self._connections = threading.BoundedSemaphore(self.max_connections)
        self._delivery_mutex = threading.RLock()
        self._delivery_state = threading.local()
        with self.open() as state:
            state.db.require_schema()

    @contextmanager
    def open(self):
        from agenthub.draft_review import borrowed_state
        borrowed=borrowed_state(self)
        if borrowed is not None:
            yield borrowed
            return
        if not self._connections.acquire(timeout=5):
            raise TimeoutError("tenant_connection_limit")
        connection = None
        try:
            connection = connect(self.dsn)
            database = PostgresConnection(connection)
            yield State.from_database(self.home, database)
        finally:
            if connection:
                connection.close()
            self._connections.release()

    @contextmanager
    def search_reader(self,ctx,project=None):
        from agenthub.search_reader import restrict
        with self.open() as state, restrict(self,state,ctx,project):
            yield state

    @contextmanager
    def delivery_read_lock(self):
        """Concurrent readers; policy commits retain their exclusive boundary.

        This does not replace PostgreSQL MVCC or row locks. It coordinates the
        final service delivery with policy mutations across API processes.
        """
        if getattr(self._delivery_state,'depth',0):
            yield
            return
        key=int.from_bytes(hashlib.sha256(self.tenant_id.encode()).digest()[:8], 'big', signed=True)
        connection=connect(self.dsn,autocommit=True)
        try:
            connection.execute('SELECT pg_advisory_lock_shared(%s)',(key,))
            yield
        finally:
            connection.execute('SELECT pg_advisory_unlock_shared(%s)',(key,))
            connection.close()

    @contextmanager
    def delivery_lock(self):
        # Cross-process lock guards policy commits and the delivery checkpoint;
        # no filesystem lock and no lock is held across model execution.
        key = int.from_bytes(hashlib.sha256(self.tenant_id.encode()).digest()[:8], "big", signed=True)
        with self._delivery_mutex:
            if getattr(self._delivery_state, "depth", 0):
                self._delivery_state.depth += 1
                try:
                    yield
                finally:
                    self._delivery_state.depth -= 1
                return
            connection = connect(self.dsn, autocommit=True)
            try:
                connection.execute("SELECT pg_advisory_lock(%s)", (key,))
                self._delivery_state.depth = 1
                yield
            finally:
                self._delivery_state.depth = 0
                connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
                connection.close()

    def create_organization(self, tenant):
        if tenant != self.tenant_id:
            raise Denied()
        return super().create_organization(tenant)

    def authenticate(self, token, request_id=None):
        ctx = super().authenticate(token, request_id)
        if ctx["tenant"] != self.tenant_id:
            raise Denied()
        if self.registry:
            self.registry.require_active(self.tenant_id)
        return ctx

    def enroll(self, tenant, principal, enrollment, actions, *, acting_for=None, **kwargs):
        if tenant != self.tenant_id:
            raise Denied()
        token = super().enroll(tenant, principal, enrollment, actions, acting_for=acting_for, **kwargs)
        if self.registry:
            self.registry.bind_credential(token, self.tenant_id)
        return token

    def backup(self, path):
        from psycopg.conninfo import conninfo_to_dict
        destination = Path(path).expanduser().resolve()
        if destination.exists():
            raise ValueError("backup_target_exists")
        info = conninfo_to_dict(self.dsn); environment = os.environ.copy()
        for field, variable in [("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"), ("password", "PGPASSWORD"), ("dbname", "PGDATABASE")]:
            if field in info:
                environment[variable] = info[field]
        subprocess.run(["pg_dump", "--format=custom", "--file", str(destination)], env=environment,
            check=True, capture_output=True, timeout=60)
        destination.chmod(0o600)
        return {"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


class TenantRegistry:
    """Server-owned credential routes, bounded tenant-store cache, no corpus."""
    def __init__(self, control_dsn, home, *, max_stores=16, store_factory=PostgresEnterpriseStore):
        self.control_dsn = control_dsn; self.home = Path(home); self.max_stores = max_stores
        self.store_factory = store_factory; self._stores = {}; self._lock = threading.Lock()

    @contextmanager
    def open_control(self):
        with connect(self.control_dsn) as connection:
            yield PostgresConnection(connection)

    def register(self, tenant, dsn):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tenant):
            raise ValueError("invalid_tenant")
        now = time.time()
        with connect(self.control_dsn) as db:
            prior = db.execute("SELECT dsn FROM cloud_tenants WHERE id=%s", (tenant,)).fetchone()
            if prior and prior[0] != dsn:
                raise ValueError("tenant_route_conflict")
            db.execute("INSERT INTO cloud_tenants VALUES(%s,%s,1,%s,%s) ON CONFLICT(id) DO NOTHING", (tenant, dsn, now, now))

    def require_active(self, tenant):
        with connect(self.control_dsn) as db:
            row = db.execute("SELECT active FROM cloud_tenants WHERE id=%s", (tenant,)).fetchone()
            if not row or not row[0]:
                raise Denied()

    def set_active(self, tenant, active):
        with connect(self.control_dsn) as db:
            db.execute("UPDATE cloud_tenants SET active=%s,updated=%s WHERE id=%s", (int(bool(active)), time.time(), tenant))

    def bind_credential(self, token, tenant):
        self.require_active(tenant)
        with connect(self.control_dsn) as db:
            digest = _digest(token)
            prior = db.execute("SELECT tenant FROM cloud_credential_routes WHERE digest=%s", (digest,)).fetchone()
            if prior and prior[0] != tenant:
                raise Denied()
            db.execute("INSERT INTO cloud_credential_routes VALUES(%s,%s,1,%s) ON CONFLICT(digest) DO NOTHING", (digest, tenant, time.time()))

    def resolve(self, tenant):
        self.require_active(tenant)
        with connect(self.control_dsn) as db:
            row = db.execute("SELECT dsn FROM cloud_tenants WHERE id=%s AND active=1", (tenant,)).fetchone()
        if not row:
            raise Denied()
        with self._lock:
            if tenant not in self._stores:
                if len(self._stores) >= self.max_stores:
                    self._stores.pop(next(iter(self._stores)))
                self._stores[tenant] = self.store_factory(self.home / tenant, row[0], tenant, registry=self)
            return self._stores[tenant]

    def store_for_token(self, token):
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise Denied()
        with connect(self.control_dsn) as db:
            row = db.execute("SELECT r.tenant FROM cloud_credential_routes r JOIN cloud_tenants t ON t.id=r.tenant WHERE r.digest=%s AND r.active=1 AND t.active=1", (_digest(token),)).fetchone()
        if not row:
            raise Denied()
        return self.resolve(row[0])
