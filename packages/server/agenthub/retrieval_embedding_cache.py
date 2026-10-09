"""Private derivative cache and one finite local embedding campaign budget.

This SQLite file contains hashes, vectors and execution counters, never source
documents or searchable knowledge. PostgreSQL remains the corpus authority.
Query vectors are deliberately not cached: measured requests run the real model.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import struct
import time

from agenthub.processing.semantic import DIMENSION, normalize_vector, prepare_document


def resolve_shared_accounting_root(value, *, model_key=None, dimension=DIMENSION):
    """Resolve an explicit existing private campaign; never create a fresh budget."""
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError('embedding_shared_accounting_root_invalid')
    directory = path / 'embedding-derivative'
    cache = directory / 'cache.sqlite'
    if (directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077
            or cache.is_symlink() or not cache.is_file() or cache.stat().st_mode & 0o077):
        raise ValueError('embedding_shared_accounting_cache_required')
    with sqlite3.connect(cache.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT max_seconds,used_seconds FROM campaign WHERE singleton=1').fetchone()
        if row is None or not math.isfinite(row[0]) or row[0] <= 0 or not math.isfinite(row[1]) or row[1] < 0:
            raise ValueError('embedding_shared_accounting_campaign_invalid')
        if model_key is not None and not db.execute(
                'SELECT 1 FROM embeddings WHERE model_key=? AND dimension=? LIMIT 1',
                (model_key, dimension)).fetchone():
            raise ValueError('embedding_shared_accounting_model_mismatch')
    return path.resolve()


class EmbeddingBudgetExceeded(RuntimeError):
    def __init__(self):
        super().__init__('retrieval_embedding_campaign_budget_exhausted')


class CampaignEmbedder:
    """Wrap a local model; verified document hits do not spend model work.

    An explicitly requested diagnostic ceiling is persisted. Normal operation
    is accounting-only; removing a limit preserves every execution counter.
    Later wrappers can lower it but cannot reset/increase it. A synchronous batch
    admitted before exhaustion may finish beyond the ceiling; the full time is
    charged and the next model batch is rejected. No provider dispatch is added.
    """
    def __init__(self, model, campaign_root, max_embedding_seconds=None, *, accounting_root=None):
        try:
            requested = None if max_embedding_seconds is None else float(max_embedding_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError('invalid_embedding_campaign_budget') from error
        if requested is not None and (not math.isfinite(requested) or requested <= 0):
            raise ValueError('invalid_embedding_campaign_budget')
        self.model = model
        self._validate_model()
        self.accounting_root = (resolve_shared_accounting_root(accounting_root, model_key=self.model_key, dimension=self.dimension)
                                if accounting_root is not None else Path(campaign_root).expanduser().resolve())
        self.directory = self.accounting_root / 'embedding-derivative'
        if self.directory.is_symlink():
            raise ValueError('embedding_cache_symlink')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_mode & 0o077:
            raise ValueError('embedding_cache_not_private')
        self.cache_path = self.directory / 'cache.sqlite'
        self.lock_path = self.directory / 'campaign.lock'
        self._private_file(self.cache_path)
        self._private_file(self.lock_path)
        with self._locked() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS campaign(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    max_seconds REAL NOT NULL,used_seconds REAL NOT NULL DEFAULT 0,
                    model_calls INTEGER NOT NULL DEFAULT 0,query_calls INTEGER NOT NULL DEFAULT 0,
                    document_calls INTEGER NOT NULL DEFAULT 0,failed_calls INTEGER NOT NULL DEFAULT 0,
                    uncertain_calls INTEGER NOT NULL DEFAULT 0,cache_hits INTEGER NOT NULL DEFAULT 0,
                    cache_misses INTEGER NOT NULL DEFAULT 0,invalid_entries INTEGER NOT NULL DEFAULT 0,
                    active_started_at REAL,active_kind TEXT);
                CREATE TABLE IF NOT EXISTS embeddings(
                    model_key TEXT NOT NULL,dimension INTEGER NOT NULL,
                    preprocessing TEXT NOT NULL,text_sha256 TEXT NOT NULL,
                    vector BLOB NOT NULL,checksum TEXT NOT NULL,
                    PRIMARY KEY(model_key,dimension,preprocessing,text_sha256));
            ''')
            if 'limit_enabled' not in {row[1] for row in db.execute('PRAGMA table_info(campaign)')}:
                db.execute('ALTER TABLE campaign ADD COLUMN limit_enabled INTEGER NOT NULL DEFAULT 1')
            ceiling = 3600.0 if requested is None else requested
            db.execute('INSERT OR IGNORE INTO campaign(singleton,max_seconds) VALUES(1,?)', (ceiling,))
            db.execute('UPDATE campaign SET max_seconds=min(max_seconds,?) WHERE singleton=1', (ceiling,))
            if requested is None:
                db.execute('UPDATE campaign SET limit_enabled=0 WHERE singleton=1')
            self._recover(db)
            db.commit()

    def remove_limit(self):
        """Owner-authorized accounting-only mode; preserve cache and all usage."""
        with self._locked() as db:
            self._recover(db)
            db.execute('UPDATE campaign SET limit_enabled=0 WHERE singleton=1')
            db.commit()

    @staticmethod
    def _private_file(path):
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.fstat(descriptor).st_mode & 0o077:
                raise ValueError('embedding_cache_file_not_private')
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self):
        descriptor = os.open(self.lock_path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        database = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            database = sqlite3.connect(self.cache_path, timeout=10)
            database.row_factory = sqlite3.Row
            yield database
        finally:
            if database is not None:
                database.close()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @property
    def model_key(self):
        return self.model.model_key

    @property
    def dimension(self):
        return self.model.dimension

    def _validate_model(self):
        if (not isinstance(self.model_key, str) or not self.model_key or
                self.dimension != DIMENSION):
            raise ValueError('embedding_cache_model_identity_or_dimension')

    @staticmethod
    def _recover(db):
        row = db.execute('SELECT active_started_at,used_seconds,max_seconds FROM campaign WHERE singleton=1').fetchone()
        if row and row['active_started_at'] is not None:
            # The process lock proves no live wrapper still owns this reservation.
            # A crash cannot yield a fresh budget; elapsed wall time is charged
            # conservatively because the previous monotonic interval is lost.
            elapsed = time.time() - row['active_started_at']
            if not math.isfinite(elapsed) or elapsed < 0:
                # A wall-clock reversal leaves the previous duration unknown;
                # it cannot create unaccounted execution capacity.
                elapsed = max(0.0, row['max_seconds'] - row['used_seconds'])
            db.execute('''UPDATE campaign SET used_seconds=used_seconds+?,
                uncertain_calls=uncertain_calls+1,active_started_at=NULL,active_kind=NULL
                WHERE singleton=1''', (elapsed,))
            db.commit()

    @staticmethod
    def _key(model_key, text):
        prepared = prepare_document(text)
        prefix = prepare_document('prefix-sentinel').removesuffix('prefix-sentinel')
        preprocessing = 'canonical-document-prefix-v1:' + hashlib.sha256(prefix.encode()).hexdigest()
        return (model_key, DIMENSION, preprocessing, hashlib.sha256(prepared.encode()).hexdigest())

    @staticmethod
    def _packed(values):
        normalized = normalize_vector(values, DIMENSION)
        packed = struct.pack('<512f', *normalized)
        return packed, hashlib.sha256(packed).hexdigest()

    @staticmethod
    def _verified(packed, checksum):
        if not isinstance(packed, bytes) or len(packed) != DIMENSION * 4:
            raise ValueError('embedding_cache_vector_shape')
        if hashlib.sha256(packed).hexdigest() != checksum:
            raise ValueError('embedding_cache_vector_checksum')
        values = list(struct.unpack('<512f', packed))
        magnitude = sum(value * value for value in values)
        if not math.isfinite(magnitude) or abs(magnitude - 1) > 1e-5:
            raise ValueError('embedding_cache_vector_not_normalized')
        return values

    def _invoke(self, db, kind, texts):
        self._recover(db)
        row = db.execute('SELECT used_seconds,max_seconds,limit_enabled FROM campaign WHERE singleton=1').fetchone()
        if row['limit_enabled'] and row['used_seconds'] >= row['max_seconds']:
            raise EmbeddingBudgetExceeded()
        db.execute('''UPDATE campaign SET active_started_at=?,active_kind=?,
            model_calls=model_calls+1,query_calls=query_calls+?,document_calls=document_calls+?
            WHERE singleton=1''', (time.time(), kind, int(kind == 'query'), int(kind == 'document')))
        db.commit()  # Persist admission before beginning any actual model work.
        started = time.monotonic()
        failed = True
        try:
            method = self.model.embed_queries if kind == 'query' else self.model.embed_documents
            values = method(texts)
            if len(values) != len(texts):
                raise ValueError('embedding_cache_batch_shape')
            result = [normalize_vector(vector, DIMENSION) for vector in values]
            failed = False
            return result
        finally:
            elapsed = max(0.0, time.monotonic() - started)
            db.execute('''UPDATE campaign SET used_seconds=used_seconds+?,
                failed_calls=failed_calls+?,active_started_at=NULL,active_kind=NULL
                WHERE singleton=1''', (elapsed, int(failed)))
            db.commit()

    def embed_documents(self, texts):
        self._validate_model()
        keys = [self._key(self.model_key, text) for text in texts]
        if not keys:
            return []
        with self._locked() as db:
            self._recover(db)
            values = {}
            missing = {}
            hits = 0
            invalid = 0
            for key, text in zip(keys, texts):
                if key in values or key in missing:
                    continue
                row = db.execute('''SELECT vector,checksum FROM embeddings
                    WHERE model_key=? AND dimension=? AND preprocessing=? AND text_sha256=?''', key).fetchone()
                if row:
                    try:
                        values[key] = self._verified(row['vector'], row['checksum'])
                        hits += 1
                        continue
                    except ValueError:
                        db.execute('''DELETE FROM embeddings WHERE model_key=? AND dimension=?
                            AND preprocessing=? AND text_sha256=?''', key)
                        invalid += 1
                missing[key] = text
            db.execute('''UPDATE campaign SET cache_hits=cache_hits+?,cache_misses=cache_misses+?,
                invalid_entries=invalid_entries+? WHERE singleton=1''', (hits, len(missing), invalid))
            db.commit()
            if missing:
                vectors = self._invoke(db, 'document', list(missing.values()))
                for key, vector in zip(missing, vectors):
                    packed, checksum = self._packed(vector)
                    values[key] = self._verified(packed, checksum)
                    db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?)', (*key, packed, checksum))
                db.commit()
            return [values[key] for key in keys]

    def embed_queries(self, texts):
        self._validate_model()
        if not texts:
            return []
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError('semantic_text_required')
        with self._locked() as db:
            return self._invoke(db, 'query', texts)

    def stats(self):
        with self._locked() as db:
            self._recover(db)
            row = db.execute('SELECT * FROM campaign WHERE singleton=1').fetchone()
            entries = db.execute('SELECT count(*) FROM embeddings').fetchone()[0]
            return {
                'derivative_cache': True, 'provider_calls': 0, 'cache_entries': entries,
                'accounting_root': str(self.accounting_root),
                'max_embedding_seconds': row['max_seconds'] if row['limit_enabled'] else None, 'legacy_max_embedding_seconds': row['max_seconds'], 'embedding_seconds': row['used_seconds'],
                'remaining_seconds': max(0.0, row['max_seconds'] - row['used_seconds']) if row['limit_enabled'] else None,
                'overrun_seconds': max(0.0, row['used_seconds'] - row['max_seconds']),
                'model_calls': row['model_calls'], 'query_calls': row['query_calls'],
                'document_calls': row['document_calls'], 'failed_calls': row['failed_calls'],
                'uncertain_calls': row['uncertain_calls'], 'cache_hits': row['cache_hits'],
                'cache_misses': row['cache_misses'], 'invalid_cache_entries': row['invalid_entries'],
            }
