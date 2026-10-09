"""Server-owned source ingestion state. Never opens a client corpus."""
import hashlib
import json
import time
from agentclient.cleaning import terms
from agentclient.local_state import private_dir

class State:
    def __init__(self, *args, **kwargs):
        raise RuntimeError('Use State.from_database with the authoritative PostgreSQL connection')

    @classmethod
    def from_database(cls, home, database):
        """Reuse canonical algorithms with a server-owned, migrated connection.

        This constructor performs no local database initialization or fallback.
        PostgreSQL migrations and credentials belong to AgentHub.
        """
        state = cls.__new__(cls)
        state.home = private_dir(home)
        state.db = database
        database.require_schema()
        return state

    def close(self):
        self.db.close()

    def enqueue(self, event):
        serialized = json.dumps(event, sort_keys=True)
        identity = [event.get(k, "") for k in ("session", "turn", "kind", "tool_use_id")]
        if not event.get("tool_use_id"):
            identity.append(serialized)
        ident = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if event.get('capture_id'):
            ident=hashlib.sha256(json.dumps([event['session'],event['capture_id']]).encode()).hexdigest()
        with self.db:
            inserted=self.db.execute("INSERT INTO events(id,session,turn,kind,project,payload,created) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (ident, event["session"], event.get("turn", ""), event["kind"], event["project"], serialized, time.time())).rowcount
            if inserted:
                self.db.execute("""INSERT INTO source_event_metadata
                    (source_id,tool_name,source_role,capture_id,event_fields,response_shape,created)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET tool_name=excluded.tool_name,source_role=excluded.source_role,capture_id=excluded.capture_id,event_fields=excluded.event_fields,response_shape=excluded.response_shape,created=excluded.created""",(ident,event.get('tool_name',''),event.get('source_role','episode_evidence'),
                    event.get('capture_id'),json.dumps(event.get('event_fields',[])),
                    json.dumps(event.get('response_shape')),time.time()))
                for key,value in {'captured_events':1,'captured_clean_bytes':len(event.get('body','').encode()),'quarantined_events':int(bool(event.get('quarantined')))}.items():
                    self.db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=counters.value+excluded.value',(key,value))
                self.db.execute("INSERT INTO health VALUES(?,?) ON CONFLICT DO NOTHING",('counters_since',str(time.time())))
        return ident

    def process(self, config=None):
        with self.db:
            for row in self.db.execute("SELECT * FROM events WHERE processed=0 ORDER BY created LIMIT 100").fetchall():
                e = json.loads(row["payload"])
                body = e.get("body", "")
                # Keep source records rather than pretending deterministic text extraction is reasoning.
                if len(body.strip())>=20 and len(terms(body))>=2 and not e.get("quarantined"):
                    inserted = self.db.execute("INSERT INTO memories(id,session,project,body,kind,created,command_key,exit_code,turn,environment_key) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (row["id"], e["session"], e["project"], body, e["kind"], row["created"], e.get("command_key"), e.get("exit_code"),e.get('turn',''),e.get('environment_key'))).rowcount
                self.db.execute("UPDATE events SET processed=1 WHERE id=?", (row["id"],))
                self.db.execute("INSERT INTO health VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('processed:'+e['project'],str(time.time())))
            # Only sanitized event payloads are queued; expire processed temporary copies.
            self.db.execute("DELETE FROM events WHERE processed=1 AND created<?", (time.time()-86400,))
            self.db.execute("DELETE FROM offers WHERE created<?", (time.time()-30*86400,))
            self.db.execute("DELETE FROM remote_offers WHERE created<?", (time.time()-30*86400,))
            self.db.execute("DELETE FROM memory_access WHERE created<?", (time.time()-30*86400,))
