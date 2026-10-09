"""Evidence-linked temporal assertions over private knowledge revisions.

All intervals are half-open UTC seconds. An absent endpoint is *unknown* unless
``to_status`` is explicitly ``ongoing``. Recording time is admission/audit time,
not a substitute for the source event or the period during which a fact was true.
This module does not expose historical revisions without current scope, evidence,
document lifecycle, and generation checks.
"""
from __future__ import annotations

from agenthub.processing.storage import table_exists

from datetime import date, datetime, time as day_time, timedelta, timezone
import hashlib
import json
import math
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
_PRECISIONS = {"year", "month", "day", "instant"}
_BASES = {"explicit_source", "source_event_time", "inferred", "unknown"}


def initialize(db):
    db.require_schema()


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("invalid_temporal_timezone") from None


def _point(raw, *, precision, timezone_name, basis):
    if not isinstance(raw, str) or not raw or len(raw) > 40:
        raise ValueError("invalid_temporal_point")
    if precision not in _PRECISIONS or basis not in _BASES:
        raise ValueError("invalid_temporal_qualification")
    zone = _zone(timezone_name)
    if precision == "year" and re.fullmatch(r"\d{4}", raw):
        first = datetime(int(raw), 1, 1, tzinfo=zone)
        after = first.replace(year=first.year + 1)
    elif precision == "month" and re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            year, month = map(int, raw.split("-"))
            first = datetime(year, month, 1, tzinfo=zone)
            after = (datetime(year + 1, 1, 1, tzinfo=zone) if month == 12
                     else datetime(year, month + 1, 1, tzinfo=zone))
        except ValueError:
            raise ValueError("invalid_temporal_point") from None
    elif precision == "day" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            raise ValueError("invalid_temporal_point") from None
        first = datetime.combine(day, day_time.min, zone)
        after = datetime.combine(day + timedelta(days=1), day_time.min, zone)
    elif precision == "instant":
        try:
            first = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid_temporal_point") from None
        if first.tzinfo is None or first.utcoffset() is None:
            raise ValueError("temporal_instant_requires_offset")
        after = first
    else:
        raise ValueError("temporal_precision_mismatch")
    return first.timestamp(), after.timestamp()


