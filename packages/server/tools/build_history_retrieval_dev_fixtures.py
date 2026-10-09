"""Build the frozen, synthetic H1 development bank. No provider or private data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


DEST = Path(__file__).parent / "fixtures/history_retrieval_v1"
CONFIRMATION_SHA256 = "6272eaccdb7d7b2f044f0e7ffa8efd24d95f496454aa90da3c9d31676f0e7315"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


class Bank:
    def __init__(self) -> None:
        self.sources: list[dict] = []
        self.events: list[dict] = []
        self.questions: list[dict] = []
        self.families: list[dict] = []
        self.runtime_operations: list[dict] = []
        self.invariants: list[dict] = []
        self.family = ""
        self.project = ""

    def start(self, family: str, project: str, distinction: str) -> None:
        self.family, self.project = family, project
        self.families.append({"id": family, "project_id": project, "distinction": distinction})

    def source(self, suffix: str, text: str, *, speaker: str, recorded: str,
               session: str, kind: str = "conversation_segment", reader_ids=("ada", "ben"),
               connection: str = "team", version: str = "1", lifecycle: str = "active",
               original_id: str | None = None) -> str:
        sid = f"dev-{self.family}-{suffix}"
        self.sources.append({"id": sid, "family_id": self.family,
                             "project_id": self.project, "tenant_id": "synthetic-lab",
                             "session_id": f"{self.family}:{session}", "kind": kind,
                             "speaker_id": speaker, "recorded_at": recorded,
                             "text": text, "text_sha256": digest(text.encode()),
                             "reader_ids": list(reader_ids), "connection_id": connection,
                             "source_version": version, "lifecycle": lifecycle,
                             "original_id": original_id})
        return sid

    def event(self, suffix: str, source_id: str, quote: str, *, actor: str,
              status: str, effective: str | None, reason: str | None = None,
              truth: str = "observed", order: int, precision: str = "day",
              replaces: str | None = None, lineage: str | None = None,
              audience=("ada", "ben")) -> str:
        source = next(s for s in self.sources if s["id"] == source_id)
        start = source["text"].index(quote)
        eid = f"dev-{self.family}-{suffix}"
        self.events.append({"id": eid, "family_id": self.family,
                            "project_id": self.project, "actor_id": actor,
                            "speaker_id": source["speaker_id"], "status": status,
                            "truth": truth, "effective_at": effective,
                            "time_precision": precision if effective else "unknown",
                            "recorded_at": source["recorded_at"], "source_order": order,
                            "reason": reason, "replaces_event_id": replaces,
                            "evidence_lineage_id": lineage or source_id,
                            "policy_dependencies": [source_id], "audience": list(audience),
                            "source_refs": [{"source_id": source_id, "version": source["source_version"],
                                             "start": start, "end": start + len(quote), "quote": quote}]})
        return eid

    def question(self, suffix: str, query: str, event_ids: list[str], answer: str | None,
                 *, mode: str, reader: str = "ben", at: str | None = None,
                 deny: str | None = None, forbidden=(), page_limit: int | None = None) -> None:
        refs = [r for event in self.events if event["id"] in event_ids for r in event["source_refs"]]
        self.questions.append({"id": f"dev-{self.family}-q{suffix}", "family_id": self.family,
                               "project_id": self.project, "tenant_id": "synthetic-lab",
                               "reader_id": reader, "query": query, "mode": mode,
                               "as_of": at, "page_byte_limit": page_limit,
                               "expected": {"answerable": answer is not None,
                                            "answer": answer, "event_ids": event_ids,
                                            "evidence_refs": refs, "abstention_reason": deny,
                                            "forbidden_event_ids": list(forbidden)}})

    def build(self) -> dict:
        return {"version": "history-retrieval-h1-dev-v1", "classification": "authored synthetic development; no natural-use evidence",
                "oracle_protocol": "history-source-event-current-policy-v1",
                "families": self.families, "sources": self.sources,
                "runtime_operations": self.runtime_operations,
                "invariants": self.invariants,
                "oracle_events": self.events,
                "fixed_curated_records": [
                    {"id": f"fixed:{e['id']}", "event_id": e["id"], "project_id": e["project_id"],
                     "actor_id": e["actor_id"], "status": e["status"], "truth": e["truth"],
                     "effective_at": e["effective_at"], "time_precision": e["time_precision"],
                     "recorded_at": e["recorded_at"], "source_order": e["source_order"],
                     "reason": e["reason"], "source_refs": e["source_refs"],
                     "policy_dependencies": e["policy_dependencies"],
                     "audience": e["audience"]} for e in self.events],
                "questions": self.questions,
                "scoring_layers": {
                    "source_to_curated": "Compare extracted occurrence IDs, order, actor, status, truth, reason, time precision and source spans to oracle_events; omissions belong to extraction.",
                    "fixed_curated_retrieval": "Load fixed_curated_records only; compare delivery to questions. This does not score extraction.",
                    "end_to_end": "Ingest sources, curate and deliver; score both extraction and answer separately, including current authorization."},
                "metrics": ["event_coverage", "event_order", "rationale_attribution", "current_accuracy",
                            "effective_at_accuracy", "known_at_accuracy", "false_merges", "missed_conflicts",
                            "evidence_fidelity", "permission_denial", "pagination_completeness"]}


def create() -> dict:
    b = Bank()
    b.start("reversion", "cedar-routing", "A adopted, changed to B, reverted to A with three source-supported reasons")
    s1 = b.source("s1", "Mara approved route A for Cedar because the depot has a staffed morning desk.", speaker="mara", recorded="2026-05-02T15:00:00Z", session="1")
    e1 = b.event("e1", s1, "Mara approved route A for Cedar because the depot has a staffed morning desk.", actor="mara", status="adopted", effective="2026-05-02", reason="staffed morning desk", order=1)
    s2 = b.source("s2", "Mara changed Cedar to route B because route A's bridge was closed.", speaker="mara", recorded="2026-05-09T15:00:00Z", session="2")
    e2 = b.event("e2", s2, "Mara changed Cedar to route B because route A's bridge was closed.", actor="mara", status="adopted", effective="2026-05-09", reason="route A bridge closed", order=2, replaces=e1)
    s3 = b.source("s3", "Mara restored route A for Cedar after the bridge reopened and the desk remained staffed.", speaker="mara", recorded="2026-05-16T15:00:00Z", session="3")
    e3 = b.event("e3", s3, "Mara restored route A for Cedar after the bridge reopened and the desk remained staffed.", actor="mara", status="adopted", effective="2026-05-16", reason="bridge reopened and staffed desk", order=3, replaces=e2)
    b.question("1", "Which route does Cedar use now?", [e3], "Route A.", mode="current", forbidden=[e2])
    b.question("2", "How did Cedar's route decisions evolve, and why?", [e1, e2, e3], "A for the staffed desk; B while A's bridge was closed; A again after reopening.", mode="history")
    b.question("3", "Which route applied to Cedar on May 11?", [e2], "Route B.", mode="effective_at", at="2026-05-11")
    b.question("4", "Did Mara choose route C for Cedar?", [], None, mode="history", deny="false_premise")

    b.start("corrected_report", "elm-migration", "Incorrect success report retained as a corrected report, not asserted success")
    s1 = b.source("s1", "Ivo reported that the Elm migration completed successfully, pending checksum review.", speaker="ivo", recorded="2026-05-03T10:00:00Z", session="1")
    e1 = b.event("e1", s1, "Ivo reported that the Elm migration completed successfully, pending checksum review.", actor="ivo", status="reported_success", truth="reported_unverified", effective="2026-05-03", order=1)
    s2 = b.source("s2", "Checksum review found missing rows; the Elm migration had failed, correcting Ivo's earlier report.", speaker="nina", recorded="2026-05-04T12:00:00Z", session="2")
    e2 = b.event("e2", s2, "Checksum review found missing rows; the Elm migration had failed, correcting Ivo's earlier report.", actor="nina", status="correction", truth="observed", effective="2026-05-04", reason="missing rows", order=2, replaces=e1)
    s3 = b.source("s3", "Nina reran Elm migration; checksum review passed and she marked it complete.", speaker="nina", recorded="2026-05-06T12:00:00Z", session="3")
    e3 = b.event("e3", s3, "Nina reran Elm migration; checksum review passed and she marked it complete.", actor="nina", status="observed_success", effective="2026-05-06", reason="checksum review passed", order=3)
    b.question("1", "Did Elm migration actually succeed on May 3?", [e1, e2], "No. Ivo reported success, but checksum review later found it had failed.", mode="history", forbidden=[e3])
    b.question("2", "What is Elm migration's verified state now?", [e3], "Complete after the May 6 rerun and checksum review.", mode="current")
    b.question("3", "Who corrected the original Elm report and why?", [e2], "Nina corrected it after finding missing rows.", mode="history")
    b.question("4", "Why did Elm migration succeed on May 3?", [], None, mode="history", deny="false_premise")

    b.start("late_import", "birch-queue", "Earlier effective date recorded later; effective-at and known-at diverge")
    s1 = b.source("s1", "On May 8, the Birch queue policy still used a 20-item batch.", speaker="omar", recorded="2026-05-08T17:00:00Z", session="1")
    e1 = b.event("e1", s1, "On May 8, the Birch queue policy still used a 20-item batch.", actor="omar", status="observed", effective="2026-05-08", order=1)
    s2 = b.source("s2", "Imported June 1: the Birch board adopted a 30-item batch effective May 10.", speaker="archive-service", recorded="2026-06-01T09:00:00Z", session="2", kind="native_document")
    e2 = b.event("e2", s2, "the Birch board adopted a 30-item batch effective May 10", actor="birch-board", status="adopted", effective="2026-05-10", order=2, replaces=e1)
    s3 = b.source("s3", "On June 2, Omar confirmed that Birch currently uses the 30-item batch.", speaker="omar", recorded="2026-06-02T09:00:00Z", session="3")
    e3 = b.event("e3", s3, "On June 2, Omar confirmed that Birch currently uses the 30-item batch.", actor="omar", status="confirmed", effective="2026-06-02", order=3)
    b.question("1", "Using today's knowledge, what applied to Birch on May 15?", [e2], "A 30-item batch.", mode="effective_at", at="2026-05-15")
    b.question("2", "What did the system know about Birch on May 15?", [e1], "It had the earlier 20-item batch record; the 30-item decision was not imported until June 1.", mode="known_at", at="2026-05-15", forbidden=[e2])
    b.question("3", "When was the 30-item Birch decision recorded here?", [e2], "June 1, though effective May 10.", mode="history")
    b.question("4", "Was the 30-item Birch decision recorded by May 11?", [], None, mode="known_at", at="2026-05-11", deny="not_yet_known")

    b.start("unknown_dst", "spruce-shift", "Unknown event date and America/Denver DST local-day boundary")
    s1 = b.source("s1", "The undated Spruce note says technician Lee changed the filter orientation.", speaker="archivist", recorded="2026-11-03T11:00:00Z", session="1", kind="native_document")
    e1 = b.event("e1", s1, "technician Lee changed the filter orientation", actor="lee", status="observed", effective=None, precision="unknown", order=1)
    s2 = b.source("s2", "At 2026-11-01 01:30 MDT, Lee paused the Spruce shift for a sensor check.", speaker="lee", recorded="2026-11-01T07:45:00Z", session="2")
    e2 = b.event("e2", s2, "At 2026-11-01 01:30 MDT, Lee paused the Spruce shift for a sensor check.", actor="lee", status="observed", effective="2026-11-01T01:30:00-06:00", precision="instant", order=2)
    s3 = b.source("s3", "At 2026-11-01 01:30 MST, Lee resumed the Spruce shift after the sensor passed.", speaker="lee", recorded="2026-11-01T08:45:00Z", session="3")
    e3 = b.event("e3", s3, "At 2026-11-01 01:30 MST, Lee resumed the Spruce shift after the sensor passed.", actor="lee", status="observed", effective="2026-11-01T01:30:00-07:00", precision="instant", order=3)
    b.question("1", "What happened on Spruce's November 1 local shift?", [e2, e3], "Lee paused at 01:30 MDT, then resumed at 01:30 MST.", mode="history")
    b.question("2", "What is known about the Spruce filter orientation change?", [e1], "Lee changed it; the date is unknown.", mode="history")
    b.question("3", "Which 01:30 came first in Spruce's shift?", [e2, e3], "The MDT pause came before the MST resume.", mode="history")
    b.question("4", "Did Lee change the filter orientation on November 1?", [], None, mode="effective_at", at="2026-11-01", deny="unknown_event_date")

    b.start("replay_repeat", "ash-gateway", "Same transport event replayed; same failure on two dates are two occurrences")
    s1 = b.source("s1", "Ash gateway failed a handshake on May 4; event key ash-evt-7.", speaker="monitor", recorded="2026-05-04T11:00:00Z", session="1")
    e1 = b.event("e1", s1, "Ash gateway failed a handshake on May 4; event key ash-evt-7.", actor="ash-gateway", status="failed_attempt", effective="2026-05-04", order=1, lineage="ash-evt-7")
    s2 = b.source("s2", "Replay of event key ash-evt-7: Ash gateway failed a handshake on May 4.", speaker="transport", recorded="2026-05-05T10:00:00Z", session="2")
    e2 = b.event("e2", s2, "Replay of event key ash-evt-7: Ash gateway failed a handshake on May 4.", actor="ash-gateway", status="transport_replay", effective="2026-05-04", order=2, lineage="ash-evt-7")
    s3 = b.source("s3", "Ash gateway failed another handshake on May 8; event key ash-evt-9.", speaker="monitor", recorded="2026-05-08T11:00:00Z", session="3")
    e3 = b.event("e3", s3, "Ash gateway failed another handshake on May 8; event key ash-evt-9.", actor="ash-gateway", status="failed_attempt", effective="2026-05-08", order=3, lineage="ash-evt-9")
    b.question("1", "How many distinct Ash handshake failures occurred?", [e1, e3], "Two, on May 4 and May 8.", mode="history", forbidden=[e2])
    b.question("2", "What happened to ash-evt-7 on May 5?", [e1, e2], "It was replayed; it was not a second failure.", mode="history")
    b.question("3", "List Ash's failed handshake dates.", [e1, e3], "May 4 and May 8.", mode="history")
    b.question("4", "Was there a third Ash failure on May 5?", [], None, mode="history", deny="duplicate_transport")

    b.start("quoted_lineage", "maple-safety", "Several agents quote one source; distinct mentions do not create independent corroboration")
    s1 = b.source("s1", "The Maple safety memo states: inspection takes 12 minutes per unit.", speaker="safety-office", recorded="2026-05-01T12:00:00Z", session="1", kind="native_document")
    e1 = b.event("e1", s1, "inspection takes 12 minutes per unit", actor="safety-office", status="source_statement", effective="2026-05-01", order=1, lineage="maple-memo-v1")
    s2 = b.source("s2", "Agent Echo quoted the Maple memo's 12-minute inspection figure; Echo did not measure it.", speaker="agent-echo", recorded="2026-05-03T12:00:00Z", session="2")
    e2 = b.event("e2", s2, "Agent Echo quoted the Maple memo's 12-minute inspection figure; Echo did not measure it.", actor="agent-echo", status="quoted_mention", effective="2026-05-03", order=2, lineage="maple-memo-v1")
    s3 = b.source("s3", "Agent Foxtrot repeated Echo's Maple quotation without independent measurement.", speaker="agent-foxtrot", recorded="2026-05-06T12:00:00Z", session="3")
    e3 = b.event("e3", s3, "Agent Foxtrot repeated Echo's Maple quotation without independent measurement.", actor="agent-foxtrot", status="quoted_mention", effective="2026-05-06", order=3, lineage="maple-memo-v1")
    b.question("1", "What duration does the Maple memo give?", [e1], "12 minutes per unit.", mode="current")
    b.question("2", "How many independent sources support Maple's duration?", [e1, e2, e3], "One: the Maple memo; two agents repeated it.", mode="history")
    b.question("3", "Which agents repeated the Maple source?", [e2, e3], "Echo and Foxtrot.", mode="history")
    b.question("4", "Did Echo independently measure Maple inspection time?", [], None, mode="history", deny="no_independent_measurement")

    b.start("proposal_attempt", "juniper-release", "Rejected proposal and failed attempt survive without adoption or reusable lesson")
    s1 = b.source("s1", "Tess proposed a Friday Juniper release, but the review board rejected it because staffing was thin.", speaker="tess", recorded="2026-05-02T10:00:00Z", session="1")
    e1 = b.event("e1", s1, "Tess proposed a Friday Juniper release, but the review board rejected it because staffing was thin.", actor="tess", status="rejected_proposal", effective="2026-05-02", reason="thin staffing", order=1)
    s2 = b.source("s2", "Juniper's test release on Tuesday failed during setup; the log states no general lesson yet.", speaker="test-agent", recorded="2026-05-05T10:00:00Z", session="2")
    e2 = b.event("e2", s2, "Juniper's test release on Tuesday failed during setup; the log states no general lesson yet.", actor="test-agent", status="failed_attempt", effective="2026-05-05", order=2)
    s3 = b.source("s3", "The Juniper board adopted Thursday release windows after staffing was confirmed.", speaker="board-clerk", recorded="2026-05-08T10:00:00Z", session="3")
    e3 = b.event("e3", s3, "The Juniper board adopted Thursday release windows after staffing was confirmed.", actor="juniper-board", status="adopted", effective="2026-05-08", reason="staffing confirmed", order=3)
    b.question("1", "Was Friday adopted for Juniper?", [e1, e3], "No. Friday was rejected; Thursday was adopted.", mode="history")
    b.question("2", "What happened in the Juniper test release?", [e2], "The Tuesday attempt failed during setup; no general lesson was recorded.", mode="history")
    b.question("3", "What release window applies now?", [e3], "Thursday.", mode="current")
    b.question("4", "What reusable rule did the failed Tuesday attempt establish?", [], None, mode="history", deny="no_supported_lesson")

    b.start("conflicts", "willow-budget", "Conflicting same-scope amount versus conditional actor/version variants")
    s1 = b.source("s1", "Willow v2 shared budget is 80 EUR, according to finance review A.", speaker="finance-a", recorded="2026-05-03T10:00:00Z", session="1")
    e1 = b.event("e1", s1, "Willow v2 shared budget is 80 EUR, according to finance review A.", actor="finance-a", status="reported_value", effective="2026-05-03", order=1)
    s2 = b.source("s2", "Willow v2 shared budget is 95 EUR, according to finance review B; no reconciliation is recorded.", speaker="finance-b", recorded="2026-05-04T10:00:00Z", session="2")
    e2 = b.event("e2", s2, "Willow v2 shared budget is 95 EUR, according to finance review B; no reconciliation is recorded.", actor="finance-b", status="reported_value", effective="2026-05-04", order=2)
    s3 = b.source("s3", "Willow v2 shared budget is 70 GBP, according to finance review C; the exchange basis is unspecified.", speaker="finance-c", recorded="2026-05-05T10:00:00Z", session="3")
    e3 = b.event("e3", s3, "Willow v2 shared budget is 70 GBP, according to finance review C; the exchange basis is unspecified.", actor="finance-c", status="reported_value", effective="2026-05-05", order=3)
    s4 = b.source("s4", "Willow v3 sandbox budget is 120 USD for Tess only; this is a different scope.", speaker="tess", recorded="2026-05-06T10:00:00Z", session="4")
    e4 = b.event("e4", s4, "Willow v3 sandbox budget is 120 USD for Tess only; this is a different scope.", actor="tess", status="conditional_variant", effective="2026-05-06", order=4)
    b.question("1", "What is the Willow v2 shared budget?", [e1, e2, e3], "Disputed: finance A says 80 EUR, B says 95 EUR, and C says 70 GBP without an exchange basis.", mode="current")
    b.question("2", "What budget applies to Tess's Willow v3 sandbox?", [e4], "120 USD for Tess's v3 sandbox.", mode="current")
    b.question("3", "Are the Willow v2 reports reconciled?", [e1, e2, e3], "No reconciliation is recorded.", mode="history")
    b.question("4", "Is 120 USD the resolved Willow v2 shared budget?", [], None, mode="current", deny="wrong_scope_and_unresolved_conflict")

    b.start("deep_history", "poplar-catalog", "Old paraphrased claim survives more than 200 intervening records")
    s1 = b.source("s1", "Poplar's archive lookup uses a three-letter shelf tag, adopted after the paper index was lost.", speaker="catalog-board", recorded="2026-01-05T10:00:00Z", session="1")
    e1 = b.event("e1", s1, "Poplar's archive lookup uses a three-letter shelf tag, adopted after the paper index was lost.", actor="catalog-board", status="adopted", effective="2026-01-05", reason="paper index lost", order=1)
    for i in range(205):
        b.source(f"noise{i:03d}", f"Poplar inventory note {i:03d} records routine shelf cleaning; it changes no lookup decision.", speaker="inventory-bot", recorded=f"2026-02-{i % 28 + 1:02d}T10:00:00Z", session=f"routine-{i:03d}")
    s2 = b.source("s2", "After catalog review, Poplar still locates archival boxes by short alphabetic rack code.", speaker="catalog-board", recorded="2026-04-05T10:00:00Z", session="2")
    e2 = b.event("e2", s2, "Poplar still locates archival boxes by short alphabetic rack code.", actor="catalog-board", status="confirmed", effective="2026-04-05", order=207, lineage=s1)
    b.question("1", "How does Poplar find archival boxes now?", [e1, e2], "By a three-letter shelf tag, also described as a short alphabetic rack code.", mode="current")
    b.question("2", "Why did Poplar adopt its lookup tag?", [e1], "The paper index was lost.", mode="history")
    b.question("3", "Did the April review replace Poplar's lookup method?", [e1, e2], "No; it confirmed the same method in different words.", mode="history")
    b.question("4", "Did routine cleaning authorize a numeric lookup code?", [], None, mode="history", deny="noise_is_not_decision")

    b.start("identity_private", "larch-team", "Same-name actors/projects and private preference distinct from shared project event")
    s1 = b.source("s1", "Alex-17 approved the Larch-team green dashboard for shared review.", speaker="alex-17", recorded="2026-05-02T10:00:00Z", session="1")
    e1 = b.event("e1", s1, "Alex-17 approved the Larch-team green dashboard for shared review.", actor="alex-17", status="adopted", effective="2026-05-02", order=1)
    s2 = b.source("s2", "Alex-42 privately prefers the Larch-personal violet dashboard; this is not a team decision.", speaker="alex-42", recorded="2026-05-03T10:00:00Z", session="2", reader_ids=("alex-42",), connection="private")
    e2 = b.event("e2", s2, "Alex-42 privately prefers the Larch-personal violet dashboard; this is not a team decision.", actor="alex-42", status="private_preference", effective="2026-05-03", order=2, audience=("alex-42",))
    s3 = b.source("s3", "Alex-17 confirmed that Larch-team dashboard remains green.", speaker="alex-17", recorded="2026-05-05T10:00:00Z", session="3")
    e3 = b.event("e3", s3, "Alex-17 confirmed that Larch-team dashboard remains green.", actor="alex-17", status="confirmed", effective="2026-05-05", order=3)
    b.question("1", "What dashboard did Larch-team approve?", [e1, e3], "Green, approved by Alex-17.", mode="current", forbidden=[e2])
    b.question("2", "Who confirmed Larch-team's color?", [e3], "Alex-17.", mode="history")
    b.question("3", "What is Alex-42's private Larch-personal preference?", [e2], "Violet for Larch-personal; not a team decision.", mode="current", reader="alex-42")
    b.question("4", "What is Alex-42's private Larch-personal preference?", [], None, mode="current", reader="ben", deny="current_policy_denial", forbidden=[e2])

    b.start("concurrency_lifecycle", "fir-incident", "Concurrent observers, late update, revocation and deletion constrain derivatives")
    s1 = b.source("s1", "Observer One recorded Fir incident opened by Dana at 09:00.", speaker="observer-one", recorded="2026-05-04T09:01:00Z", session="observer-1")
    e1 = b.event("e1", s1, "Fir incident opened by Dana at 09:00.", actor="dana", status="opened", effective="2026-05-04T09:00:00Z", precision="instant", order=1)
    s2 = b.source("s2", "Observer Two recorded Fir mitigation attempted by Eli at 09:03; it failed.", speaker="observer-two", recorded="2026-05-04T09:04:00Z", session="observer-2")
    e2 = b.event("e2", s2, "Fir mitigation attempted by Eli at 09:03; it failed.", actor="eli", status="failed_attempt", effective="2026-05-04T09:03:00Z", precision="instant", order=2)
    s3 = b.source("s3", "Observer One delivered late: Dana closed Fir incident at 09:08 after rollback.", speaker="observer-one", recorded="2026-05-04T09:20:00Z", session="observer-1")
    e3 = b.event("e3", s3, "Dana closed Fir incident at 09:08 after rollback.", actor="dana", status="closed", effective="2026-05-04T09:08:00Z", precision="instant", reason="rollback", order=3)
    s4 = b.source("s4", "Fir access ledger: Ben was revoked at 09:10; source Two was deleted at 09:15.", speaker="policy-service", recorded="2026-05-04T09:16:00Z", session="policy", kind="policy_record", reader_ids=("ada",))
    e4 = b.event("e4", s4, "Ben was revoked at 09:10; source Two was deleted at 09:15.", actor="policy-service", status="policy_change", effective="2026-05-04T09:10:00Z", precision="instant", order=4, audience=("ada",))
    b.question("1", "What is Fir's full authorized incident sequence?", [e1, e3], "Dana opened Fir at 09:00 and closed it at 09:08 after rollback; the deleted attempt must not be delivered.", mode="history", reader="ada", forbidden=[e2])
    b.question("2", "Did Fir's late close update erase its opening?", [e1, e3], "No; opening and close remain distinct events.", mode="history", reader="ada")
    b.question("3", "What policy change governs Fir delivery?", [e4], "Ben was revoked and source Two was deleted.", mode="history", reader="ada")
    b.question("4", "What happened in Fir's private incident?", [], None, mode="history", reader="ben", deny="current_policy_denial", forbidden=[e1, e2, e3, e4])

    b.start("long_pages", "oak-build", "Long multi-phase history with stable byte-limited continuation and native source versions")
    phases = ["charter approved", "prototype assembled", "trial failed", "trial retried", "review passed", "parts ordered", "assembly revised", "test passed", "launch proposed", "launch delayed", "launch approved", "handoff recorded"]
    event_ids = []
    for i, phase in enumerate(phases, 1):
        s = b.source(f"phase{i:02d}", f"Oak phase {i:02d}: {phase}; curator Mira recorded this milestone in the project log.", speaker="mira", recorded=f"2026-05-{i:02d}T10:00:00Z", session=f"phase-{i:02d}")
        event_ids.append(b.event(f"e{i:02d}", s, f"Oak phase {i:02d}: {phase}", actor="mira", status="milestone", effective=f"2026-05-{i:02d}", order=i))
    doc1 = b.source("native-v1", "Oak design v1: prototype uses the narrow bracket.", speaker="document-service", recorded="2026-05-02T12:00:00Z", session="native-1", kind="native_document", original_id="oak-design", version="1")
    d1 = b.event("document1", doc1, "prototype uses the narrow bracket", actor="oak-design-team", status="document_version", effective="2026-05-02", order=2)
    doc2 = b.source("native-v2", "Oak design v2: revised assembly uses the wide bracket.", speaker="document-service", recorded="2026-05-07T12:00:00Z", session="native-2", kind="native_document", original_id="oak-design", version="2")
    d2 = b.event("document2", doc2, "revised assembly uses the wide bracket", actor="oak-design-team", status="document_version", effective="2026-05-07", order=7, replaces=d1)
    b.question("1", "Give Oak's complete project history with continuation.", event_ids, "All twelve phases from charter approval through handoff in order, with a continuation token until complete.", mode="history", page_limit=320)
    b.question("2", "What did Oak's original design v1 say?", [d1], "The prototype uses the narrow bracket.", mode="original", at="1")
    b.question("3", "What does Oak design v2 say?", [d2], "The revised assembly uses the wide bracket.", mode="original", at="2")
    b.question("4", "Was Oak launch approved before the failed trial?", [], None, mode="history", deny="false_order", forbidden=[event_ids[2], event_ids[10]])

    b.runtime_operations.extend([
        {"id": "dev-poplar-ordinary-ingest", "kind": "ordinary_ingest", "source_id": "dev-deep_history-s1", "expected_index_state": "pending_then_active"},
        {"id": "dev-poplar-update", "kind": "ordinary_update", "source_id": "dev-deep_history-s2", "precondition": "previous decision remains addressable", "expected_index_state": "pending"},
        {"id": "dev-poplar-vector-refresh", "kind": "bounded_vector_refresh", "source_id": "dev-deep_history-s2", "after": "dev-poplar-update", "expected_index_state": "active", "expected_query": "short alphabetic rack code"},
        {"id": "dev-oak-source-replacement", "kind": "replace_native_version", "old_source_id": doc1, "new_source_id": doc2, "expected_originals": "both versions retained under current authorization"},
        {"id": "dev-fir-delete", "kind": "delete_source", "source_id": "dev-concurrency_lifecycle-s2", "evidence_source_id": "dev-concurrency_lifecycle-s4", "expected": "no historical/detail/summary/cache/delivery output from deleted source"},
        {"id": "dev-fir-revoke", "kind": "revoke_reader", "reader_id": "ben", "project_id": "fir-incident", "evidence_source_id": "dev-concurrency_lifecycle-s4", "expected": "current denial at every historical boundary"},
        {"id": "dev-larch-summary-invalidate", "kind": "invalidate_summary", "source_id": "dev-identity_private-s2", "expected": "team summary excludes private preference and dependency metadata"},
        {"id": "dev-fir-stale-resolver", "kind": "concurrent_expected_revision", "first_event_id": "dev-concurrency_lifecycle-e2", "late_event_id": "dev-concurrency_lifecycle-e3", "expected": "stale proposal retries after accepted revision; both distinct occurrences retained; no stale install"},
    ])
    b.invariants.extend([
        {"id": "corrected-episode", "event_ids": ["dev-corrected_report-e1", "dev-corrected_report-e2"], "assert": "old reported-success episode remains addressable and labeled corrected; current claim never states May 3 success"},
        {"id": "activity-without-learning", "event_ids": ["dev-proposal_attempt-e2"], "assert": "failed attempt remains addressable with zero canonical lesson claims; routine Poplar cleaning produces no event"},
        {"id": "timeline-serialized-limit", "question_id": "dev-long_pages-q1", "assert": "all 12 milestone IDs returned exactly once in stable order across serialized pages of at most 320 bytes, with truthful continuation"},
        {"id": "source-replacement-v-withdrawal", "operation_ids": ["dev-oak-source-replacement", "dev-fir-delete"], "assert": "replacement retains permitted v1 original; withdrawal suppresses deleted evidence from all derivatives"},
    ])
    return b.build()


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    data = create()
    path = DEST / "development.json"
    raw = encoded(data)
    path.write_bytes(raw)
    manifest = {"version": "history-retrieval-h1-manifest-v1", "development_path": path.name,
                "development_sha256": digest(raw), "development_families": len(data["families"]),
                "development_questions": len(data["questions"]),
                "confirmation_classification": "separate authored synthetic supplement, private sealed; not independent real-team evidence",
                "confirmation_questions": 48, "confirmation_positive": 36,
                "confirmation_abstention_or_denial": 12,
                "confirmation_sha256": CONFIRMATION_SHA256,
                "confirmation_body_location": "private history-retrieval-v1/sealed/; never load for DEV or guard tests",
                "S_confirmation_sha256": "a41fea2bc3983d8e99ea3dcdfcd7f419d4250ac325e0bd2fb151979c086fb79f"}
    (DEST / "manifest.json").write_bytes(encoded(manifest))


if __name__ == "__main__":
    main()
