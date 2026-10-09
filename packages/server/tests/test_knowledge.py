import json
import tempfile
import unittest
from pathlib import Path

from agenthub.processing.knowledge import (
    accept_local_candidate,
    compatibility,
    initialize,
    ingest_observation,
    retire_memory,
    split_support,
)
from vaelius_test_support.fixtures.state import State


def claim(*, project_scope=None, title="Keep the explicit retry limit"):
    return {
        "title": title,
        "problem": "A transient request can be repeated too often.",
        "lesson": "Keep the explicit retry limit and preserve the status code.",
        "applicability": "Applies to the synthetic client fixture.",
        "applicability_constraints": {
            "versions": ["client 2.4"], "platforms": [], "date_ranges": [],
            "units": [], "project_scope": project_scope or [],
        },
        "knowledge_type": "procedure", "domain": "software_debugging",
        "subjects": ["retry policy"], "tags": ["fixture"], "outcome": "unknown",
    }


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "config.json").write_text(json.dumps({"observer": {"enabled": False}}))
        self.state = State(self.home)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def add(self, ident, project, value, sources=()):
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO memories(id,session,project,body,kind,created,active) VALUES(?,?,?,?,?,?,1)",
                (ident, "session-" + ident, project, json.dumps(value), "Observation", 10),
            )
            for source in sources:
                self.state.db.execute('INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                    (source, "source-session", project, "synthetic source evidence", "PostToolUse", 1))
                self.state.db.execute('INSERT INTO observation_sources VALUES(?,?) ON CONFLICT DO NOTHING', (ident, source))
            return ingest_observation(self.state.db, ident, project, "session-" + ident,
                {**value, "evidence": [{"source_id": source, "segment_id": "seg-" + source} for source in sources]})

    def test_exact_grouping_is_same_project_and_preserves_case_and_conditions(self):
        first = self.add("obs-a", "project-a", claim(), ["src-a"])
        same = self.add("obs-b", "project-a", claim(), ["src-b"])
        case_change = self.add("obs-c", "project-a", claim(title="Keep the explicit Retry limit"), ["src-c"])
        scope_change = self.add("obs-d", "project-a", claim(project_scope=["project-a"]), ["src-d"])
        other_project = self.add("obs-e", "project-b", claim(), ["src-e"])

        self.assertEqual(first["document_id"], same["document_id"])
        self.assertNotEqual(first["document_id"], case_change["document_id"])
        self.assertNotEqual(first["document_id"], scope_change["document_id"])
        self.assertNotEqual(first["document_id"], other_project["document_id"])
        supports = self.state.db.execute("SELECT independence FROM knowledge_support WHERE revision_id=?",
            (first["revision_id"],)).fetchall()
        self.assertEqual([row[0] for row in supports], ["unknown", "unknown"])
        candidates = self.state.db.execute("SELECT count(*) FROM knowledge_relations WHERE review_state='needs_review'").fetchone()[0]
        self.assertGreater(candidates, 0)
        # Lexical resemblance creates review work, never an automatic merge.
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM knowledge_documents WHERE project='project-a'").fetchone()[0], 3)

    def test_same_lineage_copy_does_not_inflate_independent_support(self):
        first = self.add("obs-a", "project-a", claim(), ["src-shared"])
        copied = self.add("obs-b", "project-a", claim(), ["src-shared"])
        self.assertEqual(first["document_id"], copied["document_id"])
        rows = self.state.db.execute("SELECT source_memory_id,independence FROM knowledge_support WHERE revision_id=?",
            (first["revision_id"],)).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("src-shared", "unknown")])

    def test_explicit_version_platform_unit_date_and_scope_conflicts_are_excluded(self):
        conditions={"versions":["2.4"],"platforms":["macOS"],"date_ranges":["2024-01-01 to 2024-12-31"],
            "units":["USD"],"project_scope":["project-a"]}
        value=claim();value["applicability_constraints"]=conditions
        self.assertEqual(compatibility(value,"Version 3.0 retry behavior","project-a")[0],"incompatible")
        self.assertEqual(compatibility(value,"Linux retry behavior","project-a")[0],"incompatible")
        self.assertEqual(compatibility(value,"Retry costs in EUR","project-a")[0],"incompatible")
        self.assertEqual(compatibility(value,"Retry behavior in 2025","project-a")[0],"incompatible")
        self.assertEqual(compatibility(value,"Retry behavior","project-b")[0],"incompatible")
        unknown=claim();unknown["applicability_constraints"]={}
        self.assertEqual(compatibility(unknown,"Version 99 retry behavior","project-a")[0],"unknown")


    def test_correction_adds_immutable_revision_and_reactivates_document(self):
        prior_claim = claim()
        prior = self.add("obs-old", "project-a", prior_claim, ["src-old"])
        retire_memory(self.state.db, "obs-old", withdrawn=False, replacement_memory_id="obs-new")
        replacement = accept_local_candidate(self.state.db, "obs-new", "project-a", "s-new",
            "Corrected retry limit", "Use the corrected retry policy after checking the status code.",
            ["src-new"], ["obs-old"])

        old = self.state.db.execute("SELECT claim_json,previous_revision_id FROM knowledge_revisions WHERE revision_id=?",
            (prior["revision_id"],)).fetchone()
        doc = self.state.db.execute("SELECT lifecycle,active_revision_id FROM knowledge_documents WHERE document_id=?",
            (prior["document_id"],)).fetchone()
        new = self.state.db.execute("SELECT previous_revision_id,revision_number FROM knowledge_revisions WHERE revision_id=?",
            (replacement["revision_id"],)).fetchone()
        self.assertEqual(json.loads(old["claim_json"])["title"], prior_claim["title"])
        self.assertIsNone(old["previous_revision_id"])
        self.assertEqual(tuple(doc.values()), ("active", replacement["revision_id"]))
        self.assertEqual(tuple(new.values()), (prior["revision_id"], 2))



    def test_ingest_preserves_caller_rollback(self):
        value = claim()
        try:
            self.state.db.execute('BEGIN')
            self.state.db.execute(
                "INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)",
                ("rollback-observation", "rollback-session", "project-a", json.dumps(value), "Observation", 10),
            )
            ingest_observation(
                self.state.db, "rollback-observation", "project-a", "rollback-session",
                {**value, "evidence": []},
            )
            initialize(self.state.db)
            raise RuntimeError("force outer rollback")
        except RuntimeError:
            self.state.db.rollback()

        self.assertIsNone(self.state.db.execute(
            "SELECT 1 FROM memories WHERE id='rollback-observation'"
        ).fetchone())
        self.assertIsNone(self.state.db.execute(
            "SELECT 1 FROM knowledge_document_members WHERE memory_id='rollback-observation'"
        ).fetchone())
        self.assertEqual(self.state.db.execute(
            "SELECT count(*) FROM knowledge_fts WHERE document_id LIKE 'doc_%'"
        ).fetchone()[0], 0)

    def test_withdrawal_removes_support_and_purges_last_revision(self):
        first = self.add("obs-a", "project-a", claim(), ["src-a"])
        second = self.add("obs-b", "project-a", claim(), ["src-b"])
        self.assertEqual(first["document_id"], second["document_id"])

        retire_memory(self.state.db, "obs-b", withdrawn=True)
        remaining = self.state.db.execute("SELECT source_memory_id FROM knowledge_support WHERE revision_id=?",
            (first["revision_id"],)).fetchall()
        self.assertEqual([row[0] for row in remaining], ["src-a"])
        retire_memory(self.state.db, "obs-a", withdrawn=True)
        document = self.state.db.execute("SELECT lifecycle,active_revision_id FROM knowledge_documents WHERE document_id=?",
            (first["document_id"],)).fetchone()
        self.assertEqual(tuple(document.values()), ("withdrawn", None))
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM knowledge_revisions WHERE document_id=?",
            (first["document_id"],)).fetchone()[0], 0)

    def test_split_rebuilds_selected_support_as_independent_singleton(self):
        first = self.add("obs-a", "project-a", claim(), ["src-a"])
        self.add("obs-b", "project-a", claim(), ["src-b"])
        result = split_support(self.state.db, first["document_id"], ["obs-b"], "project-a")
        self.assertEqual(result["remaining_supports"], 1)
        self.assertEqual(len(result["rebuilt_document_ids"]), 1)
        docs = self.state.db.execute("SELECT lifecycle,count(*) FROM knowledge_documents GROUP BY lifecycle").fetchall()
        self.assertEqual({row[0]: row[1] for row in docs}, {"active": 2})
        rebuilt = self.state.db.execute("SELECT document_id FROM knowledge_document_members WHERE memory_id='obs-b'").fetchone()[0]
        self.assertEqual(rebuilt, result["rebuilt_document_ids"][0])
        self.assertNotEqual(rebuilt, first["document_id"])


if __name__ == "__main__":
    unittest.main()
