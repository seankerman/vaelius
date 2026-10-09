"""Private, revisioned knowledge documents backed by stable source memories."""
from __future__ import annotations

from agenthub.processing.storage import table_exists, begin_write, create_fulltext

import hashlib
import json
import re
import time


SCHEMA_VERSION=1
INDEX_SCHEMA_VERSION=2
SEMANTIC_INDEX_SCHEMA_VERSION=1
ALGORITHM_VERSION="exact-identity-1"


def initialize(db):
    db.require_schema()


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity(claim):
    fields={key:claim.get(key) for key in (
        "title","problem","lesson","applicability","applicability_constraints",
        "knowledge_type","domain","subjects","tags","aliases","outcome",
        "action","outcome_text","evidence_status")}
    if "memory_context" in claim:
        fields["memory_context"]=claim["memory_context"]
    if "source_context" in claim:
        fields["source_context"]=claim["source_context"]
    # Exact identity intentionally preserves case, punctuation, digits, negation,
    # ordering, and whitespace. Similarity never authorizes a merge.
    encoded=json.dumps(fields,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return encoded,_hash(encoded)


def _claim(observation):
    claim = {key:observation.get(key) for key in (
        "title","problem","lesson","applicability","applicability_constraints",
        "knowledge_type","domain","subjects","tags","aliases","outcome",
        "action","outcome_text","evidence_status")}
    if "memory_context" in observation:
        claim["memory_context"]=observation["memory_context"]
    if "source_context" in observation:
        claim["source_context"]=observation["source_context"]
    return claim


def _index_text(claim):
    """Index claim fields, never serialized JSON property names."""
    values=[claim.get(key, "") for key in
            ("title", "problem", "lesson", "action", "outcome_text", "applicability")]
    for key in ("subjects", "tags", "aliases"):
        values.extend(claim.get(key) or [])
    context=claim.get("memory_context") or {}
    values.extend(context.get(key, "") for key in ("subject", "artifact_name", "location", "reason_quote"))
    values.extend(context.get("actors", []))
    constraints=claim.get("applicability_constraints") or {}
    for value in constraints.values() if isinstance(constraints,dict) else ():
        values.extend(value if isinstance(value,list) else [value])
    return " ".join(str(value) for value in values if value is not None and value != "")


def _candidate_keys(claim):
    """Index source-stated entity handles without conflating same-name actors."""
    context=claim.get("memory_context") or {}
    values={"subject":claim.get("subjects") or [],
            "alias":claim.get("aliases") or [],
            "artifact":([context.get("artifact_name")]
                        if isinstance(context,dict) and context.get("artifact_name") else [])}
    if isinstance(context,dict) and context.get("subject"):
        values["subject"]=[*values["subject"],context["subject"]]
    keys=set()
    for kind,items in values.items():
        for item in items:
            if not isinstance(item,str):continue
            key=" ".join(item.casefold().split())
            if key and len(key)<=200:keys.add((kind,key))
    return sorted(keys)


def _refresh_candidate_keys(db, document_id, project, revision_id, claim):
    db.execute("DELETE FROM knowledge_candidate_keys WHERE document_id=?",(document_id,))
    if revision_id is None:return
    db.executemany("""INSERT INTO knowledge_candidate_keys
        (project,key_kind,key_value,document_id,revision_id) VALUES(?,?,?,?,?)
        ON CONFLICT DO NOTHING""",
        ((project,kind,key,document_id,revision_id) for kind,key in _candidate_keys(claim)))


def backfill_candidate_keys(db, *, after_document_id="", limit=200):
    """Page existing active claims into the exact-entity discovery index.

    This makes old documents discoverable without a model call or a broad FTS
    rebuild. Call until has_more is false; each page commits independently.
    """
    if type(after_document_id) is not str or type(limit) is not int or not 1<=limit<=2000:
        raise ValueError("invalid_candidate_key_backfill_bounds")
    with db:
        begin_write(db)
        rows=db.execute("""SELECT d.document_id,d.project,d.active_revision_id,r.claim_json
            FROM knowledge_documents d JOIN knowledge_revisions r
              ON r.revision_id=d.active_revision_id
            WHERE d.lifecycle='active' AND d.document_id>?
            ORDER BY d.document_id LIMIT ?"""+
            (" FOR UPDATE OF d"),
            (after_document_id,limit)).fetchall()
        for row in rows:
            _refresh_candidate_keys(db,row["document_id"],row["project"],
                row["active_revision_id"],json.loads(row["claim_json"]))
    return {"scanned":len(rows),"next_after_document_id":(
        rows[-1]["document_id"] if rows else after_document_id),
        "has_more":len(rows)==limit,"model_calls":0}


def refresh_index(db, document_id=None):
    """Keep active and in-progress rebuild indexes in the same write transaction."""
    indexes=[("knowledge_fts","knowledge_index_rows")]
    if (table_exists(db,'knowledge_fts_staging')
            and table_exists(db,'knowledge_index_rows_staging')):
        indexes.append(("knowledge_fts_staging","knowledge_index_rows_staging"))
    if document_id is None:
        rows=db.execute("SELECT d.document_id,d.project,d.active_revision_id,r.claim_json FROM knowledge_documents d "
            "LEFT JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id WHERE d.lifecycle='active'").fetchall()
        db.execute("""DELETE FROM knowledge_candidate_keys WHERE document_id NOT IN
            (SELECT document_id FROM knowledge_documents WHERE lifecycle='active'
             AND active_revision_id IS NOT NULL)""")
        for row in rows:
            _refresh_candidate_keys(db,row["document_id"],row["project"],
                row["active_revision_id"],json.loads(row["claim_json"]) if row["claim_json"] else {})
        for index,mapping in indexes:
            db.execute(f"DELETE FROM {index} WHERE document_id NOT IN (SELECT document_id FROM knowledge_documents WHERE lifecycle='active' AND active_revision_id IS NOT NULL)")
            db.execute(f"DELETE FROM {mapping} WHERE document_id NOT IN (SELECT document_id FROM knowledge_documents WHERE lifecycle='active' AND active_revision_id IS NOT NULL)")
            for row in rows:
                claim=json.loads(row["claim_json"]) if row["claim_json"] else {}
                db.execute(f"DELETE FROM {index} WHERE document_id=?",(row["document_id"],))
                db.execute(f"DELETE FROM {mapping} WHERE document_id=?",(row["document_id"],))
                if row["active_revision_id"]:
                    db.execute(f"INSERT INTO {index}(document_id,revision_id,body) VALUES(?,?,?)",
                        (row["document_id"],row["active_revision_id"],_index_text(claim)))
                    db.execute(f"INSERT INTO {mapping}(document_id,revision_id) VALUES(?,?)",
                        (row["document_id"],row["active_revision_id"]))
        # Embeddings are derived from the active immutable revision. Purge rows
        # whose document or revision is no longer live; an explicit semantic
        # rebuild fills any missing rows. Retrieval never mutates this index.
        db.execute("""DELETE FROM knowledge_embeddings WHERE NOT EXISTS (
            SELECT 1 FROM knowledge_documents d WHERE d.document_id=knowledge_embeddings.document_id
            AND d.lifecycle='active' AND d.active_revision_id=knowledge_embeddings.revision_id)""")
        return len(rows)
    row=db.execute("SELECT d.document_id,d.project,d.active_revision_id,r.claim_json FROM knowledge_documents d "
        "LEFT JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id WHERE d.document_id=? AND d.lifecycle='active'",
        (document_id,)).fetchone()
    _refresh_candidate_keys(db,document_id,row["project"] if row else "",
        row["active_revision_id"] if row else None,
        json.loads(row["claim_json"]) if row and row["claim_json"] else {})
    for index,mapping in indexes:
        db.execute(f"DELETE FROM {index} WHERE document_id=?",(document_id,))
        db.execute(f"DELETE FROM {mapping} WHERE document_id=?",(document_id,))
        if row and row["active_revision_id"]:
            claim=json.loads(row["claim_json"])
            db.execute(f"INSERT INTO {index}(document_id,revision_id,body) VALUES(?,?,?)",
                (document_id,row["active_revision_id"],_index_text(claim)))
            db.execute(f"INSERT INTO {mapping}(document_id,revision_id) VALUES(?,?)",
                (document_id,row["active_revision_id"]))
    # Invalidate every model representation in the caller's transaction. This
    # keeps corrections and withdrawals atomic with both retrieval indexes.
    db.execute("DELETE FROM knowledge_embeddings WHERE document_id=?",(document_id,))
    db.execute("UPDATE knowledge_embedding_state SET status='stale',completed=NULL,updated=?",
               (time.time(),))
    return int(bool(row and row["active_revision_id"]))


def index_ready(db):
    state=db.execute("SELECT schema_version FROM knowledge_index_state WHERE singleton=1").fetchone()
    if not state or state[0]!=INDEX_SCHEMA_VERSION:return False
    missing=db.execute("""SELECT 1 FROM knowledge_documents d LEFT JOIN knowledge_index_rows i
        ON i.document_id=d.document_id WHERE d.lifecycle='active' AND d.active_revision_id IS NOT NULL
        AND (i.revision_id IS NULL OR i.revision_id!=d.active_revision_id) LIMIT 1""").fetchone()
    stale=db.execute("""SELECT 1 FROM knowledge_index_rows i LEFT JOIN knowledge_documents d
        ON d.document_id=i.document_id WHERE d.document_id IS NULL OR d.lifecycle!='active'
        OR d.active_revision_id!=i.revision_id LIMIT 1""").fetchone()
    missing_fts=db.execute("""SELECT 1 FROM knowledge_index_rows i
        LEFT JOIN knowledge_fts f ON f.document_id=i.document_id AND f.revision_id=i.revision_id
        WHERE f.document_id IS NULL LIMIT 1""").fetchone()
    stale_fts=db.execute("""SELECT 1 FROM knowledge_fts f
        LEFT JOIN knowledge_index_rows i ON i.document_id=f.document_id AND i.revision_id=f.revision_id
        WHERE i.document_id IS NULL LIMIT 1""").fetchone()
    duplicate_fts=db.execute("""SELECT 1 FROM knowledge_fts
        GROUP BY document_id,revision_id HAVING count(*)!=1 LIMIT 1""").fetchone()
    return not any((missing,stale,missing_fts,stale_fts,duplicate_fts))


def _identifier(prefix, value):
    return prefix+_hash(value)[:48]


def _support_rows(db, revision_id, observation, independence="unknown"):
    now=time.time()
    for ref in observation.get("evidence",[]):
        db.execute("""INSERT INTO knowledge_support
          (revision_id,source_memory_id,source_segment_id,relation,independence,created)
          VALUES(?,?,?,'supports',?,?) ON CONFLICT DO NOTHING""",
          (revision_id,ref["source_id"],ref.get("segment_id"),independence,now))


def _decision(db, action, input_revision_ids, output_document_id, reason, review_state="automatic_exact"):
    encoded=json.dumps(sorted(set(input_revision_ids)),separators=(",",":"))
    ident=_identifier("cd_",f"{action}:{encoded}:{output_document_id}:{ALGORITHM_VERSION}")
    db.execute("""INSERT INTO consolidation_decisions
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",(ident,action,encoded,output_document_id,ALGORITHM_VERSION,reason,review_state,time.time()))
    return ident


def _possible_relations(db, project, document_id, revision_id, claim, limit=40):
    """Create bounded review candidates; never merge paraphrases automatically."""
    from agentclient.cleaning import terms
    words=set(terms(" ".join(str(claim.get(k) or "") for k in ("title","problem","lesson","subjects","tags"))))
    if not words:return 0
    candidates=db.execute("""SELECT d.document_id,r.revision_id,r.claim_json FROM knowledge_documents d
        JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
        WHERE d.project=? AND d.lifecycle='active' AND d.document_id!=?
        ORDER BY d.updated DESC LIMIT ?""",(project,document_id,limit)).fetchall()
    made=0
    for row in candidates:
        prior=json.loads(row["claim_json"])
        prior_words=set(terms(" ".join(str(prior.get(k) or "") for k in ("title","problem","lesson","subjects","tags"))))
        union=words|prior_words
        score=(2*len(words&prior_words)/len(union)) if union else 0
        same_subject=bool(set(claim.get("subjects") or []) & set(prior.get("subjects") or []))
        same_scope_condition=(claim.get("applicability_constraints")==prior.get("applicability_constraints")
            and claim.get("applicability")==prior.get("applicability"))
        if same_subject and same_scope_condition and claim.get("outcome")!=prior.get("outcome"):
            relation="possible_disagreement";reason="same_subject_and_conditions_different_outcome"
            score=max(score,1.0)
        elif same_subject and not same_scope_condition:
            relation="conditional_variant";reason="same_subject_different_applicability_conditions"
            score=max(score,0.75)
        elif score>=0.45:
            relation="possible_paraphrase";reason="bounded_lexical_candidate_needs_review"
        else:continue
        rel_id=_identifier("rel_",f"{document_id}:{row['document_id']}:{relation}:{ALGORITHM_VERSION}")
        db.execute("""INSERT INTO knowledge_relations
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",(rel_id,document_id,row["document_id"],relation,score,reason,
              ALGORITHM_VERSION,"needs_review",time.time()))
        made+=1
    return made


