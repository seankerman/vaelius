from vaelius_test_support.fixtures.state import visible_episode_claims
from vaelius_test_support.fixtures.state import invalidate_source
"""Synthetic reproductions frozen before repairing historical curation.

No private transcript is copied here. Long tool results exercise exact original
offsets, continuation, restart, evidence installation, and withdrawal together.
"""
import json
import unittest
from unittest.mock import patch

import test_episode_pipeline as fixtures
from agenthub.processing.continuous_observer import _whole_turn
from agenthub.processing.durable_memory import packets_for, validate_records
from agenthub.processing.episode_curator import EpisodeError, prepare_episode
from agenthub.processing.episode_pipeline import run_once, generation_status, activate_generation
from agenthub.processing.evidence_references import EvidenceReferences


def sources(body=None):
    common = dict(project='enterprise:fixture', session='synthetic-chat', turn='one',
                  source_role='episode_evidence', occurred_at='2026-09-20T12:00:00Z')
    return [dict(common, id='goal', kind='UserPromptSubmit', body='Verify Cobalt retry.', created=1),
            dict(common, id='execution', kind='PostToolUse',
                 body=body or ('Cobalt retry verification passed.\n' * 8000), created=2,
                 tool_name='exec_command', exit_code=0, actor='principal-1'),
            dict(common, id='final', kind='Stop', body='Cobalt verification completed.', created=3)]


class LargeEvidenceTests(unittest.TestCase):
    def test_every_original_span_survives_bounded_fragments_with_stable_ids(self):
        rows = sources()
        original = prepare_episode(rows, max_chars=1_000_000)
        packets, staged = packets_for(rows, max_chars=8000, max_stages=64,
                                      split_oversized_events=True)
        self.assertTrue(staged)
        self.assertGreater(len(packets), 2)
        full = _whole_turn(packets)
        self.assertEqual([{k:v for k,v in e.items() if k!='source_created'}
                          for e in full['episode']['events']], original['episode']['events'])
        for packet in packets:
            self.assertEqual(packet['episode']['episode_id'], original['episode']['episode_id'])
            self.assertLessEqual(sum(len(s['text']) for e in packet['episode']['events']
                                     for s in e['spans']), 8000)
            self.assertEqual(packet['episode']['objective_event_ids'], ['goal'])
            self.assertEqual(packet['episode']['final_event_ids'], ['final'])
            refs = EvidenceReferences(packet)
            self.assertEqual(refs.events, EvidenceReferences(original).events)
        self.assertEqual(''.join(s['text'] for s in full['episode']['events'][1]['spans']),
                         rows[1]['body'])

    def test_stage_and_anchor_limits_remain_explicit_and_old_policy_stays_frozen(self):
        with self.assertRaisesRegex(EpisodeError, 'episode_event_exceeds_stage_limit'):
            packets_for(sources(), max_chars=8000)
        with self.assertRaisesRegex(EpisodeError, 'memory_stage_count_exceeds_limit'):
            packets_for(sources(), max_chars=8000, max_stages=2, split_oversized_events=True)
        rows = sources(); rows[0]['body'] = 'request ' * 2000
        with self.assertRaisesRegex(EpisodeError, 'episode_anchors_exceed_stage_limit'):
            packets_for(rows, max_chars=8000, split_oversized_events=True)

    def test_unsupported_actor_and_unknown_handle_still_fail_closed(self):
        from agenthub.processing.durable_memory import packet_for
        packet = packet_for(sources('Mira verified the Cobalt retry.'))
        refs = EvidenceReferences(packet); sent = refs.packet(packet)
        event = sent['episode']['events'][1]
        raw = dict(title='Cobalt retry verified', text='Mira verified the Cobalt retry.',
                   subject='Cobalt retry', facets=['activity'], actors=['Mira'],
                   artifact=None, rationale=None, state='observed', occurred_date='',
                   event_id=event['event_id'], evidence_span_ids=[event['spans'][0]['span_id']])
        valid = validate_records({'records':[raw]}, packet, {'execution'}, references=refs)
        self.assertEqual(len(valid['records']), 1)
        for changed in (dict(raw, actors=['Absent person']),
                        dict(raw, evidence_span_ids=['sunknown']),
                        dict(raw, evidence_span_ids=[sent['episode']['events'][0]['spans'][0]['span_id']])):
            result = validate_records({'records':[changed]}, packet, {'execution'}, references=refs)
            self.assertFalse(result['records'])
            self.assertTrue(result['rejections'])