def _qualification(value, *, event):
    if value is None:
        return (None, None, None, None, None, None) if event else (
            None, None, None, None, "unknown", None, None, None)
    if not isinstance(value, dict):
        raise ValueError("invalid_temporal_qualification")
    precision = value.get("precision")
    zone = value.get("timezone")
    basis = value.get("basis")
    if event:
        at = value.get("at")
        if set(value) != {"at", "precision", "timezone", "basis"}:
            raise ValueError("invalid_event_fields")
        if zone is None and precision == "day" and basis == "explicit_source":
            try:
                date.fromisoformat(at)
            except (TypeError, ValueError):
                raise ValueError("invalid_temporal_point") from None
            if not isinstance(at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", at):
                raise ValueError("invalid_temporal_point")
            # A source-stated calendar date has no implicit UTC offset.
            return at, None, None, precision, None, basis
        start, end = _point(at, precision=precision, timezone_name=zone, basis=basis)
        return at, start, end, precision, zone, basis
    if set(value) - {"from", "to", "to_status", "precision", "timezone", "basis"}:
        raise ValueError("invalid_validity_fields")
    lower = value.get("from")
    upper = value.get("to")
    status = value.get("to_status", "bounded" if upper else "unknown")
    if status not in {"unknown", "ongoing", "bounded"} or bool(upper) != (status == "bounded"):
        raise ValueError("invalid_valid_to_status")
    if lower is None and upper is None:
        if status != "unknown":
            raise ValueError("unknown_validity_cannot_be_ongoing")
        return None, None, None, None, "unknown", None, None, None
    if precision not in _PRECISIONS or basis not in _BASES:
        raise ValueError("invalid_temporal_qualification")
    _zone(zone)
    start = (_point(lower, precision=precision, timezone_name=zone, basis=basis)[0]
             if lower is not None else None)
    end = (_point(upper, precision=precision, timezone_name=zone, basis=basis)[0]
           if upper is not None else None)
    if start is not None and end is not None and start >= end:
        raise ValueError("invalid_validity_interval")
    return lower, start, upper, end, status, precision, zone, basis


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("invalid_temporal_identity")
    return " ".join(value.casefold().split())


def _encode(value, *, max_chars=2000):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("invalid_temporal_json") from None
    if len(encoded) > max_chars:
        raise ValueError("temporal_value_too_long")
    return encoded


def source_event_seconds(db, source):
    """Enterprise source time is original time; receipt time is never substituted."""
    if table_exists(db,'enterprise_sources'):
        row=db.execute('SELECT occurred_at FROM enterprise_sources WHERE id=?',(source['id'] if 'id' in source.keys() else source['source_memory_id'],)).fetchone()
        if row:
            raw=row[0]
            try:
                instant=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
                if instant.tzinfo is None:return None
                return instant.timestamp()
            except (ValueError,TypeError,OverflowError):
                try:return float(raw) if math.isfinite(float(raw)) else None
                except (ValueError,TypeError):return None
    return source['created']


def _native_evidence_text(db,revision_id,source_id):
    """Exact registered native passage, never unrelated display/source prose."""
    if not (table_exists(db,'backend_native_artifacts') and table_exists(db,'backend_native_spans')):return ''
    row=db.execute('''SELECT r.claim_json,p.start,p."end" FROM knowledge_revisions r
        JOIN backend_native_artifacts n ON n.document_id=r.document_id AND n.source_id=?
        JOIN backend_native_spans p ON p.document_id=r.document_id AND p.source_id=n.source_id
        WHERE r.revision_id=? AND n.artifact_kind='native_document' ''',(source_id,revision_id)).fetchone()
    if not row:return ''
    lesson=json.loads(row['claim_json']).get('lesson','')
    return lesson if isinstance(lesson,str) and row['end']-row['start']==len(lesson) else ''


def record_assertion(db, *, revision_id, subject, predicate, value, actor=None,
                     qualifiers=None, event=None, validity=None,
                     evidence_source_ids=None, change=None, recorded_at=None,
                     retained_source_authorizer=None):
    """Admit one assertion whose evidence already supports the named revision.

    ``change`` is a reviewed relation to a prior assertion. ``supersedes`` means
    the prior value was once true; ``corrects`` means it was wrong even historically.
    An unreviewed relation is retained for audit but cannot choose an answer.
    """
    initialize(db)
    revision = db.execute("""SELECT r.document_id,d.project,d.lifecycle,d.active_revision_id
        FROM knowledge_revisions r JOIN knowledge_documents d ON d.document_id=r.document_id
        WHERE r.revision_id=?""", (revision_id,)).fetchone()
    if revision is None or revision["lifecycle"] != "active" or revision["active_revision_id"] != revision_id:
        raise ValueError("temporal_revision_unavailable")
    subject_key = _key(subject)
    predicate_key = _key(predicate)
    actor_key = _key(actor) if actor is not None else ""
    qualifiers_json = _encode(qualifiers or {}, max_chars=1000)
    if not isinstance(qualifiers or {}, dict):
        raise ValueError("invalid_temporal_qualifiers")
    value_json = _encode(value)
    event_fields = _qualification(event, event=True)
    validity_fields = _qualification(validity, event=False)
    sources = sorted(set(evidence_source_ids or []))
    if not sources or len(sources) > 40 or any(not isinstance(item, str) for item in sources):
        raise ValueError("temporal_evidence_required")
    placeholders = ",".join("?" for _ in sources)
    found = db.execute("""SELECT DISTINCT s.source_memory_id,m.body,m.created,m.kind,m.exit_code FROM knowledge_support s
        JOIN memories m ON m.id=s.source_memory_id
        WHERE s.revision_id=? AND s.source_memory_id IN (""" + placeholders + ")"
        """ AND (m.active=1 OR ?=1) AND m.project=?
        AND NOT EXISTS (SELECT 1 FROM memory_exclusions x WHERE x.memory_id=m.id)
        AND NOT EXISTS (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)""",
        [revision_id, *sources, int(retained_source_authorizer is not None),revision["project"]]).fetchall()
    if {row["source_memory_id"] for row in found} != set(sources):
        raise ValueError("temporal_evidence_unavailable")
    if retained_source_authorizer and any(not retained_source_authorizer(row['source_memory_id']) or
            not _native_evidence_text(db,revision_id,row['source_memory_id']) for row in found):
        raise ValueError('temporal_retained_native_evidence_unavailable')
    for qualification, field_names in ((event, ("at",)), (validity, ("from", "to"))):
        if not qualification:
            continue
        basis = qualification.get("basis")
        points = [qualification.get(field) for field in field_names if qualification.get(field)]
        if basis == "explicit_source" and any(not any(
                point in row["body"] or point in _native_evidence_text(db,revision_id,row['source_memory_id'])
                for row in found) for point in points):
            raise ValueError("temporal_time_not_in_source")
        if basis == "source_event_time":
            for point in points:
                seconds = _point(point, precision=qualification["precision"],
                    timezone_name=qualification["timezone"], basis=basis)[0]
                if qualification["precision"] != "instant" or not any(
                        source_event_seconds(db,row) is not None and
                        (row["kind"] == "PostToolUse" or revision['project'].startswith('enterprise:')) and
                        abs(source_event_seconds(db,row) - seconds) < 0.001 for row in found):
                    raise ValueError("temporal_source_event_mismatch")
    if validity and validity.get("to_status") == "ongoing" and validity.get("basis") == "explicit_source":
        if not any(re.search(r"\b(?:ongoing|currently|still)\b", row["body"], re.I) or
                   re.search(r'\b(?:from|since|as of|effective from)\s+\d{4}-\d{2}-\d{2}\b',
                             _native_evidence_text(db,revision_id,row['source_memory_id']),re.I)
                   for row in found):
            raise ValueError("temporal_ongoing_not_in_source")
    now = time.time() if recorded_at is None else float(recorded_at)
    if not math.isfinite(now):
        raise ValueError("invalid_recorded_time")
    stable = _encode([revision_id, subject_key, predicate_key, actor_key,
                      qualifiers_json, value_json, event_fields, validity_fields], max_chars=10000)
    assertion_id = "ta_" + hashlib.sha256(stable.encode()).hexdigest()[:48]
    if change is not None:
        if not isinstance(change, dict) or set(change) != {"relation", "assertion_id", "reviewed"}:
            raise ValueError("invalid_temporal_change")
        if change["relation"] not in {"supersedes", "corrects", "conflicts"} or type(change["reviewed"]) is not bool:
            raise ValueError("invalid_temporal_change")
        old = db.execute("SELECT * FROM knowledge_temporal_assertions WHERE assertion_id=?",
                         (change["assertion_id"],)).fetchone()
        if old is None or old["project"] != revision["project"] or old["subject_key"] != subject_key or old["predicate_key"] != predicate_key or old["actor_key"] != actor_key or old["qualifiers_json"] != qualifiers_json:
            raise ValueError("temporal_change_scope_mismatch")
        if change["relation"] == "supersedes" and validity_fields[1] is None:
            raise ValueError("supersession_requires_valid_from")
    db.execute("SAVEPOINT agentclient_temporal_record")
    try:
        db.execute("""INSERT INTO knowledge_temporal_assertions VALUES(
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (assertion_id, revision["document_id"], revision_id, revision["project"],
             subject, subject_key, predicate, predicate_key, actor, actor_key,
             qualifiers_json, value_json, *event_fields, *validity_fields, now, SCHEMA_VERSION))
        for source in sources:
            db.execute("INSERT INTO knowledge_temporal_evidence VALUES(?,?) ON CONFLICT DO NOTHING", (assertion_id, source))
        if change is not None:
            db.execute("INSERT INTO knowledge_temporal_relations VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                (assertion_id, change["assertion_id"], change["relation"], int(change["reviewed"]), now))
    except BaseException:
        db.execute("ROLLBACK TO agentclient_temporal_record")
        db.execute("RELEASE agentclient_temporal_record")
        raise
    else:
        db.execute("RELEASE agentclient_temporal_record")
    return {"assertion_id": assertion_id, "document_id": revision["document_id"],
            "revision_id": revision_id, "recorded_at": now}


def _native_validity(prose):
    explicit=re.search(r'\b(?:from|since|as of|effective(?: from)?)\s+(\d{4}-\d{2}-\d{2})\b',prose,re.I)
    if not explicit:return None
    try:lower=date.fromisoformat(explicit[1])
    except ValueError:return None
    validity={'from':explicit[1],'to_status':'ongoing','precision':'day','timezone':'UTC','basis':'explicit_source'}
    remainder=prose[explicit.end():]
    upper=re.search(r'\b(?:until|before)\s+(\d{4}-\d{2}-\d{2})\b',remainder,re.I)
    if upper:
        try:end=date.fromisoformat(upper[1])
        except ValueError:return None
        if end<=lower:return None
        validity.update(to=upper[1],to_status='bounded')
    elif re.search(r'\b(?:until|through|before)\b',remainder,re.I):
        # Inclusive calendar ends and unspecified end conditions are not an
        # explicit half-open timestamp. Preserve the fact with unknown validity.
        validity['to_status']='unknown'
    return validity


def admit_native_assertion(db,revision_id,source_id,*,subject,retained_source_authorizer=None):
    """Deterministic explicit validity over a faithful original-document passage.

    Revision capture never supplies validity. An ordered source revision with an
    explicit later effective date supersedes the earlier interval in the same
    source lineage. Unknown validity remains unknown and cannot answer an as-of.
    """
    text=_native_evidence_text(db,revision_id,source_id)
    if not text or not table_exists(db,'backend_document_versions'):return {'status':'not_applicable'}
    prose='\n'.join(line for line in text.splitlines() if line.strip() and not re.match(r'^\s*#{1,6}\s',line)).strip()
    if not prose:return {'status':'not_applicable'}
    source=db.execute('SELECT connection,external_id,version FROM backend_document_versions WHERE source_id=?',(source_id,)).fetchone()
    if not source:return {'status':'not_applicable'}
    validity=_native_validity(prose)
    predicate='location' if re.search(r'\b(?:stored|saved|located|archive|directory|path)\b',prose,re.I) and re.search(r'[/\\]',prose) else 'fact'
    old=None
    if validity:
        older=db.execute('''SELECT a.* FROM knowledge_temporal_assertions a
            JOIN knowledge_temporal_evidence e ON e.assertion_id=a.assertion_id
            JOIN backend_document_versions v ON v.source_id=e.source_memory_id
            WHERE v.connection=? AND v.external_id=? AND v.source_id!=?
            AND a.subject_key=? AND a.predicate_key=? AND a.actor_key=''
            AND a.valid_basis='explicit_source' AND a.valid_from_utc IS NOT NULL
            ORDER BY a.valid_from_utc DESC,a.recorded_at DESC''',
            (source['connection'],source['external_id'],source_id,_key(subject),predicate)).fetchall()
        lower=_point(validity['from'],precision='day',timezone_name='UTC',basis='explicit_source')[0]
        old=next((row for row in older if row['valid_from_utc']<lower),None)
    return record_assertion(db,revision_id=revision_id,subject=subject,predicate=predicate,value=prose,
        qualifiers={'origin':'native_document','connection':source['connection'],'external_id':source['external_id']},
        evidence_source_ids=[source_id],validity=validity,
        retained_source_authorizer=retained_source_authorizer,
        change={'relation':'supersedes','assertion_id':old['assertion_id'],'reviewed':True} if old else None)


def admit_curated_assertion(db, revision_id, observation, *, operation=None,
                            previous_revision_id=None):
    """Admit typed durable-memory facts using their already checked source links.

    The curator validates cited dates and execution outcomes before creating the
    observation. This adapter checks the source again for time and location. It
    does not infer an event date from capture of a user/agent report, nor infer an
    ongoing validity interval from a completed save.
    """
    context = observation.get("memory_context") if isinstance(observation, dict) else None
    if not isinstance(context, dict) or not re.fullmatch(
            r"durable-memory-\d+", str(context.get("policy", ""))):
        return {"status": "not_applicable", "assertions": []}
    refs = observation.get("evidence") or []
    sources = sorted({ref.get("source_id") for ref in refs
                      if isinstance(ref, dict) and isinstance(ref.get("source_id"), str)})
    if not sources:
        raise ValueError("temporal_evidence_required")
    document = db.execute("""SELECT d.project FROM knowledge_revisions r
        JOIN knowledge_documents d ON d.document_id=r.document_id
        WHERE r.revision_id=?""", (revision_id,)).fetchone()
    if document is None:
        raise ValueError("temporal_revision_unavailable")
    placeholders = ",".join("?" for _ in sources)
    source_rows = {row["id"]: row for row in db.execute(
        "SELECT id,project,kind,body,created,exit_code FROM memories WHERE id IN (" + placeholders + ")",
        sources)}
    project = document["project"]
    anchor = source_rows.get(context.get("event_id"))
    location = context.get("location")
    original_time=source_event_seconds(db,anchor) if anchor is not None else None
    source_event_anchor = bool(anchor is not None and
        anchor["project"] == project and anchor["kind"] == "PostToolUse" and
        type(context.get("source_created")) in (int, float) and
        math.isfinite(context["source_created"]) and
        original_time is not None and abs(original_time - context["source_created"]) < 0.001)
    tool_anchor = bool(source_event_anchor and context.get("state") == "observed" and
        context.get("attribution") == "execution_result" and anchor["exit_code"] == 0 and
        isinstance(location, str) and bool(location) and location in anchor["body"])
    explicit_day = context.get("occurred_date")
    if isinstance(explicit_day, str) and explicit_day:
        # A date in memory_context is usable only when the original cited source
        # also contains it. The source may be a prompt or a successful tool event.
        if not any(row["project"] == project and explicit_day in row["body"]
                   for row in source_rows.values()):
            raise ValueError("temporal_date_not_in_source")
        event = {"at": explicit_day, "precision": "day", "timezone": None,
                 "basis": "explicit_source"}
    elif source_event_anchor and context.get("time_basis") == "source_event_time":
        event = {"at": datetime.fromtimestamp(original_time, timezone.utc).isoformat(),
                 "precision": "instant", "timezone": "UTC", "basis": "source_event_time"}
    else:
        event = None
    actors = context.get("actors")
    actor = actors[0] if isinstance(actors, list) and len(actors) == 1 and isinstance(actors[0], str) else None
    candidates = []
    artifact = context.get("artifact_name")
    if isinstance(artifact, str) and artifact and isinstance(location, str) and location:
        if context.get("state") != "attempted":
            validity = ({"from": event["at"], "to_status": "unknown",
                         "precision": "instant", "timezone": "UTC",
                         "basis": "source_event_time"}
                        if tool_anchor and event and event["precision"] == "instant" else None)
            if (project.startswith('enterprise:') and anchor is not None and original_time is not None
                    and context.get('state')=='reported' and anchor['kind']=='UserPromptSubmit'
                    and location in anchor['body'] and re.search(r'\b(?:now|current|currently|still)\b',anchor['body'],re.I)):
                validity={'from':datetime.fromtimestamp(original_time,timezone.utc).isoformat(),
                    'to_status':'ongoing','precision':'instant','timezone':'UTC','basis':'source_event_time'}
            candidates.append((artifact, "location", location, actor, validity))
    reason = context.get("reason_quote")
    subject = context.get("subject")
    if isinstance(reason, str) and reason and isinstance(subject, str) and subject:
        candidates.append((subject, "reason", reason, context.get("reason_actor") or actor, None))
    facets = context.get("facets") or []
    if isinstance(subject, str) and subject and isinstance(facets, list):
        if "activity" in facets:
            candidates.append((subject, "activity", observation.get("lesson") or observation.get("title"), actor, None))
        elif "procedure" in facets:
            candidates.append((subject, "procedure", observation.get("lesson") or observation.get("title"), actor, None))
        elif "fact" in facets and not candidates:
            candidates.append((subject, "fact", observation.get("lesson") or observation.get("title"), actor, None))
    admitted = []
    for item_subject, predicate, value, item_actor, validity in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        old = None
        if operation == "CORRECT" and previous_revision_id:
            old = db.execute("""SELECT * FROM knowledge_temporal_assertions
                WHERE revision_id=? AND subject_key=? AND predicate_key=? AND actor_key=?
                  AND qualifiers_json='{}' ORDER BY recorded_at DESC LIMIT 1""",
                (previous_revision_id, _key(item_subject), _key(predicate),
                 _key(item_actor) if item_actor else "")).fetchone()
            if old and old["value_json"] == _encode(value):
                old = None
            relation = "corrects"
        else:
            relation = "supersedes"
            if predicate == "location" and tool_anchor and validity:
                prior = db.execute("""SELECT * FROM knowledge_temporal_assertions
                    WHERE project=? AND subject_key=? AND predicate_key='location'
                      AND actor_key=? AND qualifiers_json='{}' AND revision_id!=?
                    ORDER BY event_start_utc DESC,recorded_at DESC LIMIT 20""",
                    (project, _key(item_subject), _key(item_actor) if item_actor else "",
                     revision_id)).fetchall()
                for candidate in prior:
                    prior_value = json.loads(candidate["value_json"])
                    if (isinstance(prior_value, str) and prior_value != value and
                            prior_value in anchor["body"] and
                            re.search(r"\b(?:moved?|renamed?|relocated?)\b|\bmv\s+", anchor["body"], re.I)):
                        old = candidate
                        break
        change = ({"relation": relation, "assertion_id": old["assertion_id"],
                   "reviewed": True} if old else None)
        result = record_assertion(db, revision_id=revision_id, subject=item_subject,
            predicate=predicate, value=value, actor=item_actor, event=event,
            validity=validity, evidence_source_ids=sources, change=change)
        admitted.append(result["assertion_id"])
    return {"status": "admitted" if admitted else "no_typed_assertion",
            "assertions": admitted}


def _as_of_seconds(value, *, now):
    if value is None:
        clock = datetime.now(timezone.utc) if now is None else now
    elif isinstance(value, datetime):
        clock = value
    elif isinstance(value, str):
        try:
            clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid_as_of_time") from None
    elif type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError("invalid_as_of_time")
        return float(value)
    else:
        raise ValueError("invalid_as_of_time")
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("as_of_requires_offset")
    return clock.timestamp()


def _eligible_evidence(db, assertion_id, revision_id, project,source_authorizer=None):
    rows = db.execute("""SELECT DISTINCT e.source_memory_id,s.source_segment_id
        FROM knowledge_temporal_evidence e
        JOIN knowledge_support s ON s.revision_id=? AND s.source_memory_id=e.source_memory_id
        JOIN memories m ON m.id=e.source_memory_id AND m.project=?
        WHERE e.assertion_id=?
          AND NOT EXISTS (SELECT 1 FROM memory_exclusions x WHERE x.memory_id=m.id)
          AND NOT EXISTS (SELECT 1 FROM historical_sources h WHERE h.source_id=m.id)
          AND (m.active=1 OR ?=1)
        ORDER BY e.source_memory_id,s.source_segment_id""",
        (revision_id, project, assertion_id,int(source_authorizer is not None))).fetchall()
    # Inactive evidence is eligible only when the backend proves an authorized
    # retained native revision under the current head, policy and denial journal.
    if source_authorizer:rows=[row for row in rows if source_authorizer(row['source_memory_id'])]
    return [{"source_id": row["source_memory_id"], "segment_id": row["source_segment_id"]}
            for row in rows]


def _relation_state(db, assertion_id, generation, known_cutoff=None):
    generation_clause = ("""AND EXISTS (SELECT 1 FROM knowledge_generation_documents g
        WHERE g.document_id=n.document_id AND g.generation_id=?)""" if generation else
        "AND NOT EXISTS (SELECT 1 FROM knowledge_generation_documents g WHERE g.document_id=n.document_id)")
    args = [assertion_id, generation] if generation else [assertion_id]
    known_clause = ''
    if known_cutoff is not None:
        known_clause = ' AND rel.recorded_at<=?'
        args.append(known_cutoff)
    return db.execute("""SELECT rel.relation,n.valid_from_utc
        FROM knowledge_temporal_relations rel
        JOIN knowledge_temporal_assertions n ON n.assertion_id=rel.new_assertion_id
        JOIN knowledge_documents d ON d.document_id=n.document_id
        WHERE rel.old_assertion_id=? AND rel.reviewed=1
        """ + generation_clause + known_clause + " ORDER BY rel.recorded_at", args).fetchall()


def _time_status(row, instant, relations):
    if any(rel["relation"] == "corrects" for rel in relations):
        return "excluded"
    start = row["valid_from_utc"]
    end = row["valid_to_utc"]
    supersession = [rel["valid_from_utc"] for rel in relations
                    if rel["relation"] == "supersedes" and rel["valid_from_utc"] is not None]
    if supersession:
        cutoff = min(supersession)
        end = min(end, cutoff) if end is not None else cutoff
    if start is not None and instant < start:
        return "excluded"
    if end is not None and instant >= end:
        return "excluded"
    if row["valid_basis"] not in {"explicit_source", "source_event_time"}:
        return "unknown"
    if start is None or row["valid_to_status"] == "unknown":
        return "unknown"
    return "valid"


def select_assertions(db, project, *, read_projects=None, subject=None,
                      predicate=None, actor=None, qualifiers=None, as_of=None,
                      known_at=None, now=None, event_day=None, timezone_name="UTC", limit=100,
                      authorized_document_ids=None, source_authorizer=None):
    """Select eligible assertion values; report conflicts and unknown intervals.

    A direct call never widens read scope. ``event_day`` selects source event time;
    ``as_of`` selects a fact's validity. Both may be supplied only when a caller
    deliberately wants the intersection.
    """
    scopes = sorted(set(read_projects)) if read_projects is not None else [project]
    if project not in scopes:
        raise ValueError("read_scope_missing_receiver")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid_temporal_limit")
    instant = _as_of_seconds(as_of, now=now)
    known_cutoff = _as_of_seconds(known_at, now=now) if known_at is not None else None
    clauses = [("d.lifecycle IN ('active','superseded')" if known_cutoff is not None
                else "d.lifecycle='active'"),
               "a.project IN (" + ",".join("?" for _ in scopes) + ")"]
    args = list(scopes)
    if known_cutoff is not None:
        clauses.append('a.recorded_at<=?')
        args.append(known_cutoff)
    if authorized_document_ids is not None:
        permitted=sorted(set(authorized_document_ids))
        if permitted:
            clauses.append('a.document_id IN ('+','.join('?' for _ in permitted)+')')
            args.extend(permitted)
        else:clauses.append('1=0')
    if subject is not None:
        clauses.append("a.subject_key=?")
        args.append(_key(subject))
    if predicate is not None:
        clauses.append("a.predicate_key=?")
        args.append(_key(predicate))
    if actor is not None:
        clauses.append("a.actor_key=?")
        args.append(_key(actor))
    if qualifiers is not None:
        clauses.append("a.qualifiers_json=?")
        args.append(_encode(qualifiers, max_chars=1000))
    active = db.execute("SELECT active_generation_id FROM knowledge_generation_state WHERE singleton=1").fetchone()
    generation = active[0] if active else None
    if generation and authorized_document_ids is None:
        clauses.append("""EXISTS (SELECT 1 FROM knowledge_generation_documents g
            WHERE g.document_id=d.document_id AND g.generation_id=?)""")
        args.append(generation)
    elif authorized_document_ids is None:
        clauses.append("""NOT EXISTS (SELECT 1 FROM knowledge_generation_documents g
            WHERE g.document_id=d.document_id)""")
    if event_day is not None:
        try:
            day = date.fromisoformat(event_day)
        except (TypeError, ValueError):
            raise ValueError("invalid_event_day") from None
        zone = _zone(timezone_name)
        start = datetime.combine(day, day_time.min, zone).timestamp()
        end = datetime.combine(day + timedelta(days=1), day_time.min, zone).timestamp()
        clauses.append("""((a.event_start_utc IS NOT NULL AND a.event_start_utc<?
            AND a.event_end_utc>?) OR (a.event_start_utc IS NULL AND a.event_precision='day'
            AND a.event_at=? AND a.event_basis='explicit_source'))""")
        args.extend([end, start, day.isoformat()])
    query = """SELECT a.*,d.active_revision_id FROM knowledge_temporal_assertions a
        JOIN knowledge_documents d ON d.document_id=a.document_id
        JOIN knowledge_revisions r ON r.revision_id=a.revision_id AND r.document_id=d.document_id
        WHERE """ + " AND ".join(clauses) + " ORDER BY a.recorded_at DESC,a.assertion_id LIMIT ?"
    args.append(limit + 1)
    accepted = []
    unknown_time = []
    raw_rows = db.execute(query, args).fetchall()
    truncated = len(raw_rows) > limit
    for row in raw_rows[:limit]:
        evidence = _eligible_evidence(db, row["assertion_id"], row["revision_id"], row["project"],source_authorizer)
        if not evidence:
            continue
        if source_authorizer and not all(source_authorizer(ref['source_id']) for ref in evidence):
            continue
        relations = _relation_state(db, row["assertion_id"], generation, known_cutoff)
        # An unlinked inactive revision might have been corrected away. Reviewed
        # supersession is the only path to its earlier truth interval.
        if row["active_revision_id"] != row["revision_id"] and not any(
                rel["relation"] == "supersedes" for rel in relations):
            if known_cutoff is None:
                continue
            active = (db.execute('SELECT created FROM knowledge_revisions WHERE revision_id=?',
                                 (row['active_revision_id'],)).fetchone()
                      if row['active_revision_id'] else None)
            if active is not None and active['created'] <= known_cutoff:
                continue
        status = (("valid" if row["event_basis"] in {"explicit_source", "source_event_time"}
                   else "unknown") if event_day is not None and as_of is None
                  else _time_status(row, instant, relations))
        if any(rel["relation"] == "corrects" for rel in relations):
            status = "excluded"
        if status == "excluded":
            continue
        item = {"assertion_id": row["assertion_id"], "document_id": row["document_id"],
                "revision_id": row["revision_id"], "project": row["project"],
                "subject": row["subject"], "predicate": row["predicate"],
                "actor": row["actor"], "qualifiers": json.loads(row["qualifiers_json"]),
                "value": json.loads(row["value_json"]), "event_at": row["event_at"],
                "event_precision": row["event_precision"],
                "event_timezone": row["event_timezone"], "event_basis": row["event_basis"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "valid_to_status": row["valid_to_status"],
                "valid_precision": row["valid_precision"],
                "valid_timezone": row["valid_timezone"],
                "valid_basis": row["valid_basis"], "recorded_at": row["recorded_at"],
                "evidence": evidence, "time_status": status}
        (accepted if status == "valid" else unknown_time).append(item)
    grouped = {}
    for item in [*accepted, *unknown_time]:
        key = (item["project"], _key(item["subject"]), _key(item["predicate"]),
               _key(item["actor"]) if item["actor"] else "", _encode(item["qualifiers"], max_chars=1000))
        group = grouped.setdefault(key, {"project": item["project"],
            "subject": item["subject"], "predicate": item["predicate"],
            "actor": item["actor"], "qualifiers": item["qualifiers"],
            "assertions": [], "unknown_time": []})
        group["assertions" if item["time_status"] == "valid" else "unknown_time"].append(item)
    groups = []
    for group in grouped.values():
        values = {_encode(item["value"]) for item in group["assertions"]}
        group["status"] = ("conflict" if len(values) > 1 else "uncertain"
                           if group["unknown_time"] else "supported")
        groups.append(group)
    result_status = ("uncertain" if truncated else "multiple" if len(groups) > 1
                     else groups[0]["status"] if groups else "absent")
    return {"status": result_status, "assertions": accepted,
            "unknown_time": unknown_time, "groups": groups,
            "as_of_utc": instant, "known_at_utc": known_cutoff,
            "truncated": truncated}


def parse_time_intent(query, *, reference_clock, timezone_name):
    """Conservative date intent for current, explicit day, yesterday, last Monday.

    The day interval is in the named local zone; ``as_of_utc`` is the last instant
    of that day for state questions. Activity queries use the day window instead.
    Unsupported relative periods are marked ambiguous for clarification/abstention.
    """
    if not isinstance(query, str) or not isinstance(reference_clock, datetime):
        raise ValueError("invalid_temporal_query")
    if reference_clock.tzinfo is None or reference_clock.utcoffset() is None:
        raise ValueError("reference_clock_requires_offset")
    zone = _zone(timezone_name)
    local_today = reference_clock.astimezone(zone).date()
    lower = query.casefold()
    explicit = re.findall(r"\b(?:as of|on|at)\s+(\d{4}-\d{2}-\d{2})\b", lower)
    bare = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", lower)
    relative = [token for token in ("yesterday", "last monday") if token in lower]
    if len(explicit) > 1 or len(relative) > 1 or (explicit and relative) or (bare and not explicit):
        return {"kind": "ambiguous", "reason": "multiple_or_unqualified_dates"}
    if explicit:
        try:
            day = date.fromisoformat(explicit[0])
        except ValueError:
            return {"kind": "ambiguous", "reason": "invalid_date"}
    elif "yesterday" in relative:
        day = local_today - timedelta(days=1)
    elif "last monday" in relative:
        day = local_today - timedelta(days=(local_today.weekday() or 7))
    else:
        if re.search(r"\b(last|previous|ago|before|after|during|between)\b", lower):
            return {"kind": "ambiguous", "reason": "unsupported_relative_time"}
        return {"kind": "current", "as_of_utc": reference_clock.timestamp(),
                "timezone": timezone_name}
    start = datetime.combine(day, day_time.min, zone).timestamp()
    end = datetime.combine(day + timedelta(days=1), day_time.min, zone).timestamp()
    return {"kind": "as_of", "day": day.isoformat(), "timezone": timezone_name,
            "day_start_utc": start, "day_end_utc": end,
            # Keep a representable instant inside the day. A one-ULP decrement
            # can round back to next midnight when converted to microseconds.
            "as_of_utc": end - 0.001}