def ingest_observation(db, memory_id, project, session, observation, now=None, *, force_new=False):
    """Create or attach exact same-project knowledge; return stable doc/revision IDs."""
    initialize(db)
    now=now or time.time()
    existing=db.execute("SELECT document_id,active_revision_id FROM knowledge_documents WHERE origin_memory_id=?",
                         (memory_id,)).fetchone()
    if existing:
        return {"action":"already_ingested","document_id":existing["document_id"],
                "revision_id":existing["active_revision_id"],"related_candidates":0}
    member=db.execute("SELECT document_id FROM knowledge_document_members WHERE memory_id=?",(memory_id,)).fetchone()
    if member:
        doc=db.execute("SELECT active_revision_id FROM knowledge_documents WHERE document_id=?",(member["document_id"],)).fetchone()
        return {"action":"already_ingested","document_id":member["document_id"],
                "revision_id":doc["active_revision_id"] if doc else None,"related_candidates":0}
    claim=_claim(observation);identity_json,identity_hash=_identity(claim)
    duplicate=None if force_new else db.execute("""SELECT d.document_id,r.revision_id FROM knowledge_documents d
        JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
        WHERE d.project=? AND d.lifecycle='active' AND r.identity_hash=? AND r.identity_json=?
        ORDER BY d.created LIMIT 1""",(project,identity_hash,identity_json)).fetchone()
    if duplicate:
        doc_id=duplicate["document_id"];rev_id=duplicate["revision_id"]
        db.execute("INSERT INTO knowledge_document_members VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                   (doc_id,memory_id,"attached_exact",now))
        _support_rows(db,rev_id,observation,"unknown")
        _decision(db,"attach_exact_support",[rev_id],doc_id,"exact_identity_same_project_support_added")
        from agenthub.processing.temporal import admit_curated_assertion
        temporal=admit_curated_assertion(db,rev_id,observation)
        return {"action":"attach_exact_support","document_id":doc_id,"revision_id":rev_id,
                "related_candidates":0,"temporal":temporal}
    doc_id=_identifier("doc_",memory_id)
    rev_id=_identifier("rev_",memory_id+":1")
    db.execute("""INSERT INTO knowledge_documents VALUES(?,?,?,?,?,?,?,?,?)""",
        (doc_id,project,session,memory_id,"active",rev_id,SCHEMA_VERSION,now,now))
    db.execute("INSERT INTO knowledge_document_members VALUES(?,?,?,?)",(doc_id,memory_id,"origin",now))
    db.execute("""INSERT INTO knowledge_revisions VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (rev_id,doc_id,1,json.dumps(claim,ensure_ascii=False,sort_keys=True),identity_json,identity_hash,
         None,"singleton_from_observation",SCHEMA_VERSION,now))
    _support_rows(db,rev_id,observation)
    if 'source_context' in claim:
        # Exact original passages are evidence, not inferred assertions or
        # candidates for semantic consolidation. Keep their source identity.
        refresh_index(db,doc_id)
        return {'action':'source_passage','document_id':doc_id,'revision_id':rev_id}
    _decision(db,"create_singleton",[],doc_id,"one_supported_claim_from_curated_observation")
    from agenthub.processing.temporal import admit_curated_assertion
    temporal=admit_curated_assertion(db,rev_id,observation)
    candidates=_possible_relations(db,project,doc_id,rev_id,claim)
    refresh_index(db,doc_id)
    return {"action":"create_singleton","document_id":doc_id,"revision_id":rev_id,
            "related_candidates":candidates,"temporal":temporal}


def apply_resolved_observation(db, memory_id, project, session, observation, operation,
                               target_document_id=None, now=None, *,
                               expected_revision_id=None):
    """Apply one validated resolver decision while preserving immutable revisions."""
    initialize(db)
    now=now or time.time()
    if operation not in {"CREATE","SUPPORT","CORRECT","SUPERSEDE","CONTRADICT",
                          "CONDITIONAL_VARIANT","RELATED"}:
        raise ValueError("invalid_resolution_operation")
    if expected_revision_id is not None:
        if not isinstance(expected_revision_id,str) or not expected_revision_id:
            raise ValueError("invalid_expected_revision")
        if not db.in_transaction:begin_write(db)
    target=None
    if target_document_id:
        target=db.execute("""SELECT * FROM knowledge_documents
            WHERE document_id=? AND project=? AND lifecycle='active'
              AND active_revision_id IS NOT NULL"""+
            (" FOR UPDATE" if expected_revision_id is not None else ""),
            (target_document_id,project)).fetchone()
        if target is None:raise ValueError("resolution_target_unavailable")
        if expected_revision_id is not None and target["active_revision_id"]!=expected_revision_id:
            raise ValueError("stale_resolution_revision")
    if operation in {"SUPPORT","CORRECT","SUPERSEDE","CONTRADICT",
                      "CONDITIONAL_VARIANT","RELATED"} and target is None:
        raise ValueError("resolution_target_required")
    if operation=="CREATE":
        if target_document_id or expected_revision_id is not None:
            raise ValueError("resolution_target_for_create")
        return ingest_observation(db,memory_id,project,session,observation,now,force_new=True)
    if operation=="SUPPORT":
        revision_id=target["active_revision_id"]
        db.execute("INSERT INTO knowledge_document_members VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                   (target_document_id,memory_id,"resolver_support",now))
        _support_rows(db,revision_id,observation,"unknown")
        _decision(db,"resolver_support",[revision_id],target_document_id,
                  "model_classified_compatible_support","model_resolved")
        return {"action":"resolver_support","document_id":target_document_id,
                "revision_id":revision_id,"related_candidates":0}
    if operation in {"CORRECT","SUPERSEDE"}:
        previous_id=target["active_revision_id"]
        previous=db.execute("SELECT revision_number FROM knowledge_revisions WHERE revision_id=?",
                            (previous_id,)).fetchone()
        number=(previous["revision_number"] if previous else 0)+1
        claim=_claim(observation);identity_json,identity_hash=_identity(claim)
        revision_id=_identifier("rev_",f"{target_document_id}:{number}:{memory_id}")
        db.execute("INSERT INTO knowledge_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (revision_id,target_document_id,number,
             json.dumps(claim,ensure_ascii=False,sort_keys=True),identity_json,identity_hash,
             previous_id,"resolver_"+operation.casefold(),SCHEMA_VERSION,now))
        db.execute("INSERT INTO knowledge_document_members VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                   (target_document_id,memory_id,"resolver_"+operation.casefold(),now))
        _support_rows(db,revision_id,observation,"unknown")
        changed=db.execute("""UPDATE knowledge_documents SET lifecycle='active',active_revision_id=?,
            updated=? WHERE document_id=? AND active_revision_id=?""",
            (revision_id,now,target_document_id,previous_id)).rowcount
        if changed!=1:raise ValueError("stale_resolution_revision")
        from agenthub.processing.temporal import admit_curated_assertion
        temporal=admit_curated_assertion(db,revision_id,observation,
            operation=operation,previous_revision_id=previous_id)
        refresh_index(db,target_document_id)
        _decision(db,"resolver_"+operation.casefold(),[previous_id],target_document_id,
                  "model_resolved_immutable_revision","model_resolved")
        return {"action":"resolver_"+operation.casefold(),"document_id":target_document_id,
                "revision_id":revision_id,"previous_revision_id":previous_id,
                "related_candidates":0,"temporal":temporal}
    created=ingest_observation(db,memory_id,project,session,observation,now,force_new=True)
    relation={"CONTRADICT":"contradicts","CONDITIONAL_VARIANT":"conditional_variant",
              "RELATED":"related"}[operation]
    ident=_identifier("rel_",f"{created['document_id']}:{target_document_id}:{relation}:resolver")
    db.execute("""INSERT INTO knowledge_relations
      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",(ident,created["document_id"],target_document_id,
        relation,1.0,"model_resolved_"+relation,"episode-resolver-1","model_resolved",now))
    return created


