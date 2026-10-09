"""Generation-backed private curation of complete Codex task episodes.

Source events remain private evidence.  Each complete project/session/turn is one
durable job, even when transport bounds require several fresh model calls.  A
generation is invisible to retrieval until it is explicitly activated.
"""
from __future__ import annotations

from agenthub.processing.storage import table_exists, begin_write, fulltext_predicate, fulltext_rank

import hashlib
import json
import re
import time
from contextlib import nullcontext

from agenthub.processing.episode_curator import RESOLUTION_PROMPT, EpisodeError, resolution_payload, resolution_schema, validate_resolution
from agenthub.processing.harness import HarnessError, run_structured


SOURCE_POLICY = "complete_trusted_turn_v1"
REVIEWED_REPROCESSING_GUIDANCE = """This complete episode was selected for a reviewed extraction repair.
Recheck the readable original source for omitted durable facts, correct speaker
attribution and self-contained records. Preserve material exact names, file paths,
settings, quantitative rules and thresholds, and stated completion limitations.
Do not replace a supported specific value or location with a vague phrase such as
"the proposed threshold" or "the implementation files". Keep coherent findings
together rather than producing a record per token. Advice remains advice, reports
remain reported, and a missing detail remains unknown. The review reason is not
evidence; cite original spans and never invent missing details."""
RETRY_VALIDATION_GUIDANCE = {
    'memory_current_evidence':'Choose the anchor first. Put a span belonging to that exact event_id first in evidence_span_ids, reserving one of the eight slots for it; a nearby uncited final response cannot anchor the record. Also cite at least one eligible current event span. Unchanged earlier facts or repeated completion anchors alone are not new work; omit those records.',
    'reference_unknown':'Copy the exact s-prefixed span and e-prefixed event handles supplied in this request; never invent or reuse an unavailable handle.',
    'memory_date_evidence':'Leave occurred_date blank unless that exact YYYY-MM-DD is literally present in cited readable source text. Event timestamps and earlier uncited dates are not that text.',
    'memory_unverified_outcome':'Check the chosen anchor event\'s observed_allowed field before choosing state. Use observed only when it is true; a saved screen, assistant final or unverified tool output cannot establish independently verified execution. Otherwise use reported with a cited user or assistant report, reserving a citation slot for that speaker event, or omit the claim when no such report exists.',
    'memory_location_evidence':'Use artifact only when its exact name and location occur in cited original spans. Do not reconstruct redacted paths; use an ordinary fact/activity with artifact null where supported.',
    'memory_unverified_location':'An attempted or requested save is not a completed current location. Preserve the supported attempted state or omit the location assertion.',
    'memory_reason_evidence':'A rationale needs an exact substantive original quote and its actual speaker citation. Leave rationale null when that evidence is absent.',
}
TERMINAL = {"done", "no_learning", "withdrawn", "unprocessed"}


def curator_version(config):
    if config.get("episode_curation", {}).get("policy", "durable_memory") != "durable_memory":
        raise ValueError("retired_curation_policy")
    from agenthub.processing.durable_memory import VERSION
    return VERSION


def _hash(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def initialize(db):
    db.require_schema()


def generation_id(config):
    settings=config.get("episode_curation", {})
    from agenthub.processing.media_projection import validate_policy
    validate_policy(settings.get('media_policy'))
    value=settings.get("generation_id") or "local-" + curator_version(config)
    if not isinstance(value, str) or not value or len(value) > 120:
        raise ValueError("invalid_episode_generation_id")
    return value


def ensure_generation(db, config):
    initialize(db)
    ident=generation_id(config)
    settings=config.get("episode_curation", {})
    bounded={key:settings.get(key) for key in
             ("max_events_per_stage", "max_chars_per_stage", "max_stages",
              "max_reducer_chars", "settle_seconds")}
    # Optional policies are part of a new generation definition. Omitted keys
    # preserve the hashes of existing generations and their frozen behavior.
    bounded.update({key:settings[key] for key in ('split_oversized_events','evidence_guidance','media_policy')
                    if key in settings})
    config_hash=_hash(bounded)
    row=db.execute("SELECT * FROM knowledge_generations WHERE generation_id=?",(ident,)).fetchone()
    if not row:
        # Two bounded workers may prepare the same generation concurrently.
        # Re-read the winner so a conflicting definition still fails closed.
        db.execute("""INSERT INTO knowledge_generations
            (generation_id,curator_version,status,source_policy,config_hash,created)
            VALUES(?,?,'building',?,?,?) ON CONFLICT(generation_id) DO NOTHING""",
            (ident,curator_version(config),SOURCE_POLICY,config_hash,time.time()))
        row=db.execute("SELECT * FROM knowledge_generations WHERE generation_id=?",(ident,)).fetchone()
    if (not row or row["curator_version"] != curator_version(config)
            or row["source_policy"] != SOURCE_POLICY or row["config_hash"] != config_hash):
        raise ValueError("episode_generation_definition_changed")
    return ident


def _trusted_source_clause(alias="m"):
    return f"""(
      EXISTS (SELECT 1 FROM codex_backfill_events cbe WHERE cbe.event_id={alias}.id)
      OR EXISTS (SELECT 1 FROM source_event_metadata trusted
                 WHERE trusted.source_id={alias}.id AND trusted.source_role='episode_evidence'))"""


def create_jobs(state, config, *, limit=100, project_scope=None, connection_ids=None):
    """Queue each settled complete trusted turn as exactly one durable job."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid_episode_job_limit")
    enterprise=config.get('knowledge_backend',{}).get('mode')=='enterprise_local'
    connection_clause="";connections=[]
    if connection_ids is not None:
        if not enterprise:raise ValueError('enterprise_connection_scope_required')
        if not isinstance(connection_ids,(list,tuple)) or not 1<=len(connection_ids)<=100:
            raise ValueError('invalid_episode_connection_scope')
        from agentclient.general_contract import text
        connections=sorted(set(text(value,128) for value in connection_ids))
        connection_clause=" AND EXISTS (SELECT 1 FROM backend_source_revisions selected_source WHERE selected_source.source_id=m.id AND selected_source.connection IN ("+','.join('?' for _ in connections)+"))"
    settings=config.get("episode_curation", {})
    ident=ensure_generation(state.db,config)
    settle=settings.get("settle_seconds",config.get("observer",{}).get("settle_seconds",15))
    before=time.time()-max(0,float(settle))
    trusted=_trusted_source_clause("m")
    if enterprise and not project_scope:
        raise ValueError('enterprise_curation_scope_required')
    kinds = "'UserPromptSubmit','Stop','PostToolUse'" + (",'AssistantMessage'" if enterprise else "")
    extra=" AND m.project=? AND EXISTS (SELECT 1 FROM enterprise_sources es WHERE es.id=m.id AND es.active=1 AND es.internal_project=m.project)" if enterprise else ""
    groups=state.db.execute(f"""SELECT m.project,m.session,m.turn,min(m.created) first_created
        FROM memories m
        WHERE m.active=1 AND m.turn!=''
          AND m.kind IN ({kinds})
          AND m.created<? AND {trusted}
          AND NOT EXISTS (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)
          AND NOT EXISTS (SELECT 1 FROM source_event_metadata excluded
              WHERE excluded.source_id=m.id AND excluded.source_role IN
              ('context_transfer','retrieved_memory','task_control'))
          {extra}{connection_clause}
          AND NOT EXISTS (SELECT 1 FROM curation_episode_jobs finished
              WHERE finished.generation_id=? AND finished.project=m.project
                AND finished.session=m.session AND finished.turn=m.turn
                AND finished.status IN ('done','no_learning','withdrawn','unprocessed'))
        GROUP BY m.project,m.session,m.turn
        HAVING sum(CASE WHEN m.kind='UserPromptSubmit' THEN 1 ELSE 0 END)>0 AND sum(CASE WHEN m.kind='Stop' THEN 1 ELSE 0 END)>0
        ORDER BY first_created LIMIT ?""",(before,*([project_scope] if enterprise else []),*connections,ident,limit)).fetchall()
    made=0
    for group in groups:
        rows=state.db.execute(f"""SELECT m.id,m.created FROM memories m
            WHERE m.project=? AND m.session=? AND m.turn=? AND m.active=1
              AND m.kind IN ({kinds}) AND {trusted}
              AND NOT EXISTS (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)
              AND NOT EXISTS (SELECT 1 FROM source_event_metadata excluded
                WHERE excluded.source_id=m.id AND excluded.source_role IN
                ('context_transfer','retrieved_memory','task_control'))
              {connection_clause}
            ORDER BY m.created,m.id""",
            (group["project"],group["session"],group["turn"],*connections)).fetchall()
        source_ids=[row["id"] for row in rows]
        if enterprise:
            policies=state.db.execute("SELECT id,policy_version,active,visibility FROM enterprise_sources WHERE id IN ("+
                ",".join("?" for _ in source_ids)+")",source_ids).fetchall() if source_ids else []
            if len(policies)!=len(source_ids) or any(not p["active"] for p in policies):continue
            source_hash=_hash([source_ids,sorted((p['id'],p['policy_version'],p['active'],p['visibility']) for p in policies)])
        else:
            source_hash=_hash(source_ids)
        episode_id=_hash([group["project"],group["session"],group["turn"],source_ids])
        job_id=_hash([ident,episode_id,curator_version(config)])
        existing=state.db.execute("""SELECT id,status,source_hash FROM curation_episode_jobs
            WHERE generation_id=? AND project=? AND session=? AND turn=?""",
            (ident,group["project"],group["session"],group["turn"])).fetchone()
        if existing:
            if existing["source_hash"] != source_hash and existing["status"] not in {'withdrawn','unprocessed'}:
                state.db.execute("""UPDATE curation_episode_jobs SET source_ids=?,source_hash=?,
                    episode_id=?,progress='{}',stage='curate',attempts=0,next_attempt=0,
                    updated=?,error=NULL,status='pending' WHERE id=?""",
                    (json.dumps(source_ids),source_hash,episode_id,time.time(),existing["id"]))
            continue
        now=time.time()
        made += state.db.execute("""INSERT INTO curation_episode_jobs
          (id,generation_id,episode_id,project,session,turn,source_ids,source_hash,
           created,updated,version) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
          (job_id,ident,episode_id,group["project"],group["session"],group["turn"],
           json.dumps(source_ids),source_hash,now,now,curator_version(config))).rowcount
    return made