class LargeEpisodePipelineTests(unittest.TestCase):
    tearDown = fixtures.EpisodePipelineTests.tearDown
    source = fixtures.EpisodePipelineTests.source
    episode = fixtures.EpisodePipelineTests.episode

    def setUp(self):
        fixtures.EpisodePipelineTests.setUp(self)
        self.cfg['episode_curation'].update(policy='durable_memory', max_chars_per_stage=2000,
            split_oversized_events=True, evidence_guidance=True, max_stages=64)
        self.cfg['durable_memory_retrieval'] = True
        (self.home/'config.json').write_text(json.dumps(self.cfg))

    def test_restart_installs_early_and_late_spans_without_repeating_curator_calls(self):
        self.episode('first', 'Cobalt retry verification passed.\n' * 150)
        calls = []
        def runner(home, config, instruction, payload, schema):
            if config['_purpose'] != 'durable_memory_curate':
                return self.runner(home, config, instruction, payload, schema)
            self.assertIn('people or speakers', instruction)
            self.assertIn('evidence_span_ids use s', instruction)
            self.assertIn('anchor MUST belong to a cited span', instruction)
            calls.append(payload['episode']['stage_index'])
            event = next(e for e in payload['episode']['events'] if e['kind']=='PostToolUse')
            index = payload['episode']['stage_index']
            return {'records':[dict(title=f'Cobalt retry stage {index}',
                text=f'Cobalt retry verification evidence stage {index}.', subject='Cobalt retry',
                facets=['activity','procedure'], actors=['agent'], artifact=None, rationale=None,
                state='observed', occurred_date='', event_id=event['event_id'],
                evidence_span_ids=[event['spans'][0]['span_id']])]}, {}
        with patch('agenthub.processing.episode_pipeline._install', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_once(self.state, self.cfg, runner)
        self.assertGreater(len(calls), 1)
        self.assertEqual(generation_status(self.state.db,'fixture-generation')['documents'],0)
        before=list(calls)
        run_once(self.state, self.cfg, runner)
        self.assertEqual(calls,before)
        activate_generation(self.state.db,'fixture-generation')
        self.assertEqual(generation_status(self.state.db,'fixture-generation')['documents'],len(calls))
        self.assertTrue(visible_episode_claims(self.state))
        invalidate_source(self.state,'first-t')
        self.assertEqual(visible_episode_claims(self.state),[])

    def test_repeated_completion_is_context_and_empty_later_stages_complete(self):
        self.episode('first', 'Cobalt retry verification passed.\n' * 150)
        calls=[]
        def runner(home,config,instruction,payload,schema):
            if config['_purpose']!='durable_memory_curate':
                return self.runner(home,config,instruction,payload,schema)
            index=payload['episode']['stage_index'];calls.append(index)
            eligible=set(payload['retention_boundary']['eligible_current_event_ids'])
            event=next(e for e in payload['episode']['events'] if e['kind']=='PostToolUse')
            self.assertIn(event['event_id'],eligible)
            anchors=[e for e in payload['episode']['events'] if e['kind'] in ('UserPromptSubmit','Stop')]
            self.assertTrue(anchors)
            if index:
                self.assertFalse(eligible & {e['event_id'] for e in anchors})
                # The final response is readable evidence, but cannot by itself
                # justify emitting the same memory on every transport fragment.
                raw=dict(title='Repeated completion',text='Cobalt verification completed.',
                    subject='Cobalt',facets=['activity'],actors=['agent'],artifact=None,
                    rationale=None,state='reported',occurred_date='',
                    event_id=anchors[-1]['event_id'],evidence_span_ids=[anchors[-1]['spans'][0]['span_id']])
                self.assertNotIn(raw['event_id'],eligible)
            else:self.assertTrue({e['event_id'] for e in anchors}<=eligible)
            return {'records':[],'episode_summary':{'intent':None,'open_work':[]}},{}
        run_once(self.state,self.cfg,runner)
        self.assertGreater(len(calls),1)
        self.assertEqual(generation_status(self.state.db,'fixture-generation')['jobs'],{'no_learning':1})
        activate_generation(self.state.db,'fixture-generation')
        self.assertEqual(visible_episode_claims(self.state),[])

    def test_new_fragment_and_evidence_policies_cannot_change_a_frozen_generation(self):
        from agenthub.processing.episode_pipeline import ensure_generation
        with self.state.db:
            ensure_generation(self.state.db,self.cfg)
        for key in ('split_oversized_events','evidence_guidance','media_policy'):
            changed=json.loads(json.dumps(self.cfg))
            changed['episode_curation'][key]='references-v1' if key=='media_policy' else False
            with self.assertRaisesRegex(ValueError,'episode_generation_definition_changed'):
                ensure_generation(self.state.db,changed)


if __name__ == '__main__':
    unittest.main()
