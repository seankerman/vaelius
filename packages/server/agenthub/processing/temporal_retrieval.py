"""Question-facing, scope-checked cards for versioned temporal assertions."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from agenthub.processing.knowledge import active_generation_id
from agenthub.processing.cards import compact_card
from agenthub.processing.temporal import parse_time_intent, select_assertions


_CURRENT = re.compile(r"\b(?:now|current|currently|today)\b", re.I)
_LOCATION = re.compile(r"\b(?:where|location|located|saved|stored|put)\b", re.I)
_ACTIVITY = re.compile(r"\b(?:what\s+did|worked?\s+on|do\s+on|happened|completed?)\b", re.I)
_WORDS = re.compile(r"[a-z0-9]+", re.I)
_TIME_EXPRESSION = re.compile(
    r"\b(?:as\s+of|yesterday|today|now|current(?:ly)?|\d{4}-\d{2}-\d{2}|"
    r"last\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"week|month|year|spring|summer|fall|autumn|winter))\b", re.I)
_AMBIGUOUS_TIME = re.compile(r"\b(?:before|after|during|between|ago|previous)\b",re.I)


def _words(text):
    return set(_WORDS.findall(text.casefold()))


def _question_kind(query):
    if _ACTIVITY.search(query):
        return "activity"
    if _LOCATION.search(query):
        return "location"
    if re.search(r'\b(?:what|which|how much|how many)\b',query,re.I):
        return 'fact'
    return None


def _clock(value):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reference_clock_requires_offset")
    return value


def _qualifier(item):
    if item["event_at"]:
        zone = item["event_timezone"] or "timezone unstated"
        return f"Event: {item['event_at']} ({item['event_precision']}; {zone}; {item['event_basis']})."
    if item["valid_from"]:
        zone = item["valid_timezone"] or "timezone unstated"
        return f"Valid from: {item['valid_from']} ({item['valid_precision']}; {zone}; {item['valid_basis']})."
    return "Time of event or validity was not established."


def _reference(item, *, kind, as_of_utc=None, known_at_utc=None,
               event_day=None, timezone_name="UTC", requested_day=None, current=False):
    value = item["value"] if isinstance(item["value"], str) else json.dumps(
        item["value"], ensure_ascii=False, sort_keys=True)
    if kind == "activity":
        text = f"{item['subject']}: {value}. {_qualifier(item)}"
    else:
        instant = (datetime.fromtimestamp(as_of_utc, timezone.utc).isoformat()
                   if as_of_utc is not None else None)
        noun='location' if kind=='location' else 'fact'
        if current:
            lead = f"Latest supported {noun} for {item['subject']} as of {instant} UTC"
        elif requested_day:
            lead = f"{item['subject']} {noun} as of {requested_day} ({timezone_name})"
        else:
            lead = f"{item['subject']} {noun} as of {instant} UTC"
        known = (f"Known by {datetime.fromtimestamp(known_at_utc, timezone.utc).isoformat()} UTC: "
                 if known_at_utc is not None else "")
        text = f"{known}{lead}: {value}. {_qualifier(item)}"
    if item["actor"]:
        text += f" Actor: {item['actor']}."
    text += f" Original evidence: {len({e['source_id'] for e in item['evidence']})} source(s)."
    ref = {"id": item["assertion_id"], "revision": item["revision_id"],
           "source_project": item["project"], "kind": "TemporalAssertion",
           "text": text, "evidence_level": "source_linked_curated"}
    for key in ("valid_from", "valid_to", "actor"):
        if item.get(key):
            ref[key] = item[key]
    if item.get("event_at"):
        ref["occurred_date"] = item["event_at"]
    return ref


def _entry(item, *, kind, as_of_utc=None, known_at_utc=None,
           event_day=None, timezone_name="UTC",
           generation_id="", query=None, reference_clock_utc=None,
           requested_day=None, current=False):
    reference = _reference(item, kind=kind, as_of_utc=as_of_utc,
                           known_at_utc=known_at_utc,
                           event_day=event_day, timezone_name=timezone_name,
                           requested_day=requested_day, current=current)
    card = compact_card(reference)
    card["expand"] = ["detail"]
    if as_of_utc is not None:
        card["as_of"] = datetime.fromtimestamp(as_of_utc, timezone.utc).isoformat()
    if event_day:
        card["event_day"] = event_day
    identity = [item["project"], item["subject"], item["predicate"],
                item["actor"], item["qualifiers"]]
    claim_key = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"card": card, "item": {"assertion_id": item["assertion_id"],
        "document_id": item["document_id"], "revision_id": item["revision_id"],
        "generation_id": generation_id, "source_project": item["project"],
        "layer": "temporal_card", "claim_key": claim_key,
        "as_of_utc": as_of_utc, "known_at_utc": known_at_utc,
        "event_day": event_day,
        "requested_day": requested_day, "current": current,
        "timezone_name": timezone_name, "answer_text": reference["text"],
        "query": query, "reference_clock_utc": reference_clock_utc}}


def historical_cards(state, query, project, read_projects=None, timezone_name="UTC",
                     reference_clock=None,authorized_document_ids=None,source_authorizer=None,
                     known_at=None,time_mode=None):
    """Return relevant grounded cards, [] for abstention, None for undated requests.

    Explicit current requests are temporal; an ordinary undated search stays on
    the existing retrieval path. One unresolved conflict, unknown interval, or
    incomplete bounded scan causes abstention instead of a plausible wrong answer.
    """
    if not isinstance(query, str) or not query.strip():
        return None
    if not _TIME_EXPRESSION.search(query):
        if _AMBIGUOUS_TIME.search(query) and _question_kind(query):
            return []
        return None
    clock = _clock(reference_clock)
    plan = parse_time_intent(query, reference_clock=clock,
                             timezone_name=timezone_name)
    if time_mode not in (None, 'current', 'effective_at', 'known_at'):
        raise ValueError('invalid_temporal_mode')
    if time_mode == 'known_at':
        if plan['kind'] in {'ambiguous', 'current'} or plan.get('as_of_utc') is None:
            return []
        if known_at is None:
            known_at = plan['as_of_utc']
    if plan["kind"] == "ambiguous":
        return []
    if plan["kind"] == "current" and not _CURRENT.search(query):
        return None
    kind = _question_kind(query)
    if kind is None:
        return []
    event_day = plan.get("day") if kind == "activity" else None
    as_of_utc = None if event_day else plan["as_of_utc"]
    result = select_assertions(state.db, project, read_projects=read_projects,
        predicate=kind, as_of=as_of_utc, event_day=event_day,
        known_at=known_at, timezone_name=timezone_name, now=clock, limit=100,
        authorized_document_ids=authorized_document_ids,source_authorizer=source_authorizer)
    if result["truncated"]:
        return []
    groups = [group for group in result["groups"] if _relevant(group, query, kind)]
    if not groups:
        return []
    if kind == "location":
        # Pronouns or similarly named records across projects/actors need more
        # context. Returning several incompatible locations would mislead.
        if len(groups) != 1:
            return []
    if any(group["status"] != "supported" for group in groups):
        return []
    items = [item for group in groups for item in group["assertions"]]
    if len(items) > 5:
        return []
    generation = active_generation_id(state.db) or ""
    return [_entry(item, kind=kind, as_of_utc=as_of_utc,
                   known_at_utc=result['known_at_utc'],
                   event_day=event_day, timezone_name=timezone_name,
                   generation_id=generation, query=query,
                   reference_clock_utc=clock.timestamp(),
                   requested_day=plan.get("day"),
                   current=plan["kind"] == "current") for item in items]


def _relevant(group, query, kind):
    if group["predicate"] != kind:
        return False
    if kind == "activity":
        return True
    subject_words = _words(group["subject"])
    if group.get('qualifiers',{}).get('origin')=='native_document':
        # Native titles are metadata rather than a verbatim user's question.
        # Require substantive title/topic coverage, never just its validity date.
        wanted=_words(query)
        subject_words-=set('the a an document specification policy guide manual handbook record'.split())
        if subject_words and len(subject_words&wanted)/len(subject_words)>=.5:
            return True
        # Descriptive titles can include administrative wording absent from a
        # question. A named subject plus two requested topic words in its faithful
        # passage is another match; a shared date or an entity alone is not.
        names={word.casefold() for word in re.findall(r'\b[A-Z][A-Za-z]+\b', query)
               if word.casefold() not in {'what','which','when','where','how','why','the','on','as'}}
        if not names or not names <= subject_words:
            return False
        stop=set('what which when where how why did does do is was were has have had use used '
                 'on as of at in to for from the a an and or now currently then'.split())
        topics={word for word in wanted-names-stop if not word.isdigit()}
        passage_words=set()
        for item in group.get('assertions',[])+group.get('unknown_time',[]):
            if isinstance(item.get('value'),str):passage_words.update(_words(item['value']))
        return len(topics & (subject_words | passage_words))>=2
    return bool(subject_words) and subject_words <= _words(query)


def detail_for_assertion(state, assertion_id, project, read_projects=None, *,
                         as_of=None, known_at=None, event_day=None, timezone_name="UTC",
                         authorized_document_ids=None,source_authorizer=None):
    """Fetch bounded detail after repeating current scope and evidence checks."""
    if not isinstance(assertion_id, str) or not assertion_id.startswith("ta_") or len(assertion_id) > 100:
        return None
    row = state.db.execute("""SELECT a.subject,a.predicate,a.actor,a.qualifiers_json,a.project,
        a.revision_id,a.document_id FROM knowledge_temporal_assertions a
        WHERE a.assertion_id=?""", (assertion_id,)).fetchone()
    if row is None:
        return None
    scopes = sorted(set(read_projects)) if read_projects is not None else [project]
    if project not in scopes:
        raise ValueError("read_scope_missing_receiver")
    if row["project"] not in scopes:
        return None
    selected = select_assertions(state.db, project, read_projects=scopes,
        subject=row["subject"], predicate=row["predicate"],
        actor=row["actor"], qualifiers=json.loads(row["qualifiers_json"]),
        as_of=as_of, known_at=known_at, event_day=event_day,
        timezone_name=timezone_name, limit=100,
        authorized_document_ids=authorized_document_ids,source_authorizer=source_authorizer)
    if selected["truncated"]:
        return None
    matching = [group for group in selected["groups"] if group["project"] == row["project"]
                and group["subject"].casefold() == row["subject"].casefold()
                and group["predicate"].casefold() == row["predicate"].casefold()
                and group["actor"] == row["actor"]
                and group["qualifiers"] == json.loads(row["qualifiers_json"])]
    if len(matching) != 1 or matching[0]["status"] != "supported":
        return None
    item = next((item for item in matching[0]["assertions"]
                 if item["assertion_id"] == assertion_id), None)
    if item is None:
        return None
    claim_row = state.db.execute("SELECT claim_json FROM knowledge_revisions WHERE revision_id=?",
                                 (item["revision_id"],)).fetchone()
    if claim_row is None:
        return None
    claim = json.loads(claim_row["claim_json"])
    answer = _reference(item, kind=item["predicate"],
                        as_of_utc=selected["as_of_utc"] if event_day is None else None,
                        known_at_utc=selected['known_at_utc'],
                        event_day=event_day, timezone_name=timezone_name)["text"]
    title = str(claim.get("title") or "")[:250]
    lesson = str(claim.get("lesson") or "")[:1800]
    text = f"{answer}\nCurated context: {title}. {lesson}"
    evidence = item["evidence"][:8]
    return {"id": item["assertion_id"], "document_id": item["document_id"],
            "revision": item["revision_id"], "source_project": item["project"],
            "kind": "TemporalAssertion", "text": text, "evidence": evidence,
            "additional_evidence_count": len(item["evidence"]) - len(evidence),
            "event_at": item["event_at"], "event_precision": item["event_precision"],
            "event_timezone": item["event_timezone"], "event_basis": item["event_basis"],
            "valid_from": item["valid_from"], "valid_to": item["valid_to"],
            "valid_to_status": item["valid_to_status"],
            "valid_basis": item["valid_basis"], "recorded_at": item["recorded_at"]}


def recheck_card(state, entry, project, read_projects=None):
    """Return a fresh card only if its generation and answer remain eligible."""
    if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
        return None
    item = entry["item"]
    if item.get("generation_id", "") != (active_generation_id(state.db) or ""):
        return None
    query = item.get("query")
    reference_clock = item.get("reference_clock_utc")
    if not isinstance(query, str) or type(reference_clock) not in (int, float):
        return None
    try:
        fresh = historical_cards(state, query, project, read_projects=read_projects,
            timezone_name=item.get("timezone_name", "UTC"),
            reference_clock=datetime.fromtimestamp(reference_clock, timezone.utc),
            known_at=item.get('known_at_utc'))
    except (OverflowError, ValueError):
        return None
    if not fresh:
        return None
    return next((candidate for candidate in fresh
        if candidate["item"]["assertion_id"] == item.get("assertion_id")
        and candidate["item"]["revision_id"] == item.get("revision_id")
        and candidate["item"]["generation_id"] == item.get("generation_id")), None)