def sources_for_job(db, row):
    ids=json.loads(row["source_ids"])
    if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):
        raise ValueError("episode_source_ids")
    # Bounded batches keep SQL work independent of the number of source events.
    # Preserve input order and every former per-source authorization check.
    rows_by_id={}; originals={}; policies={}
    has_originals=state_table_exists(db,'backend_source_revisions')
    enterprise=row['project'].startswith('enterprise:')
    for offset in range(0,len(ids),400):
        batch=ids[offset:offset+400];marks=','.join('?' for _ in batch)
        rows=db.execute("""SELECT m.id,m.body,m.kind,m.exit_code,m.project,m.session,m.turn,
            m.created,COALESCE(sem.tool_name,'') tool_name,
            COALESCE(sem.source_role,'episode_evidence') source_role
            FROM memories m LEFT JOIN source_event_metadata sem ON sem.source_id=m.id
            WHERE m.id IN ("""+marks+""") AND m.active=1
              AND NOT EXISTS (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)""",batch).fetchall()
        rows_by_id.update((v['id'],v) for v in rows)
        if has_originals:
            originals.update((v['source_id'],v) for v in db.execute(
                'SELECT source_id,payload FROM backend_source_revisions WHERE source_id IN ('+marks+')',batch))
        if enterprise:
            policies.update((v['id'],v) for v in db.execute(
                'SELECT id,active,internal_project,policy_version,visibility,occurred_at,occurred_precision,occurred_timezone FROM enterprise_sources WHERE id IN ('+marks+')',batch))
    result=[]
    for source_id in ids:
        source=rows_by_id.get(source_id)
        if source is None:raise ValueError("source_withdrawn")
        if (source["project"],source["session"],source["turn"]) != (
                row["project"],row["session"],row["turn"]):
            raise ValueError("episode_source_identity")
        if source["source_role"] in {"context_transfer","retrieved_memory","task_control"}:
            raise ValueError("episode_source_role_changed")
        item=dict(source)
        if has_originals:
            original=originals.get(source_id)
            if original:
                captured=json.loads(original['payload']);typed=captured.get('event',{})
                item['source_order']=typed.get('order')
                item.update({key:typed.get(key,'') for key in ('role','channel','call_id','parent_id')})
                item['actor']=captured.get('actor','')
                item['native_kind']=typed.get('kind','')
                statuses=[block['value'].get('status') for block in captured.get('blocks',[])
                    if block['type']=='tool_result' and isinstance(block['value'],dict)]
                item['execution_success']=(typed.get('exit_code')==0 and type(typed.get('exit_code')) is int
                    and typed.get('tool_name') in {'save_file','patch_file'} and any(s in {'saved','applied'} for s in statuses))
        if row["project"].startswith("enterprise:"):
            policy=policies.get(source_id)
            if not policy or not policy["active"] or policy["internal_project"]!=row["project"]:
                raise ValueError("enterprise_source_policy_changed")
            item["policy_version"]=policy["policy_version"]
            item["visibility"]=policy["visibility"]
            item["occurred_at"]=policy["occurred_at"]
            item["occurred_precision"]=policy["occurred_precision"]
            item["occurred_timezone"]=policy["occurred_timezone"]
        result.append(item)
    if not any(item["kind"]=="UserPromptSubmit" for item in result) or not any(
            item["kind"]=="Stop" for item in result):
        raise ValueError("episode_incomplete")
    return result


def state_table_exists(db,name):
    return bool(table_exists(db,name))


def _candidate_terms(candidate):
    from agentclient.cleaning import terms
    values=[candidate.get(key,"") for key in
            ("title","claim","problem","action","outcome")]
    values.extend(candidate.get("subjects") or [])
    values.extend(candidate.get("aliases") or [])
    record=candidate.get("memory_record") or {}
    if isinstance(record,dict):
        values.extend(record.get(key,"") for key in ("subject","artifact_name"))
    return set(terms(" ".join(map(str,values))))


def _candidate_entity_keys(candidate):
    from agenthub.processing.knowledge import _candidate_keys
    record=candidate.get("memory_record") or {}
    context=({"subject":record.get("subject"),
              "artifact_name":record.get("artifact_name")}
             if isinstance(record,dict) else {})
    return set(_candidate_keys({"subjects":candidate.get("subjects") or [],
        "aliases":candidate.get("aliases") or [],"memory_context":context}))


def related_artifacts(db, generation, project, candidate, *, limit=8, include_active=True,
                      enterprise_scope=False):
    """Discover a bounded same-project comparison set before model resolution."""
    from agenthub.processing.knowledge import active_generation_id, _candidate_keys
    from agenthub.processing.storage import lexical_query
    if type(limit) is not int or not 1<=limit<=20:
        raise ValueError("invalid_related_artifact_limit")
    allowed={generation}
    active=active_generation_id(db)
    if active and include_active:allowed.add(active)
    marks=",".join("?" for _ in allowed)
    base=("""SELECT DISTINCT d.document_id,r.revision_id,r.claim_json,d.lifecycle,d.updated
        FROM knowledge_documents d JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
        JOIN knowledge_generation_documents kgd ON kgd.document_id=d.document_id
        WHERE d.project=? AND d.lifecycle='active' AND kgd.generation_id IN ("""+marks+
        ")")
    if enterprise_scope:
        # Apply current source eligibility before the bounded shortlist. A stale
        # or denied document must not displace an authorized candidate.
        base+=""" AND EXISTS (SELECT 1 FROM knowledge_support s
            JOIN enterprise_sources es ON es.id=s.source_memory_id
            WHERE s.revision_id=r.revision_id AND es.active=1
              AND es.internal_project=d.project)
            AND NOT EXISTS (SELECT 1 FROM knowledge_support s
                LEFT JOIN enterprise_sources es ON es.id=s.source_memory_id
                WHERE s.revision_id=r.revision_id AND
                  (es.id IS NULL OR es.active!=1 OR es.internal_project!=d.project))
            AND NOT EXISTS (SELECT 1 FROM enterprise_documents ed
                WHERE ed.id=d.document_id AND ed.active!=1)"""
    scope=(project,*sorted(allowed))
    rows_by_id={}
    wanted_keys=_candidate_entity_keys(candidate)
    if wanted_keys:
        checks=" OR ".join("(k.key_kind=? AND k.key_value=?)" for _ in wanted_keys)
        args=[part for pair in sorted(wanted_keys) for part in pair]
        keyed=db.execute(base+""" AND EXISTS (SELECT 1 FROM knowledge_candidate_keys k
            WHERE k.project=d.project AND k.document_id=d.document_id
              AND k.revision_id=r.revision_id AND ("""+checks+"""))
            ORDER BY d.updated DESC,d.document_id LIMIT ?""",
            (*scope,*args,max(80,limit*10))).fetchall()
        rows_by_id.update((row["document_id"],row) for row in keyed)
    wanted=_candidate_terms(candidate)
    if wanted:
        match=lexical_query(db,sorted(wanted))
        # The exact-key branch is deliberately bounded. Let a specific old
        # lexical match compete before recency in its independent bounded lane.
        lexical_base=base.replace(
            "SELECT DISTINCT d.document_id,r.revision_id,r.claim_json,d.lifecycle,d.updated",
            "SELECT DISTINCT d.document_id,r.revision_id,r.claim_json,d.lifecycle,d.updated,"+
            fulltext_rank(db)+" AS match_rank",1).replace(
            " WHERE d.project=?",
            " JOIN knowledge_fts ON knowledge_fts.document_id=d.document_id"+
            " AND knowledge_fts.revision_id=r.revision_id WHERE d.project=?",1)
        rank_args=(match,)
        lexical=db.execute(lexical_base+" AND "+fulltext_predicate(db)+
            " ORDER BY match_rank ASC,d.updated DESC,d.document_id LIMIT ?",
            (*rank_args,*scope,match,max(80,limit*10))).fetchall()
        rows_by_id.update((row["document_id"],row) for row in lexical)
    rows=list(rows_by_id.values())
    if enterprise_scope:
        filtered=[]
        for row in rows:
            supports=db.execute('SELECT source_memory_id FROM knowledge_support WHERE revision_id=?',
                (row['revision_id'],)).fetchall()
            if not supports:continue
            ids=[item[0] for item in supports]
            source_rows=db.execute('SELECT id,active,internal_project FROM enterprise_sources WHERE id IN ('+
                ','.join('?' for _ in ids)+')',ids).fetchall()
            if len(source_rows)!=len(ids) or any(not item['active'] or item['internal_project']!=project for item in source_rows):
                continue
            registered=db.execute('SELECT active FROM enterprise_documents WHERE id=?',(row['document_id'],)).fetchone()
            if registered and not registered['active']:continue
            filtered.append(row)
        rows=filtered
    ranked=[]
    for row in rows:
        claim=json.loads(row["claim_json"]);present=_candidate_terms({
            "title":claim.get("title",""),"claim":claim.get("lesson",""),
            "problem":claim.get("problem",""),"action":claim.get("action",""),
            "outcome":claim.get("outcome_text",""),"subjects":claim.get("subjects",[]),
            "aliases":claim.get("aliases",[]),
            "memory_record":claim.get("memory_context",{})})
        overlap=len(wanted & present)
        key_overlap=wanted_keys & set(_candidate_keys(claim))
        if not overlap and not key_overlap:continue
        key_score=(1.0 if any(kind in {"subject","artifact"} for kind,_ in key_overlap)
                   else 0.7 if key_overlap else 0.0)
        ranked.append((key_score,overlap/max(1,len(wanted|present)),row,claim))
    ranked.sort(key=lambda item:(-item[0],-item[1],-item[2]["updated"],item[2]["document_id"]))
    return [{"artifact_id":row["document_id"],"claim":claim,
             "evidence_status":claim.get("evidence_status","unknown"),
             "applicability":claim.get("applicability_constraints",{}),
             "lifecycle":row["lifecycle"],"revision":row["revision_id"]}
            for _,_,row,claim in ranked[:limit]]


def _fenced_resolution(raw, candidate, compare, *, saved=False):
    """Bind a resolver choice to the immutable revision it was shown."""
    if not isinstance(raw, dict):
        return validate_resolution(raw,candidate,compare)
    public={key:value for key,value in raw.items() if key!="_expected_revision_id"}
    target=public.get("target_artifact_id")
    displayed=next((item["revision"] for item in compare["related_artifacts"]
                    if item["artifact_id"]==target),None) if target else ""
    if saved and target and (not displayed or raw.get("_expected_revision_id")!=displayed):
        raise ValueError("stale_resolution_revision")
    checked=validate_resolution(public,candidate,compare)
    if target and not displayed:
        raise ValueError("resolution_target_revision_missing")
    return {**checked,"_expected_revision_id":displayed}


def _observation(candidate, packet):
    if "memory_record" not in candidate:
        raise ValueError("validated_memory_record_required")
    from agenthub.processing.durable_memory import observation_for
    return observation_for(candidate["memory_record"], packet)


def _history_atom(candidate, observation, operation):
    """Keep a validated occurrence even when its claim later changes."""
    return {"atom_key":candidate["candidate_key"],"record":candidate["memory_record"],
            "evidence":observation["evidence"],"operation":operation}


def _record_episode_revision(db, row, result, atoms, links, progress, now, *, reason=None):
    """Append an immutable episode snapshot in the claim-install transaction."""
    occurrence_id="occ_"+_hash([row["project"],row["session"],row["turn"]])[:48]
    db.execute("""INSERT INTO episode_occurrences
        (occurrence_id,project,session,turn,created) VALUES(?,?,?,?,?)
        ON CONFLICT DO NOTHING""",
        (occurrence_id,row["project"],row["session"],row["turn"],now))
    context=progress.get("episode_summary_assertions") or {}
    payload={"atoms":atoms,"intent":context.get("intent"),
             "open_work":context.get("open_work") or [],
             "coverage":progress.get("coverage") or {}}
    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",", ":"))
    previous=db.execute("""SELECT revision_id,revision_number,source_hash,payload_json
        FROM episode_revisions WHERE occurrence_id=? AND generation_id=?
        ORDER BY revision_number DESC LIMIT 1""",(occurrence_id,row["generation_id"])).fetchone()
    if previous and previous["source_hash"]==row["source_hash"] and previous["payload_json"]==encoded:
        return previous["revision_id"]
    previous_id=previous["revision_id"] if previous else None
    revision_number=(previous["revision_number"]+1) if previous else 1
    revision_id="erv_"+_hash([occurrence_id,row["generation_id"],revision_number,
                              row["source_hash"],encoded])[:48]
    db.execute("""INSERT INTO episode_revisions
        (revision_id,occurrence_id,generation_id,job_id,revision_number,
         previous_revision_id,source_hash,source_ids,disposition,payload_json,reason,recorded_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (revision_id,occurrence_id,row["generation_id"],row["id"],revision_number,
         previous_id,row["source_hash"],row["source_ids"],
         result["episode_disposition"],encoded,
         reason or ("source_changed" if previous and previous["source_hash"]!=row["source_hash"]
         else "curation_revised" if previous else "initial_curation"),now))
    for atom in atoms:
        for ref in atom["evidence"]:
            db.execute("""INSERT INTO episode_revision_evidence
                (revision_id,atom_key,source_id,segment_id) VALUES(?,?,?,?)
                ON CONFLICT DO NOTHING""",
                (revision_id,atom["atom_key"],ref["source_id"],ref.get("segment_id") or ""))
    for key,item in [("@intent",payload["intent"]),
                     *(("@open_work:"+str(i),entry) for i,entry in enumerate(payload["open_work"]))]:
        if not item:continue
        for ref in item.get("evidence",[]):
            db.execute("""INSERT INTO episode_revision_evidence
                (revision_id,atom_key,source_id,segment_id) VALUES(?,?,?,?)
                ON CONFLICT DO NOTHING""",
                (revision_id,key,ref["source_id"],ref.get("segment_id") or ""))
    for link in links:
        db.execute("""INSERT INTO episode_revision_claim_links
            (revision_id,atom_key,candidate_id,document_id,claim_revision_id,operation,created)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (revision_id,link["atom_key"],link["candidate_id"],link["document_id"],
             link["claim_revision_id"],link["operation"],now))
    return revision_id


