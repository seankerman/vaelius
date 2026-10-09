"""Owner-only redacted transport spool, bounded and never searched for knowledge."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from agentclient.enterprise_capture import redact
from agentclient.general_contract import canonical, digest, split_event, validate_event


class Outbox:
    def __init__(self, home, *, max_bytes=64 * 1024 * 1024, max_age=86400):
        self.home = Path(home);self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes;self.max_age = max_age
        path = self.home / 'capture-outbox.sqlite'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600);os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS pending(digest TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY,reason TEXT NOT NULL,count INTEGER NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(digest TEXT PRIMARY KEY,source_id TEXT,created REAL NOT NULL);''')
        self.db.execute('CREATE TABLE IF NOT EXISTS diagnostic_cursor(id INTEGER PRIMARY KEY,offset INTEGER NOT NULL)')
        if 'next_attempt' not in {r[1] for r in self.db.execute('PRAGMA table_info(pending)')}:
            self.db.execute('ALTER TABLE pending ADD COLUMN next_attempt REAL NOT NULL DEFAULT 0')
            self.db.execute('ALTER TABLE pending ADD COLUMN failures INTEGER NOT NULL DEFAULT 0')
            self.db.commit()

    def close(self): self.db.close()

    def queue_diagnostics(self,config):
        """Replay metadata-only hook gaps through the same durable intake path."""
        from agentclient.transport import capture_connection
        path=self.home/'enterprise-capture-gaps.jsonl'
        if not path.exists():return 0
        from agentclient.enterprise_capture import normalize_capture
        row=self.db.execute('SELECT offset FROM diagnostic_cursor WHERE id=1').fetchone();offset=row[0] if row else 0
        count=0
        with path.open('rb') as stream:
            stream.seek(offset)
            for _ in range(64):
                start=stream.tell();line=stream.readline(16385)
                if not line or not line.endswith(b'\n') or len(line)>16384:break
                record=json.loads(line)
                # Retrieval/receipt outages do not imply missing source content.
                # Keep them locally for diagnostics, while advancing the replay cursor.
                structured=config.get('knowledge_backend',{}).get('capture_version')=='enterprise-local-2'
                if structured and record['reason'] not in {'structured_capture_backpressure_or_invalid','excluded_or_truncated'}:
                    with self.db:self.db.execute('INSERT OR REPLACE INTO diagnostic_cursor VALUES(1,?)',(stream.tell(),))
                    continue
                project=record.get('project')
                if not project:
                    projects=set(config.get('projects',{}).values())|set(config.get('sessions',{}).values())
                    if len(projects)!=1:raise ValueError('legacy_gap_project_mapping_unknown')
                    project=next(iter(projects))
                value=normalize_capture({'hook_event_name':'gap','event_id':'hook-gap-'+hashlib.sha256(canonical([record,start])).hexdigest(),
                    'session_id':record.get('session') or 'unknown:'+record['session_hash'],
                    'turn_id':record.get('turn',''),'timestamp':record['at']},project,capture_connection(config,project))
                value['blocks']=[{'type':'record_fields','value':{'reason':record['reason'],'original_kind':record['kind']}}]
                self.put(value)
                with self.db:self.db.execute('INSERT OR REPLACE INTO diagnostic_cursor VALUES(1,?)',(stream.tell(),))
                count+=1
        return count

    def _expire(self):
        old = self.db.execute('SELECT digest,payload FROM pending WHERE created<?', (time.time()-self.max_age,)).fetchall()
        for row in old:
            event = json.loads(row['payload'])
            gap = dict(event, blocks=[], redactions=[], disposition='incomplete')
            gap['event'] = dict(event['event'], kind='gap', complete=False)
            gap['external_id'] = 'expired-' + row['digest']
            self.db.execute('DELETE FROM pending WHERE digest=?', (row['digest'],))
            self.db.execute('INSERT OR IGNORE INTO pending(digest,payload,created) VALUES(?,?,?)', (digest(gap), canonical(gap).decode(), time.time()))
            self.db.execute('INSERT INTO gaps(reason,count,created) VALUES(?,?,?)', ('expired_payload_replaced_by_gap', 1, time.time()))

    def put(self, value):
        validate_event(value)
        safe, _ = redact(value)
        if safe != value: raise ValueError('unredacted_outbox_input')
        raw = canonical(value);ident = digest(value)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE');self._expire()
            if self.db.execute('SELECT 1 FROM receipts WHERE digest=?', (ident,)).fetchone(): return ident
            if self.db.execute('SELECT 1 FROM pending WHERE digest=?', (ident,)).fetchone(): return ident
            used = self.db.execute('SELECT coalesce(sum(length(cast(payload as blob))),0) FROM pending').fetchone()[0]
            if used + len(raw) > self.max_bytes:
                self.db.execute('INSERT INTO gaps(reason,count,created) VALUES(?,?,?)', ('outbox_full_backpressure', 1, time.time()))
                # Commit diagnostic, never acknowledge a payload that did not fit.
                self.db.commit()
                raise ValueError('capture_outbox_full')
            self.db.execute('INSERT INTO pending(digest,payload,created) VALUES(?,?,?)', (ident, raw.decode(), time.time()))
        return ident

    def drain(self, backend, *, max_events=32, max_seconds=5):
        if not 1 <= max_events <= 1000 or not 0 < max_seconds <= 300: raise ValueError('invalid_drain_bound')
        start=time.monotonic();accepted=0;blocked=False
        with self.db: self._expire()
        rows=self.db.execute('SELECT * FROM pending WHERE next_attempt<=? ORDER BY created LIMIT ?', (time.time(),max_events)).fetchall()
        for row in rows:
            if time.monotonic()-start >= max_seconds: break
            try:
                parts=split_event(json.loads(row['payload']));receipt=None
                for part in parts:
                    if time.monotonic()-start >= max_seconds: raise TimeoutError()
                    receipt=backend.request('/enterprise/v2/parts', part)
                    if receipt['digest'] != row['digest']: raise ValueError('outbox_receipt_mismatch')
                if not receipt or not receipt['complete']: raise ValueError('outbox_not_durable')
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)', (row['digest'], receipt['source_id'], time.time()))
                    self.db.execute('DELETE FROM pending WHERE digest=?', (row['digest'],))
                    self.db.execute('DELETE FROM receipts WHERE created<?', (time.time()-self.max_age,))
                accepted+=1
            except Exception:
                with self.db:self.db.execute('UPDATE pending SET failures=failures+1,next_attempt=? WHERE digest=?',
                    (time.time()+min(300,2**min(row['failures'],8)),row['digest']))
                blocked=True;break
        return {'acknowledged_events':accepted, 'blocked':blocked, **self.status()}

    def status(self):
        row=self.db.execute('SELECT count(*),coalesce(sum(length(cast(payload as blob))),0),min(created) FROM pending').fetchone()
        return {'pending_events':row[0], 'pending_bytes':row[1], 'oldest_age_seconds': max(0,time.time()-row[2]) if row[2] else 0,
            'max_bytes':self.max_bytes,'max_age_seconds':self.max_age,
            'gaps':{r[0]:r[1] for r in self.db.execute('SELECT reason,sum(count) FROM gaps GROUP BY reason')},
            'searchable':False, 'local_corpus_opened':False}