def active_generation_id(db):
    row=db.execute("SELECT active_generation_id FROM knowledge_generation_state WHERE singleton=1").fetchone()
    return row[0] if row and row[0] else None


def retire_memory(db, memory_id, *, withdrawn=False, replacement_memory_id=None):
    docs=db.execute("""SELECT d.* FROM knowledge_documents d
        JOIN knowledge_document_members mm ON mm.document_id=d.document_id WHERE mm.memory_id=?""",(memory_id,)).fetchall()
    raw_sources=[r[0] for r in db.execute("SELECT source_id FROM observation_sources WHERE memory_id=?",(memory_id,))]
    changed=0
    if not docs:
        # Withdrawing original evidence removes only its support links. The derived
        # member is retired by State._withdraw in the same transaction.
        db.execute("DELETE FROM knowledge_support WHERE source_memory_id=?",(memory_id,))
    for doc in docs:
        doc_id=doc["document_id"]
        if withdrawn:
            db.execute("DELETE FROM knowledge_document_members WHERE document_id=? AND memory_id=?",(doc_id,memory_id))
            for source_id in raw_sources:
                other=db.execute("""SELECT 1 FROM knowledge_document_members mm
                    JOIN observation_sources os ON os.memory_id=mm.memory_id
                    WHERE mm.document_id=? AND mm.memory_id!=? AND os.source_id=? LIMIT 1""",
                    (doc_id,memory_id,source_id)).fetchone()
                if not other:
                    db.execute("""DELETE FROM knowledge_support WHERE source_memory_id=? AND revision_id IN
                        (SELECT revision_id FROM knowledge_revisions WHERE document_id=?)""",(source_id,doc_id))
        else:
            # Owner corrections preserve the superseded revision until an accepted
            # replacement supplies the next immutable revision.
            db.execute("UPDATE knowledge_documents SET lifecycle='superseded',active_revision_id=NULL,updated=? WHERE document_id=?",(time.time(),doc_id))
            refresh_index(db,doc_id)
            changed+=1
            continue
        changed+=1
        remaining=db.execute("""SELECT mm.memory_id FROM knowledge_document_members mm
            JOIN memories m ON m.id=mm.memory_id AND m.active=1
            WHERE mm.document_id=? ORDER BY mm.created LIMIT 1""",(doc_id,)).fetchone()
        if remaining:
            db.execute("UPDATE knowledge_documents SET origin_memory_id=?,updated=? WHERE document_id=?",
                       (remaining["memory_id"],time.time(),doc_id))
            # A withdrawn correction can remain the newest immutable revision even
            # after its evidence is removed. Reactivate only a revision that still
            # has support; otherwise retrieval would publish the withdrawn claim.
            latest=db.execute("""SELECT r.revision_id FROM knowledge_revisions r
                WHERE r.document_id=? AND EXISTS (
                    SELECT 1 FROM knowledge_support s WHERE s.revision_id=r.revision_id)
                ORDER BY r.revision_number DESC LIMIT 1""",(doc_id,)).fetchone()
            db.execute("UPDATE knowledge_documents SET active_revision_id=?,lifecycle=? WHERE document_id=?",
                       (latest["revision_id"] if latest else None,"active" if latest else "withdrawn",doc_id))
            refresh_index(db,doc_id)
        elif withdrawn:
            db.execute("DELETE FROM knowledge_relations WHERE document_id=? OR related_document_id=?",(doc_id,doc_id))
            db.execute("DELETE FROM knowledge_support WHERE revision_id IN (SELECT revision_id FROM knowledge_revisions WHERE document_id=?)",(doc_id,))
            # Temporal assertions are revision derivatives, not independent
            # evidence. Remove their children before deleting the unsupported
            # revisions (PostgreSQL enforces these provenance foreign keys).
            if table_exists(db,'knowledge_temporal_assertions'):
                assertions="SELECT assertion_id FROM knowledge_temporal_assertions WHERE document_id=?"
                if table_exists(db,'knowledge_temporal_relations'):
                    db.execute("DELETE FROM knowledge_temporal_relations WHERE new_assertion_id IN ("+
                        assertions+") OR old_assertion_id IN ("+assertions+")",(doc_id,doc_id))
                if table_exists(db,'knowledge_temporal_evidence'):
                    db.execute("DELETE FROM knowledge_temporal_evidence WHERE assertion_id IN ("+
                        assertions+")",(doc_id,))
                db.execute("DELETE FROM knowledge_temporal_assertions WHERE document_id=?",(doc_id,))
            db.execute("DELETE FROM knowledge_revisions WHERE document_id=?",(doc_id,))
            db.execute("UPDATE knowledge_documents SET lifecycle='withdrawn',active_revision_id=NULL,updated=? WHERE document_id=?",(time.time(),doc_id))
            refresh_index(db,doc_id)
    return changed


