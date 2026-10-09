import hashlib
import json
from pathlib import Path
import unittest
import subprocess
import sys
import tempfile

from agenthub.processing.durable_memory import packet_for, validate, _source_time
from agenthub.processing.episode_curator import EpisodeError, prepare_episode_stages


class BackendSemanticsTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parent/'fixtures/processing'
        raw = (folder / 'backend_semantics_v2.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), json.loads(
            (folder / 'backend_semantics_v2_manifest.json').read_text())['sha256'])
        self.fixture = json.loads(raw)

    def sources(self):
        return [dict(id=str(i), project='enterprise:synthetic', session='chat', turn='one',
                     created=i, source_order=i, body=text, kind=kind,
                     occurred_at='2026-09-25T10:00:00Z', role=role, channel=channel,
                     actor=role, call_id='', parent_id='')
                for i, (text, kind, role, channel) in enumerate([
                    (self.fixture['repeated_text'], 'UserPromptSubmit', 'user', ''),
                    (self.fixture['repeated_text'], 'AssistantMessage', 'assistant', 'commentary'),
                    (self.fixture['commentary'], 'AssistantMessage', 'assistant', 'commentary'),
                    ('The dataset report is complete.', 'Stop', 'assistant', 'final')])]

    def test_distinct_speakers_survive_equal_text_and_staged_curation(self):
        sources = self.sources()
        packet = packet_for(sources)
        self.assertEqual(len(packet['episode']['events']), 4)
        self.assertEqual(packet['episode']['events'][1]['channel'], 'commentary')
        packets, staged = prepare_episode_stages(sources, max_events=3, max_chars=500)
        self.assertTrue(staged)
        self.assertEqual(set(e['event_id'] for p in packets for e in p['episode']['events']),
                         {s['id'] for s in sources})

    def test_commentary_is_reported_and_reason_preserves_speaker(self):
        packet = packet_for(self.sources()); event = packet['episode']['events'][2]
        record = dict(title='Maple location', text=self.fixture['commentary'],
                      subject='Maple', facets=['artifact', 'decision'], actors=['agent'],
                      artifact_name='review.csv', location='/tmp/review.csv',
                      reason_actor='agent', reason_quote='because the review uses CSV.',
                      state='reported', occurred_date='', event_id=event['event_id'],
                      evidence_span_ids=[event['spans'][0]['span_id']])
        result = validate({'records': [record]}, packet, {'2'})['records'][0]
        self.assertEqual(result['attribution'], 'agent_reported')
        with self.assertRaisesRegex(EpisodeError, 'memory_reason_speaker'):
            validate({'records': [dict(record, reason_actor='user', actors=['user'])]}, packet, {'2'})
        user = packet['episode']['events'][0]
        incorrect = dict(record, event_id='0', actors=['agent'], location='/tmp/maple.csv',
                         artifact_name='maple.csv', reason_quote='because the CSV header is stable.',
                         evidence_span_ids=[user['spans'][0]['span_id']])
        with self.assertRaisesRegex(EpisodeError, 'memory_reason_speaker'):
            validate({'records': [incorrect]}, packet, {'0'})

    def test_nonfinite_original_time_is_unknown(self):
        for value in ('nan', 'inf', '-inf', 'unknown'):
            self.assertIsNone(_source_time({'project': 'enterprise:synthetic', 'occurred_at': value}))

    def test_enterprise_cli_cannot_launch_legacy_model_or_corpus_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);(home/'config.json').write_text(json.dumps({'knowledge_backend':{'mode':'enterprise_local'}}))
            for command in (['view'],['semantic-setup'],['semantic-serve'],['backfill-codex'],['queue-public','unused.json']):
                result=subprocess.run([sys.executable,'-m','agentclient.cli','--home',str(home),*command],capture_output=True,text=True,timeout=10)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('invalid choice',result.stderr)
            self.assertFalse((home/'client.sqlite').exists())

    def test_resume_index_keeps_original_speaker_channel_and_time(self):
        from agenthub.processing.continuous_observer import durable_continuation, _validation_packet
        from agenthub.processing.evidence_references import EvidenceReferences
        sources=self.sources();sources[1]['actor']='Bob'
        prior=packet_for(sources)
        current=packet_for([dict(s,id=s['id']+'-next',turn='next') for s in self.sources()])
        refs=EvidenceReferences(_validation_packet(current,prior))
        payload=durable_continuation(current,prior,refs)
        speaker=next(e for e in payload['previous_evidence_index'] if e.get('actor')=='Bob')
        self.assertEqual(speaker['role'],'assistant');self.assertEqual(speaker['channel'],'commentary')
        self.assertEqual(speaker['occurred_at'],'2026-09-25T10:00:00Z')
