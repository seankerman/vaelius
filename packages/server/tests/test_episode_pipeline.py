import json
import tempfile
import unittest
from pathlib import Path

from agenthub.processing.harness import HarnessError
from vaelius_test_support.fixtures.state import State


class FixtureRunner:
    def __init__(self):
        self.calls=[]
        self.fail_resolution_once=False


    def __call__(self, home, config, instruction, payload, schema):
        purpose=config["_purpose"];self.calls.append(purpose)
        if purpose == 'durable_memory_curate':
            event=next((e for e in payload['episode']['events'] if e['kind']=='PostToolUse'),None)
            if event is None:return {'records':[]},{}
            corrected='corrected' in event['spans'][0]['text'].lower()
            return {'records':[dict(title='Cobalt retry verification',
                text='Use the corrected Cobalt retry policy.' if corrected else 'Use the initial Cobalt retry policy.',
                subject='Cobalt retry',facets=['activity','procedure'],actors=['agent'],
                artifact=None,rationale=None,state='observed',occurred_date='',
                event_id=event['event_id'],evidence_span_ids=[event['spans'][0]['span_id']])]},{}
        if purpose=="episode_resolve":
            if self.fail_resolution_once:
                self.fail_resolution_once=False
                raise HarnessError("harness_failed")
            artifacts=payload["active_artifacts"]
            correction="corrected" in payload["candidate"]["claim"].casefold()
            operation="CORRECT" if correction and artifacts else "CREATE"
            reason="same_subject_newer_correction" if operation=="CORRECT" else "new_claim"
            return {"candidate_key":payload["candidate"]["candidate_key"],
                    "operation":operation,
                    "target_artifact_id":artifacts[0]["artifact_id"] if operation=="CORRECT" else "",
                    "reason":reason},{}
        raise AssertionError(purpose)


class EpisodePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.cfg={"paused":False,"hub":None,"remote_publication":False,
            "publication":{"enabled":False},
            "observer":{"enabled":True,"model":"gpt-5.6-luna","start_at":0,
                "settle_seconds":0,"min_interval_seconds":0,"max_calls_per_day":1000},
            "episode_curation":{"policy":"durable_memory","enabled":True,"generation_id":"fixture-generation",
                "settle_seconds":0,"max_events_per_stage":80,
                "max_chars_per_stage":48_000}}
        (self.home/"config.json").write_text(json.dumps(self.cfg))
        self.state=State(self.home);self.runner=FixtureRunner()

    def tearDown(self):
        self.state.close();self.temp.cleanup()

    def source(self, ident, turn, kind, body, created, *, tool_name="", exit_code=None,
               source_role="episode_evidence"):
        with self.state.db:
            self.state.db.execute("""INSERT INTO memories
                (id,session,project,body,kind,created,active,exit_code,turn)
                VALUES(?,?,?,?,?,?,1,?,?)""",
                (ident,"session","fixture",body,kind,created,exit_code,turn))
            self.state.db.execute("""INSERT INTO source_event_metadata
                (source_id,tool_name,source_role,capture_id,event_fields,response_shape,created)
                VALUES(?,?,?,NULL,'[]','null',?)""",
                (ident,tool_name,source_role,created))

    def episode(self, turn, tool_body="Cobalt retry test passed", created=1):
        self.source(turn+"-u",turn,"UserPromptSubmit","Verify the Cobalt retry policy.",created)
        self.source(turn+"-t",turn,"PostToolUse",tool_body,created+1,
                    tool_name="exec_command",exit_code=0)
        self.source(turn+"-s",turn,"Stop","Verification completed.",created+2)
        return turn+"-t"










if __name__ == "__main__":
    unittest.main()