def split_support(db, document_id, member_memory_ids, project):
    """Undo a mistaken exact consolidation by rebuilding selected evidence as singletons."""
    document=db.execute("SELECT * FROM knowledge_documents WHERE document_id=? AND project=? AND lifecycle='active'",
                        (document_id,project)).fetchone()
    if not document:raise ValueError("knowledge_document_unavailable")
    if not isinstance(member_memory_ids,list) or not member_memory_ids or len(member_memory_ids)>40:
        raise ValueError("invalid_split_sources")
    revision=document["active_revision_id"]
    known={row[0] for row in db.execute("SELECT memory_id FROM knowledge_document_members WHERE document_id=?",(document_id,))}
    selected=set(member_memory_ids)
    if not selected<=known:raise ValueError("split_source_not_supported")
    rebuilt=[]
    for memory_id in sorted(selected):
        row=db.execute("SELECT body,kind,session FROM memories WHERE id=? AND project=? AND active=1",(memory_id,project)).fetchone()
        if not row or row["kind"]!="Observation":raise ValueError("split_source_unavailable")
        observation=json.loads(row["body"])
        raw_sources=[r[0] for r in db.execute("SELECT source_id FROM observation_sources WHERE memory_id=?",(memory_id,))]
        db.execute("DELETE FROM knowledge_document_members WHERE document_id=? AND memory_id=?",(document_id,memory_id))
        if document["origin_memory_id"]==memory_id:
            replacement_origin=db.execute("SELECT memory_id FROM knowledge_document_members WHERE document_id=? ORDER BY created LIMIT 1",(document_id,)).fetchone()
            db.execute("UPDATE knowledge_documents SET origin_memory_id=? WHERE document_id=?",
                (replacement_origin["memory_id"] if replacement_origin else document_id+":split",document_id))
        result=ingest_observation(db,memory_id,project,row["session"],observation,force_new=True)
        new_doc=result["document_id"]
        db.execute("UPDATE knowledge_revisions SET reason='split_from_consolidation' WHERE revision_id=?",(result["revision_id"],))
        _decision(db,"split_rebuild",[revision],new_doc,"owner_or_operator_undid_exact_consolidation")
        rebuilt.append(new_doc)
        for source_id in raw_sources:
            other=db.execute("""SELECT 1 FROM knowledge_document_members mm
                JOIN observation_sources os ON os.memory_id=mm.memory_id
                WHERE mm.document_id=? AND os.source_id=? LIMIT 1""",(document_id,source_id)).fetchone()
            if not other:db.execute("DELETE FROM knowledge_support WHERE revision_id=? AND source_memory_id=?",(revision,source_id))
    remaining=db.execute("SELECT count(*) FROM knowledge_support WHERE revision_id=?",(revision,)).fetchone()[0]
    if not remaining:
        db.execute("UPDATE knowledge_documents SET lifecycle='retired',active_revision_id=NULL,updated=? WHERE document_id=?",(time.time(),document_id))
    else:
        replacement=db.execute("SELECT memory_id FROM knowledge_document_members WHERE document_id=? ORDER BY created LIMIT 1",(document_id,)).fetchone()
        if replacement:db.execute("UPDATE knowledge_documents SET origin_memory_id=?,updated=? WHERE document_id=?",
                                  (replacement["memory_id"],time.time(),document_id))
    _decision(db,"split_remove_support",[revision],document_id,"selected_support_rebuilt_as_singletons","owner_reviewed")
    refresh_index(db,document_id)
    return {"document_id":document_id,"rebuilt_document_ids":rebuilt,"remaining_supports":remaining}


