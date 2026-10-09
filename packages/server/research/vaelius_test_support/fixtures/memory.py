"""Immutable synthetic inputs shared by canonical validator tests."""
import hashlib
import json
from pathlib import Path

def _fixture(path):
    path = Path(path)
    raw = path.read_bytes()
    manifest = path.with_name(path.stem + "_manifest.json")
    metadata = json.loads(manifest.read_text())
    if hashlib.sha256(raw).hexdigest() != metadata.get("corpus_sha256"):
        raise ValueError("continuous_fixture_hash_mismatch")
    corpus = json.loads(raw)
    if corpus.get("schema_version") != 1 or not isinstance(corpus.get("cases"), list):
        raise ValueError("continuous_fixture_shape")
    return corpus["cases"], metadata

def _combined(packets):
    return {**packets[-1], "episode": {**packets[-1]["episode"],
        "events": [e for p in packets for e in p["episode"]["events"]]}}

def seed_validated_records(state, result, packet):
    """Synthetic records enter the real revision compiler; no observer implementation."""
    from agenthub.processing.durable_memory import observation_for
    from agenthub.processing.knowledge import apply_resolved_observation
    documents=[]
    with state.db:
        for i,record in enumerate(result['records']):
            observation=observation_for(record,packet)
            ident='synthetic-curated-'+hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()
            project=packet['episode']['project'];session=packet['episode']['session']
            state.db.execute('INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)',
                (ident,session,project,json.dumps(observation),'KnowledgeCandidate',i))
            state.db.executemany('INSERT INTO observation_sources VALUES(?,?)',
                [(ident,ref['source_id']) for ref in observation['evidence']])
            documents.append(apply_resolved_observation(state.db,ident,project,session,observation,'CREATE'))
    return documents