def correct_episode_extraction(db, generation, project, session, turn, *,
                               expected_revision_id, atom_key, reviewer, reason):
    """Audit an erroneous extraction without turning it into historical fact.

    The linked claim must already have been corrected or withdrawn in the same
    caller-controlled transaction. The prior episode revision remains for audit;
    ordinary history reads use only the corrected revision.
    """
    if any(not isinstance(value,str) or not value.strip() or len(value)>200
           for value in (expected_revision_id,atom_key,reviewer,reason)):
        raise ValueError("invalid_episode_correction")
    owned_transaction=not db.in_transaction
    initialize(db)
    with (db if True else nullcontext()):
        if owned_transaction:begin_write(db)
        occurrence=db.execute("""SELECT occurrence_id FROM episode_occurrences
            WHERE project=? AND session=? AND turn=?""",(project,session,turn)).fetchone()
        if occurrence is None:raise ValueError("episode_unavailable")
        previous=db.execute("""SELECT * FROM episode_revisions
            WHERE occurrence_id=? AND generation_id=? ORDER BY revision_number DESC LIMIT 1""",
            (occurrence["occurrence_id"],generation)).fetchone()
        if previous is None or previous["revision_id"]!=expected_revision_id:
            raise ValueError("stale_episode_revision")
        payload=json.loads(previous["payload_json"])
        atoms=payload.get("atoms",[])
        if not any(atom.get("atom_key")==atom_key for atom in atoms):
            raise ValueError("episode_atom_unavailable")
        links=[dict(item) for item in db.execute("""SELECT * FROM episode_revision_claim_links
            WHERE revision_id=?""",(previous["revision_id"],)).fetchall()]
        for link in links:
            if link["atom_key"]!=atom_key:continue
            document=db.execute("""SELECT active_revision_id,lifecycle FROM knowledge_documents
                WHERE document_id=? AND project=?""",(link["document_id"],project)).fetchone()
            if document and document["lifecycle"]=="active" and document["active_revision_id"]==link["claim_revision_id"]:
                raise ValueError("episode_claim_correction_required")
        row={"project":project,"session":session,"turn":turn,
             "generation_id":generation,"id":previous["job_id"],
             "source_hash":previous["source_hash"],"source_ids":previous["source_ids"]}
        progress={"episode_summary_assertions":{"intent":payload.get("intent"),
                    "open_work":payload.get("open_work") or []},
                  "coverage":payload.get("coverage") or {}}
        return _record_episode_revision(db,row,
            {"episode_disposition":previous["disposition"]},
            [atom for atom in atoms if atom["atom_key"]!=atom_key],
            [link for link in links if link["atom_key"]!=atom_key],
            progress,time.time(),
            reason="extraction_correction:"+reviewer+":"+reason)


def backfill_episode_revisions(db, generation, *, project=None, limit=100,
                               after_job_id=""):
    """Copy retained, cited episode candidates into the history layer, boundedly.

    Old jobs without surviving original evidence remain gaps. This never calls a
    curator or reconstructs absent actors, motives, event times or source spans.
    """
    if not isinstance(generation,str) or not generation:
        raise ValueError("invalid_episode_generation_id")
    if type(limit) is not int or not 1<=limit<=500 or not isinstance(after_job_id,str):
        raise ValueError("invalid_episode_backfill_page")
    owned_transaction=not db.in_transaction
    initialize(db)
    clause=" AND project=?" if project is not None else ""
    args=[generation,after_job_id,*([project] if project is not None else []),limit]
    jobs=db.execute("""SELECT * FROM curation_episode_jobs WHERE generation_id=?
        AND id>?"""+clause+""" AND status IN ('done','no_learning','withdrawn')
        ORDER BY id LIMIT ?""",args).fetchall()
    made=0;skipped_atoms=0
    with (db if True else nullcontext()):
        if owned_transaction:begin_write(db)
        for job in jobs:
            latest=db.execute("""SELECT status,source_hash FROM curation_episode_jobs
                WHERE id=?""",(job["id"],)).fetchone()
            if (latest is None or latest["source_hash"]!=job["source_hash"]
                    or latest["status"]!=job["status"]):
                continue
            occurrence_id="occ_"+_hash([job["project"],job["session"],job["turn"]])[:48]
            existing=db.execute("""SELECT 1 FROM episode_revisions
                WHERE occurrence_id=? AND generation_id=? LIMIT 1""",
                (occurrence_id,generation)).fetchone()
            if existing:continue
            atoms=[];links=[]
            for candidate_row in db.execute("""SELECT * FROM episode_candidates
                WHERE job_id=? AND generation_id=? AND status='applied'
                ORDER BY created,candidate_id""",(job["id"],generation)).fetchall():
                memory=db.execute("""SELECT body FROM memories WHERE id=? AND active=1
                    AND project=? AND session=?""",
                    (candidate_row["candidate_id"],job["project"],job["session"])).fetchone()
                if memory is None:
                    skipped_atoms+=1;continue
                observation=json.loads(memory["body"])
                evidence=observation.get("evidence") or []
                if not evidence or any(not isinstance(ref,dict) or not isinstance(ref.get("source_id"),str)
                    or not db.execute("""SELECT 1 FROM memories m WHERE m.id=? AND m.active=1
                        AND m.project=? AND m.session=? AND NOT EXISTS
                        (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)
                        AND NOT EXISTS (SELECT 1 FROM memory_exclusions x WHERE x.memory_id=m.id)""",
                        (ref["source_id"],job["project"],job["session"])).fetchone()
                    or (job["project"].startswith("enterprise:") and not db.execute("""SELECT 1
                        FROM enterprise_sources WHERE id=? AND active=1 AND internal_project=?""",
                        (ref["source_id"],job["project"])).fetchone())
                    for ref in evidence):
                    skipped_atoms+=1;continue
                candidate=json.loads(candidate_row["candidate_json"])
                resolution=json.loads(candidate_row["resolution_json"])
                atoms.append(_history_atom(candidate,observation,resolution["operation"]))
                links.append({"atom_key":candidate["candidate_key"],
                    "candidate_id":candidate_row["candidate_id"],
                    "document_id":candidate_row["document_id"],
                    "claim_revision_id":candidate.get("applied_revision_id"),
                    "operation":resolution["operation"]})
            # A missing revision link cannot be represented faithfully.
            linked_keys={item["atom_key"] for item in links if item["claim_revision_id"]}
            atoms=[atom for atom in atoms if atom["atom_key"] in linked_keys]
            links=[item for item in links if item["atom_key"] in linked_keys]
            progress=json.loads(job["progress"] or "{}")
            _record_episode_revision(db,job,
                {"episode_disposition":job["disposition"] or "unknown"},
                atoms,links,progress,time.time(),reason="retained_source_backfill")
            made+=1
    return {"examined":len(jobs),"revisions_created":made,
            "atoms_without_current_evidence":skipped_atoms,
            "next_after_job_id":jobs[-1]["id"] if jobs else after_job_id,
            "has_more":len(jobs)==limit}