def accept_local_candidate(db, memory_id, project, session, title, evidence, source_ids,
                           replacement_ids=()):
    """Install a reviewed private candidate, using an immutable revision for corrections."""
    initialize(db)
    claim={"title":title,"problem":"","lesson":evidence,"applicability":"",
        "applicability_constraints":{"versions":[],"platforms":[],"date_ranges":[],"units":[],"project_scope":[]},
        "knowledge_type":"observation","domain":"unknown","subjects":[],"tags":[],"outcome":"unknown"}
    replacement_ids=list(replacement_ids)
    target=None
    if replacement_ids:
        for old_id in replacement_ids:
            target=db.execute("""SELECT d.* FROM knowledge_documents d
                JOIN knowledge_document_members mm ON mm.document_id=d.document_id
                WHERE mm.memory_id=? AND d.project=? ORDER BY d.created LIMIT 1""",(old_id,project)).fetchone()
            if target:break
    if target:
        doc_id=target["document_id"]
        previous=db.execute("SELECT revision_id,revision_number FROM knowledge_revisions WHERE document_id=? ORDER BY revision_number DESC LIMIT 1",(doc_id,)).fetchone()
        number=(previous["revision_number"]+1) if previous else 1
        previous_id=previous["revision_id"] if previous else None
        revision_id=_identifier("rev_",f"{doc_id}:{number}:{memory_id}")
        identity_json,identity_hash=_identity(claim);now=time.time()
        db.execute("INSERT INTO knowledge_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (revision_id,doc_id,number,json.dumps(claim,ensure_ascii=False,sort_keys=True),identity_json,identity_hash,
             previous_id,"owner_correction",SCHEMA_VERSION,now))
        db.execute("INSERT INTO knowledge_document_members VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
            (doc_id,memory_id,"owner_correction",now))
        for source_id in source_ids:
            db.execute("INSERT INTO knowledge_support VALUES(?,?,NULL,'supports','unknown',?) ON CONFLICT DO NOTHING",
                       (revision_id,source_id,now))
        db.execute("UPDATE knowledge_documents SET lifecycle='active',active_revision_id=?,updated=? WHERE document_id=?",
                   (revision_id,now,doc_id))
        refresh_index(db,doc_id)
        _decision(db,"owner_correction",[previous_id] if previous_id else [],doc_id,
                  "explicit_owner_correction_preserved_prior_revision","owner_reviewed")
        for old_id in replacement_ids:
            if old_id==target["origin_memory_id"]:continue
            other=db.execute("""SELECT d.document_id,d.active_revision_id FROM knowledge_documents d
                JOIN knowledge_document_members mm ON mm.document_id=d.document_id
                WHERE mm.memory_id=? AND d.project=?""",(old_id,project)).fetchone()
            if other and other["document_id"]!=doc_id:
                _decision(db,"supersede_related",[other["active_revision_id"]] if other["active_revision_id"] else [],
                    other["document_id"],"owner_correction_replaced_additional_claim","owner_reviewed")
                db.execute("UPDATE knowledge_documents SET lifecycle='superseded',active_revision_id=NULL,updated=? WHERE document_id=?",
                           (now,other["document_id"]))
                refresh_index(db,other["document_id"])
        return {"action":"owner_correction","document_id":doc_id,"revision_id":revision_id,
                "previous_revision_id":previous_id}
    observation={**claim,"evidence":[{"source_id":ident} for ident in source_ids]}
    return ingest_observation(db,memory_id,project,session,observation)






