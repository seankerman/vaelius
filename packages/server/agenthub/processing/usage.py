"""Content-free installation-wide model-call accounting, including evaluations."""
import json
import os
from pathlib import Path
import sqlite3
import time



class Ledger:
    def __init__(self,config):
        root=Path(config.get('accounting_home',Path.home()/'.local/share/agentnetwork'))
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root=root;self.db=sqlite3.connect(root/'model-usage.sqlite',timeout=5)
        os.chmod(root/'model-usage.sqlite',0o600)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,created REAL,purpose TEXT,model TEXT,status TEXT,usage TEXT);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);''')
        from agenthub.processing.accounting import initialize
        initialize(self.db,'calls')
        self.cap = None  # Legacy founder/config ceilings no longer authorize calls.

    def reserve(self,purpose,model,context=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            ident=self.db.execute("INSERT INTO calls(created,purpose,model,status) VALUES(?,?,?,'started')",(time.time(),purpose,model)).lastrowid
            from agenthub.processing.accounting import record_start
            record_start(self.db,'calls',ident,context)
            return ident

    def finish(self,ident,status,usage=None):
        from agenthub.processing.accounting import record_finish
        with self.db:
            self.db.execute('UPDATE calls SET status=?,usage=? WHERE id=?',(status,json.dumps(usage or {}),ident))
            record_finish(self.db,'calls',ident)

    def summary(self):
        rows=self.db.execute('SELECT purpose,status,usage FROM calls WHERE created>?',(time.time()-86400,)).fetchall()
        totals={};purposes={}
        for r in rows:
            purposes[r['purpose']]=purposes.get(r['purpose'],0)+1
            for k,v in json.loads(r['usage'] or '{}').items():
                if isinstance(v,int):totals[k]=totals.get(k,0)+v
        return {'rolling_calls':len(rows),'limit':self.cap,'remaining':None,'purposes':purposes,'tokens':totals}

    def seed_legacy(self,source):
        """One-time deploy checkpoint; never reads source bodies or conversations."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute("SELECT 1 FROM metadata WHERE key='legacy_seeded'").fetchone():return 0
            rows=source.execute('SELECT created,purpose,model,status,usage FROM model_calls WHERE created>?',(time.time()-86400,)).fetchall()
            self.db.executemany('INSERT INTO calls(created,purpose,model,status,usage) VALUES(?,?,?,?,?)',[tuple(r) for r in rows])
            self.db.execute("INSERT INTO metadata VALUES('legacy_seeded','1')")
        return len(rows)

    def close(self):self.db.close()