def _install(state, config, row, sources, packet, result, resolutions, *, guard=None):
    from agenthub.processing.knowledge import apply_resolved_observation
    from agenthub.processing.metadata import store_metadata
    generation=row["generation_id"];now=time.time()
    current=json.loads((state.home/"config.json").read_text())
    if current.get("paused") or not current.get("observer",{}).get("enabled") or not current.get(
            "episode_curation",{}).get("enabled"):
        raise HarnessError("observer_paused")
    sources_for_job(state.db,row)
    with state.db:
        begin_write(state.db)
        if guard: guard(state.db,row,'before')
        latest_sources=sources_for_job(state.db,row)
        if row['project'].startswith('enterprise:'):
            policies=state.db.execute("SELECT id,policy_version,active,visibility FROM enterprise_sources WHERE id IN ("+
                ",".join("?" for _ in latest_sources)+")",
                [item['id'] for item in latest_sources]).fetchall()
            current_hash=_hash([[item['id'] for item in latest_sources],
                sorted((p['id'],p['policy_version'],p['active'],p['visibility']) for p in policies)])
            if len(policies)!=len(latest_sources) or current_hash!=row['source_hash']:
                raise ValueError('enterprise_source_policy_changed')
            for related in state.db.execute('SELECT related_document_id FROM enterprise_model_inputs WHERE job_id=?',
                                            (row['id'],)):
                document=state.db.execute('SELECT project,lifecycle,active_revision_id FROM knowledge_documents WHERE document_id=?',
                                          (related[0],)).fetchone()
                if not document or document['project']!=row['project'] or document['lifecycle']!='active':
                    raise ValueError('enterprise_resolver_context_changed')
                influences={r[0] for r in state.db.execute('SELECT source_memory_id FROM knowledge_support WHERE revision_id=?',
                                                            (document['active_revision_id'],))}
                influences.update(r[0] for r in state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',
                                                                 (related[0],)))
                if not influences:raise ValueError('enterprise_resolver_context_changed')
                context_sources=state.db.execute('SELECT id,active,internal_project FROM enterprise_sources WHERE id IN ('+
                    ','.join('?' for _ in influences)+')',tuple(influences)).fetchall()
                if len(context_sources)!=len(influences) or any(not item['active'] or item['internal_project']!=row['project']
                    for item in context_sources):
                    raise ValueError('enterprise_resolver_context_changed')
            changed=state.db.execute('''SELECT 1 FROM backend_model_input_snapshots i
                JOIN knowledge_documents d ON d.document_id=i.document_id
                WHERE i.episode_job=? AND (d.active_revision_id!=i.revision_id OR d.lifecycle!='active') LIMIT 1''',(row['id'],)).fetchone()
            if changed:raise ValueError('enterprise_resolver_context_changed')
        if result.get("source_snapshot_hash") and _hash(latest_sources)!=result["source_snapshot_hash"]:
            raise ValueError("episode_source_changed")
        if curator_version(current)!=row["version"] or generation_id(current)!=generation:
            raise ValueError("episode_configuration_changed")
        latest=state.db.execute("SELECT status,source_hash FROM curation_episode_jobs WHERE id=?",
                                (row["id"],)).fetchone()
        if not latest or latest["status"]!="pending" or latest["source_hash"]!=row["source_hash"]:
            raise ValueError("episode_changed")
        for receipt in result["skip_receipts"]:
            state.db.execute("INSERT INTO curation_episode_receipts VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                             (row["id"],receipt["event_id"],receipt["reason"],now))
        history_atoms=[];history_links=[]
        for candidate,resolution in zip(result["candidates"],resolutions):
            operation=resolution["operation"]
            if resolution["target_artifact_id"] and not resolution.get("_expected_revision_id"):
                raise ValueError("stale_resolution_revision")
            identity=[row["id"],row["source_hash"],candidate["candidate_key"],resolution]
            rederivation=json.loads(row['progress'] or '{}').get('source_rederivation')
            if rederivation:identity.append(rederivation['id'])
            candidate_id=_hash(identity)
            observation=_observation(candidate,packet)
            if operation!="IGNORE" or candidate.get("memory_record"):
                history_atoms.append(_history_atom(candidate,observation,operation))
            prior=state.db.execute('SELECT document_id FROM episode_candidates WHERE candidate_id=? AND document_id IS NOT NULL',(candidate_id,)).fetchone()
            if prior:
                if not state.db.execute("SELECT 1 FROM knowledge_documents WHERE document_id=? AND lifecycle='active'",(prior[0],)).fetchone():raise ValueError('prior_resolution_no_longer_active')
                state.db.execute("UPDATE episode_candidates SET status='applied' WHERE candidate_id=?",(candidate_id,))
                continue
            state.db.execute("""INSERT INTO episode_candidate_history SELECT *,? FROM episode_candidates
                WHERE job_id=? AND candidate_key=? AND candidate_id!=? ON CONFLICT DO NOTHING""",(now,row['id'],candidate['candidate_key'],candidate_id))
            if rederivation:
                state.db.execute('DELETE FROM episode_candidates WHERE job_id=? AND candidate_key=? AND candidate_id!=?',
                    (row['id'],candidate['candidate_key'],candidate_id))
            if operation=="IGNORE":
                state.db.execute("""INSERT INTO episode_candidates
                    VALUES(?,?,?,?,?,?,'ignored',NULL,?) ON CONFLICT(candidate_id) DO UPDATE SET job_id=excluded.job_id,generation_id=excluded.generation_id,candidate_key=excluded.candidate_key,candidate_json=excluded.candidate_json,resolution_json=excluded.resolution_json,status=excluded.status,document_id=excluded.document_id,created=excluded.created""",
                    (candidate_id,row["id"],generation,candidate["candidate_key"],
                     json.dumps(candidate,ensure_ascii=False,sort_keys=True),
                     json.dumps(resolution,sort_keys=True),now))
                continue
            body=json.dumps(observation,ensure_ascii=False,sort_keys=True)
            state.db.execute("""INSERT INTO memories
                (id,session,project,body,kind,created,turn) VALUES(?,?,?,?,?,?,?)""",
                (candidate_id,row["session"],row["project"],body,"KnowledgeCandidate",now,row["turn"]))
            state.db.executemany("INSERT INTO observation_sources VALUES(?,?) ON CONFLICT DO NOTHING",
                ((candidate_id,ref["source_id"]) for ref in observation["evidence"]))
            store_metadata(state.db,candidate_id,observation,
                "supported_negative_result" if candidate["knowledge_type"]=="failed_attempt"
                else "durable_decision_or_constraint" if candidate["knowledge_type"] in
                    {"decision","constraint"} else "reusable_finding")
            applied=apply_resolved_observation(state.db,candidate_id,row["project"],row["session"],
                observation,operation,resolution["target_artifact_id"] or None,now,
                expected_revision_id=(resolution.get("_expected_revision_id") or None))
            document_id=applied["document_id"]
            history_links.append({"atom_key":candidate["candidate_key"],
                "candidate_id":candidate_id,"document_id":document_id,
                "claim_revision_id":applied["revision_id"],"operation":operation})
            state.db.execute("INSERT INTO knowledge_generation_documents VALUES(?,?,?) ON CONFLICT DO NOTHING",
                             (generation,document_id,now))
            state.db.execute("""INSERT INTO episode_candidates
                VALUES(?,?,?,?,?,?,'applied',?,?) ON CONFLICT(candidate_id) DO UPDATE SET job_id=excluded.job_id,generation_id=excluded.generation_id,candidate_key=excluded.candidate_key,candidate_json=excluded.candidate_json,resolution_json=excluded.resolution_json,status=excluded.status,document_id=excluded.document_id,created=excluded.created""",
                (candidate_id,row["id"],generation,candidate["candidate_key"],
                 json.dumps({**candidate,"applied_revision_id":applied["revision_id"]},
                            ensure_ascii=False,sort_keys=True),
                 json.dumps(resolution,sort_keys=True),document_id,now))
        saved_progress=state.db.execute("SELECT progress FROM curation_episode_jobs WHERE id=?",
                                        (row["id"],)).fetchone()
        progress=json.loads(saved_progress["progress"] or "{}") if saved_progress else {}
        _record_episode_revision(state.db,row,result,history_atoms,history_links,progress,now)
        status="done" if result["candidates"] else "no_learning"
        state.db.execute("""UPDATE curation_episode_jobs SET status=?,stage='complete',
            disposition=?,model=?,version=?,error=NULL,updated=? WHERE id=?""",
            (status,result["episode_disposition"],config["observer"].get("model","gpt-6-luna"),
             curator_version(config),now,row["id"]))
        state.db.execute("INSERT INTO health VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("episode_curator_success:"+row["project"],str(now)))
        if guard: guard(state.db,row,'after')


def _save_progress(state, job_id, progress, stage):
    with state.db:
        state.db.execute("UPDATE curation_episode_jobs SET progress=?,stage=?,updated=? WHERE id=?",
                         (json.dumps(progress,ensure_ascii=False,sort_keys=True),stage,time.time(),job_id))




def _failed(state, row, exc):
    reason=str(exc) if isinstance(exc,(HarnessError,EpisodeError,ValueError)) else type(exc).__name__
    waiting=reason in {"quota_or_rate_limit","daily_call_budget","installation_call_budget",
                       "observer_paused"}
    attempts=row["attempts"]+(0 if waiting else 1)
    status="withdrawn" if reason=="source_withdrawn" else (
        "held" if attempts>=3 and not waiting else "pending")
    delay=3600 if waiting else min(3600,60*2**max(0,row["attempts"]))
    saved=state.db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',
                           (row['id'],)).fetchone()
    progress=json.loads((saved[0] if saved else row['progress']) or '{}')
    if reason=='stale_resolution_revision':
        # The model saw an older comparison snapshot. Retain validated source
        # stages, but re-run resolution on the next bounded attempt.
        progress.pop('resolutions',None)
    if reason=='memory_all_candidates_rejected':
        # A rejected transport output remains the checkpoint. Automatic retries
        # must not redispatch the same episode or silently sample a new answer.
        # An explicit retry_held_job may clear it after review.
        rejected=progress.get('failed_stage_outputs',[])
        rejected.append({'outputs':progress.get('stage_outputs',[]),
                         'rejections':progress.get('rejections',[])})
        progress['failed_stage_outputs']=rejected[-3:]
    with state.db:
        state.db.execute("""UPDATE curation_episode_jobs SET status=?,attempts=?,error=?,
            next_attempt=?,progress=?,updated=? WHERE id=? AND status='pending'""",
            (status,attempts,reason,time.time()+delay,json.dumps(progress),time.time(),row["id"]))


def allowed(state, config):
    if (config.get("paused") or not config.get("observer",{}).get("enabled")
            or not config.get("episode_curation",{}).get("enabled")):
        return False
    last=state.db.execute("SELECT max(created) FROM model_calls").fetchone()[0] or 0
    return time.time()-last >= config["observer"].get("min_interval_seconds",60)


def run_once(state, config, runner=run_structured, *, project_scope=None,
             job_id=None, context_sources=None, guard=None):
    """Advance one complete episode job, checkpointing after each fresh Luna call."""
    from agenthub.processing.accounting import job_context
    initialize(state.db)
    if not allowed(state,config):return False
    with state.db:
        generation=ensure_generation(state.db,config)
        enterprise=config.get('knowledge_backend',{}).get('mode')=='enterprise_local'
        connections=config.get('backend_worker',{}).get('allowed_connections') if enterprise else None
        create_jobs(state,config,project_scope=project_scope,connection_ids=connections)
        state.db.execute("""UPDATE curation_episode_jobs SET next_attempt=0,error=NULL
            WHERE generation_id=? AND status='pending' AND error IN ('daily_call_budget','installation_call_budget')""",
            (generation,))
    enterprise=config.get('knowledge_backend',{}).get('mode')=='enterprise_local'
    if enterprise and not project_scope:raise ValueError('enterprise_curation_scope_required')
    row=state.db.execute("""SELECT * FROM curation_episode_jobs
        WHERE generation_id=? AND status='pending' AND next_attempt<=?"""+
        (" AND project=?" if enterprise else "")+
        (" AND id=?" if job_id else "")+
        " ORDER BY created,id LIMIT 1",(generation,time.time(),*([project_scope] if enterprise else []),*([job_id] if job_id else []))).fetchone()
    if row is None:return False
    try:
        sources=sources_for_job(state.db,row)
        attribution=job_context("episode_curation",row,sources)
        _run_durable(state, config, row, sources, runner, attribution,
            context_sources=context_sources, guard=guard)
    except Exception as exc:
        _failed(state,row,exc)
    return True


def _run_durable(state, config, row, sources, runner, attribution, *, context_sources=None,guard=None):
    """Fresh complete-turn writer on the existing durable queue and lifecycle.

    Save raw validated-reference output before resolution. Restart revalidates
    original sources, and never treats a prior curator's prose as evidence.
    Long turns use bounded transport stages with a stable full-episode identity.
    Stage output is provisional until every source stage validates; a bounded,
    deterministic reducer merges exact semantic duplicates without inventing
    support from another summary.
    """
    from agenthub.processing.durable_memory import PROMPT, EVIDENCE_GUIDANCE, packets_for, schema, validate_records, validate_episode_summary, observation_for
    from agenthub.processing.evidence_references import EvidenceReferences
    from agenthub.processing.observer import call_model
    settings=config.get("episode_curation",{})
    phase=config.get('_pipeline_phase','full')
    if phase not in {'full','extract','consolidate'}:raise ValueError('invalid_pipeline_phase')
    existing=json.loads(row['progress'] or '{}')
    artifact=existing.get('extraction')
    if artifact is not None and artifact.get('source_snapshot_hash')!=_hash(sources) and phase=='full':
        existing['extraction_history']=(existing.get('extraction_history',[])+[artifact])[-3:]
        artifact=None
    if artifact is not None:
        if (not isinstance(artifact,dict) or artifact.get('version')!=1 or
            artifact.get('sha256')!=_hash({k:v for k,v in artifact.items() if k!='sha256'})):
            raise ValueError('extraction_artifact_changed')
        inputs={v['id']:_hash(v) for v in (context_sources or [])+sources}
        if (artifact.get('source_snapshot_hash')!=_hash(sources) or
                artifact.get('extraction_version')!=row['version'] or
                any(inputs.get(k)!=v for k,v in artifact.get('input_manifest',{}).items())):
            raise ValueError('memory_reference_manifest_changed')
        if guard:
            with state.db:guard(state.db,row,'extracted')
        if phase=='extract':return
        return _consolidate_extraction(state,config,row,sources,artifact,existing,runner,attribution,guard=guard)
    if phase=='consolidate':raise ValueError('extraction_artifact_required')
    stage_review=existing.get('reviewed_stage_limit')
    if stage_review is not None and (not isinstance(stage_review,dict) or
            stage_review.get('source_snapshot_hash')!=_hash(sources)):
        raise ValueError('reviewed_stage_source_changed')
    packets,staged=packets_for(sources,
        max_events=int(settings.get("max_events_per_stage",120)),
        max_chars=int(settings.get("max_chars_per_stage",100_000)),
        max_stages=int(settings.get("max_stages",32)),
        split_oversized_events=settings.get('split_oversized_events',False),
        media_policy=settings.get('media_policy'),
        reviewed_stage_limit=stage_review.get('limit') if stage_review else None)
    for stage_packet in packets:
        stage_packet["episode"]["episode_id"]=row["episode_id"]
    reduce_limit=int(settings.get("max_reducer_chars",128_000))
    if not 32_000 <= reduce_limit <= 1_000_000:
        raise ValueError("invalid_episode_reducer_limit")
    snapshot=_hash(sources)
    progress=json.loads(row["progress"] or "{}")
    if progress.get("source_snapshot_hash") != snapshot:
        progress={"source_snapshot_hash":snapshot,
            'extraction_history':existing.get('extraction_history',[]),
            'capture_diagnostics':progress.get('capture_diagnostics',{}),
            'reviewed_stage_limit':stage_review,
            'reviewed_reprocessing':progress.get('reviewed_reprocessing',{})}
        _save_progress(state,row["id"],progress,"curate")
    outputs=progress.get("stage_outputs",[])
    if len(outputs)>len(packets):
        raise ValueError("memory_stage_checkpoint_invalid")
    records=[];rejections=[];reference_repairs=[];summary_assertions={"intent":None,"open_work":[]}
    summary_rejections=[]
    previous=None
    if context_sources:
        from agenthub.processing.continuous_observer import _whole_turn, _validation_packet
        groups={}
        for source in context_sources: groups.setdefault(source['turn'],[]).append(source)
        for group in groups.values():
            prior_packets,_=packets_for(group,
                max_events=int(settings.get('max_events_per_stage',120)),
                max_chars=int(settings.get('max_chars_per_stage',100_000)),
                max_stages=int(settings.get('max_stages',32)),
                split_oversized_events=settings.get('split_oversized_events',False),
                media_policy=settings.get('media_policy'))
            whole=_whole_turn(prior_packets)
            previous=_validation_packet(whole,previous)
    for index,packet in enumerate(packets):
        current_packet=packet
        current={e["event_id"] for e in current_packet["episode"]["events"]}
        if staged and index:
            current-=set(current_packet["episode"]["objective_event_ids"]+
                         current_packet["episode"]["final_event_ids"])
        if previous:
            from agenthub.processing.continuous_observer import _validation_packet
            packet=_validation_packet(packet,previous)
        refs=EvidenceReferences(packet)
        if index==len(outputs):
            if context_sources is not None:
                from agenthub.processing.continuous_observer import durable_continuation
                payload=durable_continuation(current_packet,previous,refs,
                    reconstruct=config.get('_observer_reconstruct',False),
                    max_context_chars=int(config.get('backend_worker',{}).get('max_context_chars',128_000)))
                if config.get('_observer_reconstruct',False) and index:
                    from agenthub.processing.continuous_observer import durable_stage_context
                    bound=int(config.get('backend_worker',{}).get('max_context_chars',128_000))
                    prior_size=len(json.dumps(payload.get('previous_evidence_index',[]),ensure_ascii=True))
                    payload['observer_earlier_stage_context']=durable_stage_context(
                        packets,index,max_chars=max(0,bound-prior_size))
                instruction='Follow this one source conversation across turns. Preserve earlier reasons and evidence, emit only new or revised records.\n'+PROMPT
            else: payload=refs.packet(packet);instruction=PROMPT
            payload['retention_boundary']={'eligible_current_event_ids':sorted(
                handle for handle,original in refs.events.items() if original in current),
                'not_evidence':True}
            instruction+='\nEvery new record needs an original span from at least one retention_boundary.eligible_current_event_ids event. Repeated anchors and earlier context can support interpretation but cannot alone create another copy of an earlier memory. If this fragment contains no supported new or revised fact, return records=[]; this is successful no-learning.'
            if progress.get('capture_diagnostics',{}).get('completion')=='partial':
                capture=progress['capture_diagnostics']
                payload['source_capture']={'completion':'partial',
                    'gap_events':len(capture.get('gap_source_ids',[])),
                    'missing_expected_events':len(capture.get('missing_expected_events',[])),
                    'not_evidence':True}
                instruction+='\nSource capture is incomplete. Retain only facts supported by the supplied readable original spans. Missing events and image references are not evidence; do not infer unseen visual content, missing actions or a complete session history.'
            if progress.get('reviewed_reprocessing'):
                payload['reviewed_reprocessing']=progress['reviewed_reprocessing']
                instruction+='\n'+REVIEWED_REPROCESSING_GUIDANCE
            if settings.get('evidence_guidance',False):
                instruction += '\n' + EVIDENCE_GUIDANCE
            if settings.get('media_policy'):
                instruction += '\nEncoded media is supplied only as non-citable metadata. You have not viewed or heard its content. Do not infer visual/audio facts from its reference, MIME type, hash, or surrounding JSON. Cite only readable original text spans.'
            retry_cycle=progress.get('explicit_retry_count',0)
            if type(retry_cycle) is not int or not 0<=retry_cycle<=1000:
                raise ValueError('memory_retry_checkpoint_invalid')
            if retry_cycle:
                # A reviewed retry has a distinct request identity. Keep the
                # old response/history, but do not replay the rejected answer.
                reasons=sorted({item['error'] for item in progress.get('rejections',[])
                    if isinstance(item,dict) and isinstance(item.get('error'),str)
                    and re.fullmatch(r'[a-z_]{1,80}',item['error'])})[:12]
                payload['reviewed_retry']={'cycle':retry_cycle,
                    'rejection_reasons':reasons,'not_evidence':True}
                instruction+='\nThis is an explicitly reviewed retry. Correct the listed validation failures using original cited spans only; omit unsupported records. Validation feedback is not evidence.'
                instruction+='\n'+'\n'.join(RETRY_VALIDATION_GUIDANCE[reason]
                    for reason in reasons if reason in RETRY_VALIDATION_GUIDANCE)
            output_schema=schema()
            limit=config.get('backend_worker',{}).get('max_records_per_turn',6)
            if type(limit) is not int or not 1<=limit<=6:raise ValueError('invalid_backend_record_bound')
            output_schema['properties']['records']['maxItems']=limit
            if limit<6:instruction+=f'\nThis bounded processing profile allows at most {limit} new record(s) per turn; select the most durable new fact. Never fill the quota with acknowledgements.'
            raw=call_model(state,config,"durable_memory_curate",instruction,
                payload,output_schema,runner,attribution)
            outputs.append({"output":raw,"reference_manifest":refs.manifest,
                "input_manifest":{s['id']:_hash(s) for s in (context_sources or [])+sources},
                "request_hash":_hash([instruction,payload,output_schema,config.get('observer',{}).get('model')]),
                "schema_hash":_hash(output_schema),"media_policy":settings.get('media_policy')})
            progress["stage_outputs"]=outputs
            _save_progress(state,row["id"],progress,f"curate:{index+1}/{len(packets)}")
        saved=outputs[index]
        try:
            refs=EvidenceReferences(packet,previous=saved.get('reference_manifest'))
            inputs=saved.get('input_manifest')
            current_inputs={s['id']:_hash(s) for s in (context_sources or [])+sources}
            if inputs is not None and (not isinstance(inputs,dict) or
                    any(current_inputs.get(k)!=v for k,v in inputs.items())):
                raise ValueError('reference_manifest_changed')
            if saved.get('reference_manifest') is None:
                raise ValueError('reference_manifest_changed')
        except ValueError as exc:
            raise ValueError("memory_reference_manifest_changed")
        checked=validate_records(saved["output"],packet,current,references=refs)
        records.extend(checked["records"])
        rejections.extend({**item,"stage_index":index} for item in checked["rejections"])
        reference_repairs.extend({**item,"stage_index":index}
                                 for item in checked["reference_repairs"])
        context=validate_episode_summary(saved["output"].get("episode_summary"),packet,
                                         current,references=refs)
        if context["intent"] is not None and summary_assertions["intent"] is None:
            summary_assertions["intent"]=context["intent"]
        summary_assertions["open_work"].extend(context["open_work"])
        summary_rejections.extend({"stage_index":index,"reason":reason}
                                  for reason in context["rejections"])
        if len(json.dumps(records,ensure_ascii=True,separators=(",", ":")))>reduce_limit:
            raise EpisodeError("memory_reducer_exceeds_limit")
    # Reuse the original source packet's spans for installation. The assembled
    # packet is private and never sent to a model as an unbounded context dump.
    packet=dict(packets[0]);episode=dict(packet["episode"])
    from agenthub.processing.continuous_observer import _whole_turn
    events={event['event_id']:event for event in _whole_turn(packets)['episode']['events']}
    if previous: events.update({event['event_id']:event for event in previous['episode']['events']})
    order={source["id"]:index for index,source in enumerate(sorted((context_sources or [])+sources,
        key=lambda source:(source.get('source_order') if type(source.get('source_order')) is int else source["created"],source["id"])))}
    episode["episode_id"]=row["episode_id"]
    episode["events"]=sorted(events.values(),key=lambda event:order[event["event_id"]])
    packet["episode"]=episode
    # Collapse only semantically identical records. Distinct reported and observed
    # outcomes, reasons, actors, locations and dates remain separate assertions.
    unique={}
    for item in records:
        key=_hash({k:item[k] for k in ("title","text","subject","facets","actors",
            "artifact_name","location","reason_actor","reason_quote","state",
            "occurred_date","attribution")})
        if key not in unique:
            unique[key]=item
        else:
            prior=unique[key]
            prior["evidence_span_ids"]=list(dict.fromkeys(
                prior["evidence_span_ids"]+item["evidence_span_ids"]))[:8]
    records=list(unique.values())
    progress["rejections"]=rejections
    progress["reference_repairs"]=reference_repairs
    progress["episode_summary_assertions"]=summary_assertions
    progress["summary_rejections"]=summary_rejections
    progress["coverage"]={"stage_count":len(packets),"processed_stages":len(outputs),
        "source_count":len(sources),"rejected_candidates":len(rejections),
        "reference_repairs":len(reference_repairs),
        "rejected_summary_assertions":len(summary_rejections),
        "completion":"partial" if rejections or summary_rejections else "complete"}
    if progress.get('capture_diagnostics',{}).get('completion')=='partial':
        capture=progress['capture_diagnostics']
        progress['coverage'].update(completion='partial',source_capture={
            'completion':'partial','gap_events':len(capture.get('gap_source_ids',[])),
            'missing_expected_events':len(capture.get('missing_expected_events',[]))})
    _save_progress(state,row["id"],progress,"resolve")
    if rejections and not records:
        raise ValueError("memory_all_candidates_rejected")
    candidates=[];cited=set()
    for item in records:
        obs=observation_for(item,packet)
        cited.update(ref["source_id"] for ref in obs["evidence"])
        candidates.append({"candidate_key":_hash(item),"title":item["title"],
            "claim":item["text"],"knowledge_type":obs["knowledge_type"],
            "subjects":obs["subjects"],"memory_record":item})
    result={"candidates":candidates,"episode_disposition":
        "partial_memory" if rejections else "has_learning" if candidates else "no_durable_learning",
        "skip_receipts":packets[0].get("deterministic_skip_receipts",[])+
            [{"event_id":s["id"],"reason":"not_cited_by_candidate"}
             for s in sources if s["id"] not in cited],
        "source_snapshot_hash":snapshot}
    # Immutable first pass, independent of resolution and derivative indexing.
    # This lives in the same authority as the raw provider returns, not a client corpus.
    artifact={'version':1,'source_snapshot_hash':snapshot,
        'input_manifest':{v['id']:_hash(v) for v in (context_sources or [])+sources},
        'packet':packet,'result':result,'records':records,
        'episode_summary_assertions':summary_assertions,'coverage':progress['coverage'],
        'rejections':rejections,'summary_rejections':summary_rejections,
        'stage_hashes':[_hash(v) for v in outputs],
        'media_policy':settings.get('media_policy'),'extraction_version':row['version']}
    artifact['sha256']=_hash(artifact)
    progress['extraction']=artifact
    _save_progress(state,row['id'],progress,'extraction_ready')
    if guard:
        with state.db:guard(state.db,row,'extracted')
    if config.get('_pipeline_phase')=='extract':return
    _consolidate_extraction(state,config,row,sources,artifact,progress,runner,attribution,guard=guard)


def snapshot_model_inputs(db,job,artifacts):
    """Freeze the exact compared revisions and their sources before dispatch.

    Legacy jobs retain their historical conservative graph. New jobs must never
    acquire dependencies retroactively from a later revision of a compared doc.
    """
    if not artifacts:return
    complete=db.execute('SELECT 1 FROM backend_jobs WHERE episode_job=? AND dependency_snapshot_complete=1',(job,)).fetchone()
    if not complete:return
    records=db.execute('''WITH requested AS MATERIALIZED (
        SELECT * FROM jsonb_to_recordset(?::jsonb) AS r(artifact_id text,revision text)
    ), found AS MATERIALIZED (
        SELECT d.document_id,d.active_revision_id FROM requested r JOIN knowledge_documents d
        ON d.document_id=r.artifact_id AND d.active_revision_id=r.revision AND d.lifecycle='active'
    ), dependencies AS (
        SELECT f.document_id,s.source_memory_id source_id FROM found f
        JOIN knowledge_support s ON s.revision_id=f.active_revision_id
        UNION SELECT f.document_id,x.source_id FROM found f JOIN enterprise_dependencies x ON x.document_id=f.document_id
    ) SELECT f.document_id,f.active_revision_id,jsonb_agg(DISTINCT x.source_id) source_ids
      FROM found f JOIN dependencies x ON x.document_id=f.document_id
      GROUP BY f.document_id,f.active_revision_id''',
      (json.dumps([{'artifact_id':a['artifact_id'],'revision':a['revision']} for a in artifacts]),)).fetchall()
    if len(records)!=len(artifacts):raise ValueError('enterprise_resolver_context_changed')
    db.executemany('''INSERT INTO backend_model_input_snapshots VALUES(?,?,?,?) ON CONFLICT DO NOTHING''',
        [(job,r['document_id'],r['active_revision_id'],json.dumps(r['source_ids'])) for r in records])


def _consolidate_extraction(state,config,row,sources,artifact,progress,runner,attribution,*,guard=None):
    from agenthub.processing.observer import call_model
    packet=artifact['packet'];result=artifact['result'];candidates=result['candidates']
    resolutions=progress.get("resolutions",[])
    for index,candidate in enumerate(candidates):
        # Building a replacement must never rewrite the live/rollback generation.
        compare={**packet,"related_artifacts":related_artifacts(state.db,
            row["generation_id"],row["project"],candidate,include_active=False,
            enterprise_scope=row['project'].startswith('enterprise:'))}
        if progress.get('source_rederivation'):compare['related_artifacts']=[]
        if row['project'].startswith('enterprise:'):
            with state.db:
                state.db.executemany("INSERT INTO enterprise_model_inputs VALUES(?,?) ON CONFLICT DO NOTHING",
                    ((row['id'],item['artifact_id']) for item in compare['related_artifacts']))
                if index>=len(resolutions):snapshot_model_inputs(state.db,row['id'],compare['related_artifacts'])
        from agenthub.processing.episode_curator import MEMORY_RESOLUTION_RULE
        raw=resolutions[index] if index<len(resolutions) else call_model(state,config,"episode_resolve",RESOLUTION_PROMPT+'\n'+MEMORY_RESOLUTION_RULE,
            resolution_payload(candidate,compare),resolution_schema(candidate,compare),runner,attribution)
        checked_resolution=_fenced_resolution(raw,candidate,compare,
            saved=index<len(resolutions))
        if index<len(resolutions):resolutions[index]=checked_resolution
        else:resolutions.append(checked_resolution)
        progress["resolutions"]=resolutions
        _save_progress(state,row["id"],progress,f"resolve:{index+1}/{len(candidates)}")
    _install(state,config,row,sources,packet,result,resolutions,guard=guard)


def generation_status(db, ident=None):
    initialize(db)
    if ident is None:
        row=db.execute("SELECT generation_id FROM knowledge_generations ORDER BY created DESC LIMIT 1").fetchone()
        if row is None:return {"generation_id":None,"status":"absent"}
        ident=row[0]
    generation=db.execute("SELECT * FROM knowledge_generations WHERE generation_id=?",(ident,)).fetchone()
    if generation is None:return {"generation_id":ident,"status":"absent"}
    jobs={row["status"]:row["n"] for row in db.execute("""SELECT status,count(*) n
        FROM curation_episode_jobs WHERE generation_id=? GROUP BY status""",(ident,))}
    candidates={row["status"]:row["n"] for row in db.execute("""SELECT status,count(*) n
        FROM episode_candidates WHERE generation_id=? GROUP BY status""",(ident,))}
    return {"generation_id":ident,"status":generation["status"],"curator_version":generation["curator_version"],
            "jobs":jobs,"candidates":candidates,"documents":db.execute(
                "SELECT count(*) FROM knowledge_generation_documents WHERE generation_id=?",(ident,)).fetchone()[0]}


def _historical_episode_view(db, generation, project, session, turn, *, limit, offset,
                             source_authorizer):
    occurrence=db.execute("""SELECT occurrence_id FROM episode_occurrences
        WHERE project=? AND session=? AND turn=?""",(project,session,turn)).fetchone()
    if occurrence is None:return None
    revision=db.execute("""SELECT * FROM episode_revisions
        WHERE occurrence_id=? AND generation_id=?
        ORDER BY revision_number DESC LIMIT 1""",
        (occurrence["occurrence_id"],generation)).fetchone()
    if revision is None:return None
    payload=json.loads(revision["payload_json"])
    links={row["atom_key"]:row for row in db.execute("""SELECT * FROM episode_revision_claim_links
        WHERE revision_id=?""",(revision["revision_id"],)).fetchall()}
    eligible_ids=set();source_missing=False

    def eligible(ref, atom_key):
        nonlocal source_missing
        source_id=ref.get("source_id")
        segment_id=ref.get("segment_id") or ""
        if not isinstance(source_id,str) or not source_id:
            source_missing=True;return False
        cited=db.execute("""SELECT 1 FROM episode_revision_evidence
            WHERE revision_id=? AND atom_key=? AND source_id=? AND segment_id=?""",
            (revision["revision_id"],atom_key,source_id,segment_id)).fetchone()
        source=db.execute("""SELECT m.id,m.active,
            EXISTS(SELECT 1 FROM historical_sources h WHERE h.source_id=m.id) AS retired
            FROM memories m WHERE m.id=? AND m.project=? AND m.session=?
            AND NOT EXISTS (SELECT 1 FROM memory_exclusions x WHERE x.memory_id=m.id)""",
            (source_id,project,session)).fetchone()
        historical_document = bool(project.startswith("enterprise:") and
            source_authorizer is not None and table_exists(db,"backend_document_versions") and
            db.execute("SELECT 1 FROM backend_document_versions WHERE source_id=?",
                       (source_id,)).fetchone())
        if (cited is None or source is None or
                ((not source["active"] or source["retired"]) and not historical_document)):
            source_missing=True;return False
        if project.startswith("enterprise:"):
            policy=db.execute("""SELECT active,internal_project FROM enterprise_sources
                WHERE id=?""",(source_id,)).fetchone()
            if (not policy or (not policy["active"] and not historical_document) or
                    policy["internal_project"]!=project):
                source_missing=True;return False
            # A scoped Hub caller must supply its present-day reader check.
            if source_authorizer is None:
                source_missing=True;return False
        if source_authorizer is not None and not source_authorizer(source_id):
            source_missing=True;return False
        eligible_ids.add(source_id)
        return True

    assertions=[]
    for atom in payload.get("atoms",[]):
        key=atom.get("atom_key")
        evidence=atom.get("evidence") or []
        if not isinstance(key,str) or not evidence or not all(eligible(ref,key) for ref in evidence):
            continue
        record=atom["record"]
        link=links.get(key)
        claim_status="no_generalized_claim"
        if link:
            document=db.execute("""SELECT lifecycle,active_revision_id FROM knowledge_documents
                WHERE document_id=? AND project=?""",(link["document_id"],project)).fetchone()
            if document is None or document["lifecycle"]!="active":
                claim_status="inactive_claim"
            elif document["active_revision_id"]==link["claim_revision_id"]:
                claim_status="current_claim"
            else:
                successor=db.execute("""SELECT reason FROM knowledge_revisions
                    WHERE document_id=? AND previous_revision_id=?""",
                    (link["document_id"],link["claim_revision_id"])).fetchone()
                claim_status=("corrected_claim" if successor and successor["reason"]=="resolver_correct"
                              else "superseded_claim")
        assertions.append({"candidate_id":link["candidate_id"] if link else key,
            "atom_key":key,"document_id":link["document_id"] if link else None,
            "revision_id":link["claim_revision_id"] if link else None,
            "episode_revision_id":revision["revision_id"],
            "title":record.get("title",""),"text":record.get("text",""),
            "subject":record.get("subject",""),"facets":record.get("facets",[]),
            "actors":record.get("actors",[]),"state":record.get("state","reported"),
            "attribution":record.get("attribution","unknown"),
            "artifact":{"name":record.get("artifact_name"),"location":record.get("location")}
                if record.get("artifact_name") else None,
            "rationale":{"actor":record.get("reason_actor"),"quote":record.get("reason_quote")}
                if record.get("reason_quote") else None,
            "occurred_date":record.get("occurred_date") or None,
            "claim_status":claim_status,"operation":atom.get("operation"),
            "evidence":evidence})
    def eligible_context(item,key):
        return item if item and item.get("evidence") and all(
            eligible(ref,key) for ref in item["evidence"]) else None
    intent=eligible_context(payload.get("intent"),"@intent")
    open_work=[entry for i,item in enumerate(payload.get("open_work",[]))
               if (entry:=eligible_context(item,"@open_work:"+str(i))) is not None]
    if not assertions and not open_work:return None
    coverage=payload.get("coverage") or {}
    gaps=[]
    if source_missing:gaps.append("source_withdrawn_or_revised")
    if coverage.get("rejected_candidates"):gaps.append("candidate_rejected")
    if coverage.get("rejected_summary_assertions"):gaps.append("summary_assertion_rejected")
    if coverage.get("processed_stages",1)!=coverage.get("stage_count",1):
        gaps.append("stage_incomplete")
    if coverage.get('source_capture',{}).get('completion')=='partial':
        gaps.append('incomplete_source_capture')
    source_times=[row[0] for row in db.execute("SELECT created FROM memories WHERE id IN ("+
        ",".join("?" for _ in eligible_ids)+")",sorted(eligible_ids))] if eligible_ids else []
    page=assertions[offset:offset+limit]
    parts=[];used=0
    if intent:
        parts.append("Goal: "+intent["text"]);used=len(parts[0])
    included=0
    for item in page:
        part=(item["title"]+" ["+item["state"]+"; "+item["claim_status"]+"]: "+
              item["text"])
        if parts and used+len(part)+1>2400:break
        parts.append(part);used+=len(part)+(1 if used else 0);included+=1
    groups={"actions":[],"decisions":[],"results":[],"artifacts":[],
            "attempted_steps":[],"open_work":[]}
    for item in page:
        key=item["candidate_id"]
        if "activity" in item["facets"] or "procedure" in item["facets"]:
            groups["actions"].append(key)
        if "decision" in item["facets"] or item["rationale"]:groups["decisions"].append(key)
        if item["state"] in {"observed","reported"}:groups["results"].append(key)
        if item["artifact"]:groups["artifacts"].append(key)
        if item["state"]=="attempted":groups["attempted_steps"].append(key)
    return {"episode_id":occurrence["occurrence_id"],
        "handle":"ep_"+occurrence["occurrence_id"][4:16],
        "occurrence_id":occurrence["occurrence_id"],
        "curated_revision_id":revision["revision_id"],
        "previous_curated_revision_id":revision["previous_revision_id"],
        "summary_revision":_hash([revision["revision_id"],
            [item["atom_key"] for item in assertions],intent,open_work,gaps]),
        "generation_id":generation,"project":project,"session":session,
        "source_turn":turn,"source_range":{"captured_at_start":min(source_times) if source_times else None,
            "captured_at_end":max(source_times) if source_times else None,
            "basis":"capture_time_not_event_time"},
        "completion":"partial" if gaps else "complete","coverage_gaps":gaps,
        "stage_count":coverage.get("stage_count",1),
        "processed_stages":coverage.get("processed_stages",1),
        "summary":" ".join(parts),"summary_has_more":included<len(page),
        "intent":intent,"intent_status":"cited_original_source" if intent else "not_explicitly_extracted",
        "open_work":open_work,"open_work_status":"cited_original_source" if open_work else "not_determined_from_attempts",
        "claim_groups":groups,"assertions":page,"offset":offset,
        "has_more":offset+limit<len(assertions),"total_assertions":len(assertions),
        "derived_context":True,"independent_support":False}


def get_episode_view(db, generation, project, session, turn, *, limit=8, offset=0,
                     include_building=False, mode="current", source_authorizer=None):
    """Read a scoped derived episode, checking every original citation at read time.

    The returned assertions are curated records with links to their original
    source spans. This view is not indexed as an independent corroborating source.
    A changed document revision or withdrawn source removes its dependent atom.
    """
    if (type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int
            or not 0 <= offset <= 1000):
        raise ValueError("invalid_episode_view_page")
    if mode not in {"current","history"}:
        raise ValueError("invalid_episode_view_mode")
    initialize(db)
    generation_row=db.execute("SELECT status FROM knowledge_generations WHERE generation_id=?",
                              (generation,)).fetchone()
    if generation_row is None or (generation_row["status"]!="active" and not include_building):
        return None
    if mode=="history":
        return _historical_episode_view(db,generation,project,session,turn,
            limit=limit,offset=offset,source_authorizer=source_authorizer)
    job=db.execute("""SELECT * FROM curation_episode_jobs WHERE generation_id=?
        AND project=? AND session=? AND turn=?""",(generation,project,session,turn)).fetchone()
    if job is None or job["status"] not in {"done","withdrawn"}:
        return None
    rows=db.execute("""SELECT c.candidate_id,c.candidate_json,c.document_id,m.body,
            d.active_revision_id
        FROM episode_candidates c JOIN memories m ON m.id=c.candidate_id
        JOIN knowledge_documents d ON d.document_id=c.document_id
        JOIN knowledge_generation_documents gd ON gd.document_id=d.document_id
        WHERE c.job_id=? AND c.generation_id=? AND gd.generation_id=?
          AND c.status='applied' AND m.active=1 AND d.lifecycle='active'
          AND d.project=? AND d.active_revision_id IS NOT NULL
        ORDER BY c.created,c.candidate_id""",(job["id"],generation,generation,project)).fetchall()
    assertions=[];withdrawn_support=False
    for row in rows:
        candidate=json.loads(row["candidate_json"])
        if candidate.get("applied_revision_id")!=row["active_revision_id"]:
            continue
        observation=json.loads(row["body"])
        evidence=observation.get("evidence",[])
        if not evidence:
            continue
        eligible=True
        for ref in evidence:
            source=db.execute("""SELECT m.id FROM memories m WHERE m.id=? AND m.active=1
                AND m.project=? AND m.session=? AND NOT EXISTS
                (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)""",
                (ref["source_id"],project,session)).fetchone()
            support=db.execute("""SELECT 1 FROM knowledge_support WHERE revision_id=?
                AND source_memory_id=? AND source_segment_id=? AND relation='supports'""",
                (row["active_revision_id"],ref["source_id"],ref["segment_id"])).fetchone()
            if source is None or support is None:
                eligible=False;withdrawn_support=True;break
        if not eligible:
            continue
        record=candidate.get("memory_record")
        if not isinstance(record,dict):
            continue
        assertions.append({"candidate_id":row["candidate_id"],
            "document_id":row["document_id"],"revision_id":row["active_revision_id"],
            "title":record["title"],"text":record["text"],
            "subject":record["subject"],"facets":record["facets"],
            "actors":record["actors"],"state":record["state"],
            "attribution":record.get("attribution","unknown"),
            "artifact":{"name":record["artifact_name"],"location":record["location"]}
                if record["artifact_name"] else None,
            "rationale":{"actor":record["reason_actor"],"quote":record["reason_quote"]}
                if record["reason_quote"] else None,
            "occurred_date":record["occurred_date"] or None,
            "evidence":evidence})
    if not assertions:
        return None
    progress=json.loads(job["progress"] or "{}")
    coverage=progress.get("coverage",{})
    gaps=[]
    if job["status"]=="withdrawn" or withdrawn_support:
        gaps.append("source_withdrawn_or_revised")
    if coverage.get("rejected_candidates"):
        gaps.append("candidate_rejected")
    if coverage.get("rejected_summary_assertions"):
        gaps.append("summary_assertion_rejected")
    if coverage.get("processed_stages",1)!=coverage.get("stage_count",1):
        gaps.append("stage_incomplete")
    if coverage.get('source_capture',{}).get('completion')=='partial':
        gaps.append('incomplete_source_capture')
    context=progress.get("episode_summary_assertions",{})
    context_withdrawn=False
    def eligible_context(item):
        nonlocal context_withdrawn
        for ref in item.get("evidence",[]):
            source=db.execute("""SELECT 1 FROM memories m WHERE m.id=? AND m.active=1
                AND m.project=? AND m.session=? AND NOT EXISTS
                (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)""",
                (ref["source_id"],project,session)).fetchone()
            if source is None:
                context_withdrawn=True
                return False
        return True
    intent=context.get("intent")
    if intent is not None and not eligible_context(intent):
        intent=None
    open_work=[item for item in context.get("open_work",[]) if eligible_context(item)]
    if context_withdrawn and "source_withdrawn_or_revised" not in gaps:
        gaps.append("source_withdrawn_or_revised")
    source_ids=json.loads(job["source_ids"])
    times=[r[0] for r in db.execute("SELECT created FROM memories WHERE id IN ("+
        ",".join("?" for _ in source_ids)+")",source_ids)] if source_ids else []
    page=assertions[offset:offset+limit]
    parts=[];used=0
    if intent:
        goal="Goal: "+intent["text"]
        parts.append(goal);used=len(goal)
    for item in page:
        part=item["title"]+": "+item["text"]
        if parts and used+len(part)+1>2400:
            break
        parts.append(part);used+=len(part)+(1 if used else 0)
    included_records=len(parts)-(1 if intent else 0)
    summary=" ".join(parts)
    narrative_more=included_records<len(page)
    groups={"actions":[],"decisions":[],"results":[],"artifacts":[],
            "attempted_steps":[],"open_work":[]}
    for item in page:
        ident=item["candidate_id"]
        if "activity" in item["facets"] or "procedure" in item["facets"]:
            groups["actions"].append(ident)
        if "decision" in item["facets"] or item["rationale"]:
            groups["decisions"].append(ident)
        if item["state"] in {"observed","reported"}:
            groups["results"].append(ident)
        if item["artifact"]:
            groups["artifacts"].append(ident)
        if item["state"]=="attempted":
            groups["attempted_steps"].append(ident)
    revision=_hash([job["episode_id"],job["source_hash"],
                    [(item["candidate_id"],item["revision_id"]) for item in assertions],
                    intent,open_work,gaps])
    return {"episode_id":job["episode_id"],"handle":"ep_"+job["episode_id"][:12],
        "summary_revision":revision,
        "generation_id":generation,"project":project,"session":session,
        "source_turn":turn,"source_range":{"captured_at_start":min(times) if times else None,
            "captured_at_end":max(times) if times else None,
            "basis":"capture_time_not_event_time"},
        "completion":"partial" if gaps else "complete","coverage_gaps":gaps,
        "stage_count":coverage.get("stage_count",1),"processed_stages":coverage.get("processed_stages",1),
        "summary":summary,"summary_has_more":narrative_more,
        "intent":intent,
        "intent_status":"cited_original_source" if intent else "not_explicitly_extracted",
        "open_work":open_work,
        "open_work_status":"cited_original_source" if open_work else "not_determined_from_attempts",
        "claim_groups":groups,"assertions":page,"offset":offset,
        "has_more":offset+limit<len(assertions),
        "total_assertions":len(assertions),"derived_context":True,
        "independent_support":False}


def session_overview(db, generation, project, session, *, limit=8, offset=0,
                     include_building=False, mode="current", source_authorizer=None):
    """Bounded overview of eligible episode revisions, without model calls."""
    if (type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int
            or not 0 <= offset <= 1000):
        raise ValueError("invalid_session_overview_page")
    if mode not in {"current","history"}:
        raise ValueError("invalid_episode_view_mode")
    initialize(db)
    jobs=db.execute("""SELECT turn FROM curation_episode_jobs
        WHERE generation_id=? AND project=? AND session=? AND status IN ('done','withdrawn'"""+
        (",'no_learning'" if mode=="history" else "")+""" )
        ORDER BY created,id LIMIT 1001""",(generation,project,session)).fetchall()
    views=[]
    for job in jobs[:1000]:
        view=get_episode_view(db,generation,project,session,job["turn"],limit=8,
                              include_building=include_building,mode=mode,
                              source_authorizer=source_authorizer)
        if view is not None:
            titles=[item["title"] for item in view["assertions"]]
            compact=[];size=0
            for title in titles:
                if compact and size+len(title)+2>1200:
                    break
                compact.append(title);size+=len(title)+(2 if size else 0)
            views.append({"episode_id":view["episode_id"],"handle":view["handle"],
                "summary_revision":view["summary_revision"],"source_turn":view["source_turn"],
                **({"occurrence_id":view["occurrence_id"],
                    "curated_revision_id":view["curated_revision_id"]} if mode=="history" else {}),
                "source_range":view["source_range"],"completion":view["completion"],
                "coverage_gaps":view["coverage_gaps"],"summary":"; ".join(compact),
                "has_more_assertions":view["has_more"] or len(compact)<len(titles)})
    views.sort(key=lambda v:(v["source_range"]["captured_at_start"] or 0,v["source_turn"]))
    return {"generation_id":generation,"project":project,"session":session,
        "episodes":views[offset:offset+limit],"offset":offset,
        "has_more":offset+limit<len(views) or len(jobs)>1000,
        "coverage_gaps":["session_episode_limit"] if len(jobs)>1000 else [],
        "derived_context":True,"independent_support":False}


def episode_links_for_document(db, generation, project, document_id, *, limit=8,
                               offset=0, include_building=False, mode="current",
                               source_authorizer=None):
    """Find source episodes through a document's current original evidence.

    A consolidated document can be supported by several tasks. The document's
    owner_session alone is not a complete expansion key. These links include only
    currently eligible original sources; missing episode views remain explicit.
    """
    if (type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int
            or not 0 <= offset <= 1000):
        raise ValueError("invalid_episode_link_page")
    if mode not in {"current","history"}:
        raise ValueError("invalid_episode_view_mode")
    initialize(db)
    status=db.execute("SELECT status FROM knowledge_generations WHERE generation_id=?",
                      (generation,)).fetchone()
    if status is None or (status["status"]!="active" and not include_building):
        return {"document_id":document_id,"episodes":[],"has_more":False,
                "coverage_gaps":["generation_unavailable"]}
    if mode=="history":
        rows=db.execute("""SELECT DISTINCT o.session,o.turn FROM episode_revision_claim_links l
            JOIN episode_revisions r ON r.revision_id=l.revision_id
            JOIN episode_occurrences o ON o.occurrence_id=r.occurrence_id
            WHERE l.document_id=? AND r.generation_id=? AND o.project=?
              AND r.revision_number=(SELECT max(latest.revision_number)
                  FROM episode_revisions latest WHERE latest.occurrence_id=r.occurrence_id
                    AND latest.generation_id=r.generation_id)
            ORDER BY o.session,o.turn LIMIT 1001""",
            (document_id,generation,project)).fetchall()
        entries=[]
        for row in rows[:1000]:
            view=get_episode_view(db,generation,project,row["session"],row["turn"],
                limit=20,include_building=include_building,mode="history",
                source_authorizer=source_authorizer)
            if view is None or not any(item["document_id"]==document_id
                                       for item in view["assertions"]):continue
            entries.append({"episode_id":view["episode_id"],"handle":view["handle"],
                "occurrence_id":view["occurrence_id"],"session":row["session"],
                "source_turn":row["turn"],"summary_revision":view["summary_revision"],
                "summary":view["summary"],"completion":view["completion"],
                "coverage_gaps":view["coverage_gaps"],"source_range":view["source_range"],
                "known_event_day":None,"order_basis":"capture_time_only_not_event_order"})
        entries=_order_episode_links(entries)
        return {"document_id":document_id,"generation_id":generation,
            "project":project,"episodes":entries[offset:offset+limit],"offset":offset,
            "has_more":offset+limit<len(entries) or len(rows)>1000,
            "coverage_gaps":["document_episode_limit"] if len(rows)>1000 else [],
            "derived_context":True,"independent_support":False}
    document=db.execute("""SELECT d.active_revision_id FROM knowledge_documents d
        JOIN knowledge_generation_documents gd ON gd.document_id=d.document_id
        WHERE d.document_id=? AND d.project=? AND d.lifecycle='active'
          AND d.active_revision_id IS NOT NULL AND gd.generation_id=?""",
        (document_id,project,generation)).fetchone()
    if document is None:
        return {"document_id":document_id,"episodes":[],"has_more":False,
                "coverage_gaps":["document_unavailable"]}
    # PostgreSQL requires DISTINCT ordering columns in the projection. created
    # is fixed by memory id, so including it preserves source deduplication.
    supports=db.execute("""SELECT DISTINCT m.session,m.turn,m.id,m.created
        FROM knowledge_support s JOIN memories m ON m.id=s.source_memory_id
        WHERE s.revision_id=? AND s.relation='supports' AND m.project=?
          AND m.active=1 AND NOT EXISTS
          (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)
        ORDER BY m.created,m.id""",(document["active_revision_id"],project)).fetchall()
    by_episode={}
    for source in supports:
        job=db.execute("""SELECT episode_id FROM curation_episode_jobs
            WHERE generation_id=? AND project=? AND session=? AND turn=?
              AND status IN ('done','withdrawn')""",
            (generation,project,source["session"],source["turn"])).fetchone()
        if job is None:
            continue
        key=(source["session"],source["turn"])
        entry=by_episode.setdefault(key,{"episode_id":job["episode_id"],
            "handle":"ep_"+job["episode_id"][:12],"session":source["session"],
            "source_turn":source["turn"],"supporting_source_ids":[]})
        entry["supporting_source_ids"].append(source["id"])
    entries=[]
    for (source_session,turn),entry in by_episode.items():
        view=get_episode_view(db,generation,project,source_session,turn,
                              limit=20,include_building=include_building)
        event_days=({item['occurred_date'] for item in view['assertions']
                     if item.get('occurred_date')} if view else set())
        known_event_day=(next(iter(event_days)) if len(event_days)==1 and view
                         and not view['has_more'] and all(item.get('occurred_date')
                         for item in view['assertions']) else None)
        entry.update(summary_revision=view["summary_revision"] if view else None,
                     summary=view["summary"] if view else None,
                     completion=view["completion"] if view else "unavailable",
                     coverage_gaps=view["coverage_gaps"] if view else ["summary_unavailable"],
                     source_range=view["source_range"] if view else None,
                     known_event_day=known_event_day,
                     order_basis='explicit_event_day' if known_event_day else
                                 'capture_time_only_not_event_order')
        entries.append(entry)
    entries=_order_episode_links(entries)
    return {"document_id":document_id,"revision_id":document["active_revision_id"],
        "generation_id":generation,"project":project,"episodes":entries[offset:offset+limit],
        "offset":offset,"has_more":offset+limit<len(entries),"coverage_gaps":[],
        "derived_context":True,"independent_support":False}


def _order_episode_links(entries):
    """Order source-stated event days; keep unknown-time episodes separate."""
    return sorted(entries,key=lambda item:(0 if item.get('known_event_day') else 1,
        item.get('known_event_day') or '',
        (item.get('source_range') or {}).get('captured_at_start') or 0,
        item.get('session',''),item.get('source_turn','')))


def retry_held_job(db, job_id, *, discard_invalid_stages=False,
                   expected_progress_sha256=None, reviewed_errors=()):
    """Grant one reviewed retry; optionally archive a proven-invalid checkpoint.

    Discard requires the exact reviewed checkpoint and bounded validator codes.
    Current source authorization remains the backend recovery/dispatch guard's job.
    """
    initialize(db)
    row=db.execute("SELECT status,error,progress FROM curation_episode_jobs WHERE id=?",(job_id,)).fetchone()
    if row is None:raise ValueError("episode_job_not_found")
    if row["status"]!="held":raise ValueError("episode_job_not_held")
    progress=json.loads(row['progress'] or '{}')
    if type(discard_invalid_stages) is not bool:
        raise ValueError('reviewed_stage_discard_flag')
    if discard_invalid_stages:
        if (expected_progress_sha256!=hashlib.sha256(row['progress'].encode()).hexdigest()
                or row['error'] not in {'LockNotAvailable','harness_timeout',
                    'worker_retry_bound','observer_usage_checkpoint_missing',
                    'memory_reference_manifest_changed','memory_all_candidates_rejected'}
                or not progress.get('stage_outputs')
                or not isinstance(reviewed_errors,(list,tuple))
                or not 1<=len(reviewed_errors)<=12
                or any(not isinstance(code,str) or not re.fullmatch(r'[a-z_]{1,80}',code)
                       for code in reviewed_errors)):
            raise ValueError('reviewed_stage_checkpoint_invalid')
        archived=progress.get('failed_stage_outputs',[])
        archived.append({'outputs':progress['stage_outputs'],
            'rejections':progress.get('rejections',[]),
            'reviewed_errors':sorted(set(reviewed_errors))})
        progress['failed_stage_outputs']=archived[-3:]
        progress['rejections']=list(progress.get('rejections',[]))+[
            {'record_index':-1,'error':code} for code in sorted(set(reviewed_errors))]
    elif expected_progress_sha256 is not None or reviewed_errors:
        raise ValueError('reviewed_stage_discard_required')
    progress['explicit_retry_count']=int(progress.get('explicit_retry_count',0))+1
    if row['error']=='memory_all_candidates_rejected' or discard_invalid_stages:
        progress.pop('stage_outputs',None)
        progress.pop('resolutions',None)
        progress.pop('coverage',None)
        if progress.get('extraction'):
            progress['extraction_history']=(progress.get('extraction_history',[])+[progress.pop('extraction')])[-3:]
    with db:
        db.execute("""UPDATE curation_episode_jobs SET status='pending',attempts=0,
            next_attempt=0,error=NULL,progress=?,updated=? WHERE id=? AND status='held' AND progress=?""",
            (json.dumps(progress),time.time(),job_id,row['progress']))
        if db.execute('SELECT progress FROM curation_episode_jobs WHERE id=?',(job_id,)).fetchone()[0]!=json.dumps(progress):
            raise ValueError('reviewed_stage_checkpoint_invalid')
    return {"job_id":job_id,"status":"pending"}


def skip_held_job(db, job_id):
    """Record an explicitly reviewed processing gap without admitting knowledge."""
    initialize(db)
    row=db.execute("SELECT status,source_ids,error FROM curation_episode_jobs WHERE id=?",
                   (job_id,)).fetchone()
    if row is None:raise ValueError("episode_job_not_found")
    if row["status"]!="held":raise ValueError("episode_job_not_held")
    now=time.time();source_ids=json.loads(row["source_ids"])
    with db:
        db.executemany("INSERT INTO curation_episode_receipts VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
            ((job_id,source_id,"insufficient_context",now) for source_id in source_ids))
        db.execute("""UPDATE curation_episode_jobs SET status='unprocessed',stage='complete',
            disposition=?,updated=? WHERE id=?""",
            ("explicit_gap:"+(row["error"] or "unknown"),now,job_id))
    return {"job_id":job_id,"status":"unprocessed","error":row["error"]}


def activate_generation(db, ident):
    """Atomically make a fully drained generation the sole retrieval corpus."""
    return _activate_generation(db, ident, rollback=False)


def rollback_generation(db, ident):
    """Restore a previously activated generation without undoing withdrawals."""
    return _activate_generation(db, ident, rollback=True)


def _activate_generation(db, ident, *, rollback):
    initialize(db)
    generation=db.execute("SELECT * FROM knowledge_generations WHERE generation_id=?",(ident,)).fetchone()
    if generation is None:raise ValueError("generation_not_found")
    if rollback:
        if generation["status"] != "retired" or generation["activated"] is None:
            raise ValueError("generation_not_rollback_target")
    elif generation["status"] not in {"building","active"}:
        raise ValueError("generation_not_activatable")
    total=db.execute("SELECT count(*) FROM curation_episode_jobs WHERE generation_id=?",(ident,)).fetchone()[0]
    pending=db.execute("""SELECT count(*) FROM curation_episode_jobs
        WHERE generation_id=? AND status NOT IN ('done','no_learning','withdrawn','unprocessed')""",(ident,)).fetchone()[0]
    if not total or pending:raise ValueError("generation_not_drained")
    invalid=db.execute("""SELECT 1 FROM knowledge_generation_documents kgd
        LEFT JOIN knowledge_documents d ON d.document_id=kgd.document_id
        LEFT JOIN knowledge_index_rows i ON i.document_id=d.document_id
        WHERE kgd.generation_id=? AND (d.document_id IS NULL
          OR (d.lifecycle!='active' AND ?=0)
          OR (d.lifecycle='active' AND (d.active_revision_id IS NULL
            OR i.document_id IS NULL OR i.revision_id!=d.active_revision_id))) LIMIT 1""",
        (ident,int(rollback))).fetchone()
    if invalid:raise ValueError("generation_index_not_ready")
    now=time.time()
    with db:
        db.execute("""UPDATE knowledge_generations SET status='retired',retired=?
            WHERE status='active' AND generation_id!=?""",(now,ident))
        db.execute("""UPDATE knowledge_generations SET status='active',activated=?,retired=NULL
            WHERE generation_id=?""",(now,ident))
        db.execute("INSERT INTO knowledge_generation_state VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET active_generation_id=excluded.active_generation_id",(ident,))
    return generation_status(db,ident)