def compatibility(claim, query, project):
    """Hard-reject only explicit conflicts; missing conditions remain unknown."""
    query_lower=query.casefold();constraints=claim.get("applicability_constraints") or {}
    if not isinstance(constraints,dict):constraints={}
    unknown=[]
    versions=constraints.get("versions") or []
    requested=re.findall(r"\b(?:version\s*|v)(\d+(?:\.\d+){0,2})\b",query,re.I)
    if requested:
        stated={version for value in versions for version in re.findall(r"\d+(?:\.\d+){0,2}",str(value))}
        if not stated:unknown.append("version_condition_unspecified")
        elif not set(requested)&stated:
            return "incompatible",["version_mismatch"]
    platforms=constraints.get("platforms") or []
    platform_aliases={"macos":("macos","mac os","darwin"),"windows":("windows","win32"),
                      "linux":("linux",),"ios":("ios","iphone","ipad"),"android":("android",)}
    requested_platform={name for name,aliases in platform_aliases.items() if any(alias in query_lower for alias in aliases)}
    if requested_platform:
        if not platforms:unknown.append("platform_condition_unspecified")
        else:
            stated=" ".join(map(str,platforms)).casefold()
            compatible={name for name,aliases in platform_aliases.items() if any(alias in stated for alias in aliases)}
            if compatible and not requested_platform&compatible:return "incompatible",["platform_mismatch"]
    requested_project=constraints.get("project_scope") or []
    if requested_project and project not in {str(value) for value in requested_project}:
        return "incompatible",["project_scope_mismatch"]
    query_dates=re.findall(r"\b20\d{2}(?:-\d{2}(?:-\d{2})?)?\b",query)
    date_ranges=constraints.get("date_ranges") or []
    if query_dates:
        if not date_ranges:unknown.append("date_condition_unspecified")
        else:
            query_points={_date_bounds(value)[0] for value in query_dates}
            ranges=[_date_bounds(value) for value in date_ranges]
            ranges=[item for item in ranges if item]
            if ranges and not any(start<=point<=end for point in query_points for start,end in ranges):
                return "incompatible",["date_range_mismatch"]
            if not ranges:unknown.append("date_range_uninterpretable")
    units=constraints.get("units") or []
    query_units={value.casefold() for value in re.findall(r"(?<!\w)(?:USD|EUR|GBP|CAD|AUD|kg|g|mg|lb|lbs|km|mi|m/s|ms|s|%)(?!\w)",query,re.I)}
    if query_units and units:
        stated={str(value).casefold() for value in units}
        if not query_units&stated:return "incompatible",["unit_mismatch"]
    elif query_units and not units:unknown.append("unit_condition_unspecified")
    return ("unknown",unknown) if unknown else ("compatible",[])


def _date_bounds(value):
    """Parse explicit ISO year/month/day or a two-endpoint ISO range."""
    if isinstance(value,dict):value=" ".join(str(value.get(key,"")) for key in ("start","end"))
    found=re.findall(r"20\d{2}(?:-\d{2}(?:-\d{2})?)?",str(value))
    if not found:return None
    def bounds(token):
        parts=token.split("-")
        if len(parts)==1:return parts[0]+"-01-01",parts[0]+"-12-31"
        if len(parts)==2:
            month=int(parts[1]);last=31 if month in (1,3,5,7,8,10,12) else (29 if month==2 else 30)
            return f"{parts[0]}-{month:02d}-01",f"{parts[0]}-{month:02d}-{last:02d}"
        return token,token
    first=bounds(found[0]);last=bounds(found[-1])
    return first[0],last[1]


def rebuild_index(db, *, dry_run=True, batch_size=200, max_batches=None):
    """Build a hidden FTS copy in resumable batches, then atomically swap it live."""
    if type(batch_size)is not int or not 1<=batch_size<=2000:raise ValueError("invalid_batch_size")
    if max_batches is not None and (type(max_batches)is not int or max_batches<1):raise ValueError("invalid_max_batches")
    total=db.execute("SELECT count(*) FROM knowledge_documents WHERE lifecycle='active' AND active_revision_id IS NOT NULL").fetchone()[0]
    if dry_run:return {"mode":"dry_run","schema_version":INDEX_SCHEMA_VERSION,"eligible":total,"indexed":0,"model_calls":0}
    build=db.execute("SELECT * FROM knowledge_index_build WHERE singleton=1").fetchone()
    staging_fts=table_exists(db,'knowledge_fts_staging')
    staging_rows=table_exists(db,'knowledge_index_rows_staging')
    if build and (build["schema_version"]!=INDEX_SCHEMA_VERSION or not staging_fts or not staging_rows):
        with db:
            db.execute("DROP TABLE IF EXISTS knowledge_fts_staging")
            db.execute("DROP TABLE IF EXISTS knowledge_index_rows_staging")
            db.execute("DELETE FROM knowledge_index_build WHERE singleton=1")
        build=None
    if not build:
        with db:
            create_fulltext(db,"knowledge_fts_staging")
            db.execute("CREATE TABLE IF NOT EXISTS knowledge_index_rows_staging(document_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL)")
            db.execute("DELETE FROM knowledge_fts_staging")
            db.execute("DELETE FROM knowledge_index_rows_staging")
            db.execute("INSERT INTO knowledge_index_build VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version,last_document_id=excluded.last_document_id,started=excluded.started",(INDEX_SCHEMA_VERSION,"",time.time()))
        build=db.execute("SELECT * FROM knowledge_index_build WHERE singleton=1").fetchone()
    batches=0;indexed=0
    while max_batches is None or batches<max_batches:
        rows=db.execute("SELECT d.document_id,d.active_revision_id,r.claim_json FROM knowledge_documents d "
            "JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id "
            "WHERE d.lifecycle='active' AND d.document_id>? ORDER BY d.document_id LIMIT ?",
            (build["last_document_id"],batch_size)).fetchall()
        if not rows:break
        with db:
            for row in rows:
                claim=json.loads(row["claim_json"])
                db.execute("DELETE FROM knowledge_fts_staging WHERE document_id=?",(row["document_id"],))
                db.execute("DELETE FROM knowledge_index_rows_staging WHERE document_id=?",(row["document_id"],))
                db.execute("INSERT INTO knowledge_fts_staging(document_id,revision_id,body) VALUES(?,?,?)",
                    (row["document_id"],row["active_revision_id"],_index_text(claim)))
                db.execute("INSERT INTO knowledge_index_rows_staging(document_id,revision_id) VALUES(?,?)",
                    (row["document_id"],row["active_revision_id"]))
            last=rows[-1]["document_id"]
            db.execute("UPDATE knowledge_index_build SET last_document_id=? WHERE singleton=1",(last,))
        build=db.execute("SELECT * FROM knowledge_index_build WHERE singleton=1").fetchone()
        indexed+=len(rows);batches+=1
    remaining=db.execute("SELECT 1 FROM knowledge_documents WHERE lifecycle='active' AND active_revision_id IS NOT NULL AND document_id>? LIMIT 1",
        (build["last_document_id"],)).fetchone()
    if remaining:
        return {"mode":"building","schema_version":INDEX_SCHEMA_VERSION,"eligible":total,"indexed":indexed,
                "last_document_id":build["last_document_id"],"model_calls":0}
    with db:
        # Mutations mirror into staging in the same transaction, so the staged
        # identifier map and FTS rows are ready for one atomic table swap.
        db.execute("DROP TABLE knowledge_fts")
        db.execute("DROP TABLE knowledge_index_rows")
        db.execute("ALTER TABLE knowledge_fts_staging RENAME TO knowledge_fts")
        db.execute("ALTER TABLE knowledge_index_rows_staging RENAME TO knowledge_index_rows")
        db.execute("INSERT INTO knowledge_index_state VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version",(INDEX_SCHEMA_VERSION,))
        db.execute("DELETE FROM knowledge_index_build WHERE singleton=1")
    actual=db.execute("SELECT count(*) FROM knowledge_fts").fetchone()[0]
    return {"mode":"complete","schema_version":INDEX_SCHEMA_VERSION,"eligible":total,"indexed":actual,"model_calls":0}
