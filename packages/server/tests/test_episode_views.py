from vaelius_test_support.fixtures.state import invalidate_source
"""M2 behavior fixtures: derived context retains original evidence and coverage."""
import json
import unittest
from unittest.mock import patch

import test_episode_pipeline as base
from agenthub.processing.episode_pipeline import (
    activate_generation, backfill_episode_revisions, correct_episode_extraction,
    episode_links_for_document,
    get_episode_view, run_once,
    session_overview, _order_episode_links, retry_held_job,
)
from agenthub.processing.knowledge import apply_resolved_observation
from vaelius_test_support.fixtures.state import State


class StageRunner(base.FixtureRunner):
    def __call__(self, home, config, instruction, payload, schema):
        if config['_purpose'] != 'durable_memory_curate':
            return super().__call__(home, config, instruction, payload, schema)
        self.calls.append(config['_purpose'])
        self.episode_ids = getattr(self, 'episode_ids', []) + [payload['episode']['episode_id']]
        event = next(e for e in payload['episode']['events'] if e['kind'] == 'PostToolUse')
        body = event['spans'][0]['text']
        subject = 'Willow dataset' if 'Willow' in body else 'Cobalt retry'
        artifact = ({'name': 'Willow dataset', 'location': 'datasets/willow.csv'}
                    if 'datasets/willow.csv' in body else None)
        text = ('Saved Willow dataset at datasets/willow.csv.' if artifact else
                'Cobalt retry verification passed.')
        return {'records': [dict(title=subject, text=text, subject=subject,
            facets=['activity'], actors=['agent'], artifact=artifact,
            rationale=None, state='observed', occurred_date='',
            event_id=event['event_id'], evidence_span_ids=[event['spans'][0]['span_id']])]}, {}


class OutcomeRunner(base.FixtureRunner):
    def __call__(self, home, config, instruction, payload, schema):
        if config['_purpose'] != 'durable_memory_curate':
            return super().__call__(home, config, instruction, payload, schema)
        self.calls.append(config['_purpose'])
        event = next(e for e in payload['episode']['events'] if e['kind'] == 'PostToolUse')
        failed = event['exit_code'] != 0
        text = 'Cobalt retry command failed.' if failed else 'Cobalt retry command passed.'
        return {'records': [dict(title='Cobalt retry command', text=text,
            subject='Cobalt retry', facets=['activity'], actors=['agent'],
            artifact=None, rationale=None, state='attempted' if failed else 'observed',
            occurred_date='', event_id=event['event_id'],
            evidence_span_ids=[event['spans'][0]['span_id']])]}, {}


class EpisodeViewFixtures(unittest.TestCase):
    tearDown = base.EpisodePipelineTests.tearDown
    source = base.EpisodePipelineTests.source
    episode = base.EpisodePipelineTests.episode

    def setUp(self):
        base.EpisodePipelineTests.setUp(self)
        self.cfg['episode_curation']['policy'] = 'durable_memory'
        self.save_config()
        self.runner = StageRunner()

    def save_config(self):
        (self.home / 'config.json').write_text(json.dumps(self.cfg))

    def test_episode_view_is_linked_and_withdrawal_removes_dependent_assertion(self):
        self.episode('first')
        run_once(self.state, self.cfg, self.runner)
        self.assertIsNone(get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'first'))
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'first')
        self.assertTrue(view['handle'].startswith('ep_'))
        self.assertEqual(view['completion'], 'complete')
        self.assertEqual(view['source_turn'], 'first')
        self.assertEqual(len(view['assertions']), 1)
        self.assertEqual(view['assertions'][0]['evidence'][0]['source_id'], 'first-t')
        self.assertEqual(view['assertions'][0]['state'], 'observed')
        self.assertIn('Cobalt retry', view['summary'])
        self.assertNotIn('Cobalt retry test passed', json.dumps(view['source_range']))
        invalidate_source(self.state,'first-t')
        after = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'first')
        self.assertIsNone(after)  # No stale summary or detail survives source withdrawal.

    def test_history_keeps_original_episode_after_claim_correction(self):
        self.episode('original')
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'fixture-generation')
        before = get_episode_view(self.state.db, 'fixture-generation', 'fixture',
                                  'session', 'original')
        document_id = before['assertions'][0]['document_id']
        original_revision = before['assertions'][0]['revision_id']
        first = get_episode_view(self.state.db, 'fixture-generation', 'fixture',
                                 'session', 'original', mode='history')
        self.assertEqual(first['assertions'][0]['revision_id'], original_revision)
        self.assertIsNone(get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','original',mode='history',
            source_authorizer=lambda source_id: False))
        self.episode('correction', 'Corrected Cobalt retry test passed', created=10)
        source = self.state.db.execute('SELECT body FROM memories WHERE id=?',
                                       (before['assertions'][0]['candidate_id'],)).fetchone()
        observation = json.loads(source['body'])
        observation.pop('memory_context', None)
        observation['lesson'] = 'The corrected Cobalt retry test passed.'
        observation['evidence'] = [{'source_id': 'correction-t',
                                    'segment_id': 'corrected-span'}]
        with self.state.db:
            self.state.db.execute('''INSERT INTO memories
                (id,session,project,body,kind,created,turn)
                VALUES(?,?,?,?,?,?,?)''',
                ('manual-correction','session','fixture',json.dumps(observation),
                 'KnowledgeCandidate',20,'correction'))
            self.state.db.execute('INSERT INTO observation_sources VALUES(?,?)',
                                  ('manual-correction','correction-t'))
            corrected=apply_resolved_observation(self.state.db,'manual-correction',
                'fixture','session',observation,'CORRECT',document_id)
        self.assertNotEqual(corrected['revision_id'],original_revision)
        self.assertIsNone(get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','original'))
        history=get_episode_view(self.state.db,'fixture-generation','fixture',
                                  'session','original',mode='history')
        self.assertEqual(history['assertions'][0]['revision_id'],original_revision)
        self.assertEqual(history['assertions'][0]['claim_status'],'corrected_claim')
        self.assertIn('[observed; corrected_claim]',history['summary'])
        self.assertEqual(history['assertions'][0]['evidence'][0]['source_id'],
                         'original-t')
        self.assertEqual(history['occurrence_id'],first['occurrence_id'])
        self.assertEqual(history['curated_revision_id'],first['curated_revision_id'])
        overview=session_overview(self.state.db,'fixture-generation','fixture',
                                  'session',mode='history')
        self.assertIn(history['occurrence_id'],
                      [item['occurrence_id'] for item in overview['episodes']])
        links=episode_links_for_document(self.state.db,'fixture-generation',
                                         'fixture',document_id,mode='history')
        self.assertEqual(links['episodes'][0]['occurrence_id'],history['occurrence_id'])
        invalidate_source(self.state,'original-t')
        self.assertIsNone(get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','original',mode='history'))

    def test_history_retains_distinct_duplicate_occurrence_without_new_claim(self):
        self.episode('first')
        self.episode('second', created=10)
        def duplicate_runner(home, config, instruction, payload, schema):
            if config['_purpose']=='episode_resolve' and payload['active_artifacts']:
                return {'candidate_key':payload['candidate']['candidate_key'],
                        'operation':'IGNORE','target_artifact_id':'',
                        'reason':'unsupported_or_redundant'}, {}
            return self.runner(home,config,instruction,payload,schema)
        run_once(self.state,self.cfg,duplicate_runner)
        run_once(self.state,self.cfg,duplicate_runner)
        activate_generation(self.state.db,'fixture-generation')
        self.assertEqual(self.state.db.execute(
            'SELECT count(*) FROM knowledge_documents').fetchone()[0],1)
        self.assertIsNone(get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','second'))
        first=get_episode_view(self.state.db,'fixture-generation','fixture',
                               'session','first',mode='history')
        second=get_episode_view(self.state.db,'fixture-generation','fixture',
                                'session','second',mode='history')
        self.assertNotEqual(first['occurrence_id'],second['occurrence_id'])
        self.assertEqual(second['assertions'][0]['claim_status'],'no_generalized_claim')
        self.assertEqual(second['assertions'][0]['evidence'][0]['source_id'],'second-t')
        self.assertEqual(self.state.db.execute(
            'SELECT count(*) FROM episode_revisions').fetchone()[0],2)
        self.assertFalse(run_once(self.state,self.cfg,duplicate_runner))
        self.assertEqual(self.state.db.execute(
            'SELECT count(*) FROM episode_revisions').fetchone()[0],2)
        with self.assertRaisesRegex(ValueError,'episode_claim_correction_required'):
            correct_episode_extraction(self.state.db,'fixture-generation','fixture',
                'session','first',expected_revision_id=first['curated_revision_id'],
                atom_key=first['assertions'][0]['atom_key'],reviewer='fixture-reviewer',
                reason='incorrect extraction')
        revised=correct_episode_extraction(self.state.db,'fixture-generation','fixture',
            'session','second',expected_revision_id=second['curated_revision_id'],
            atom_key=second['assertions'][0]['atom_key'],reviewer='fixture-reviewer',
            reason='incorrect extraction')
        audit=self.state.db.execute('''SELECT previous_revision_id,reason
            FROM episode_revisions WHERE revision_id=?''',(revised,)).fetchone()
        self.assertEqual(audit['previous_revision_id'],second['curated_revision_id'])
        self.assertIn('extraction_correction',audit['reason'])
        self.assertIsNone(get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','second',mode='history'))

    def test_retained_source_backfill_recovers_prior_validated_episode(self):
        self.episode('old')
        run_once(self.state,self.cfg,self.runner)
        activate_generation(self.state.db,'fixture-generation')
        with self.state.db:
            self.state.db.execute('DELETE FROM episode_revision_claim_links')
            self.state.db.execute('DELETE FROM episode_revision_evidence')
            self.state.db.execute('DELETE FROM episode_revisions')
            self.state.db.execute('DELETE FROM episode_occurrences')
        self.assertIsNone(get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','old',mode='history'))
        result=backfill_episode_revisions(self.state.db,'fixture-generation',
            project='fixture',limit=1)
        self.assertEqual(result['revisions_created'],1)
        self.assertEqual(result['atoms_without_current_evidence'],0)
        history=get_episode_view(self.state.db,'fixture-generation',
            'fixture','session','old',mode='history')
        self.assertEqual(history['assertions'][0]['evidence'][0]['source_id'],'old-t')
        self.assertEqual(self.state.db.execute('SELECT reason FROM episode_revisions').fetchone()[0],
                         'retained_source_backfill')
        self.assertEqual(backfill_episode_revisions(self.state.db,'fixture-generation',
            project='fixture')['revisions_created'],0)

    def test_cited_intent_and_open_work_are_linked_without_extra_call(self):
        self.source('goal-u', 'goal', 'UserPromptSubmit',
                    'Verify the Cobalt retry policy because requests fail.', 1)
        self.source('goal-t', 'goal', 'PostToolUse', 'Cobalt retry test passed.', 2,
                    tool_name='exec_command', exit_code=0)
        self.source('goal-s', 'goal', 'Stop',
                    'Verification passed. Next, inspect production telemetry.', 3)
        def summary_runner(home, config, instruction, payload, schema):
            result, usage = self.runner(home, config, instruction, payload, schema)
            if config['_purpose'] == 'durable_memory_curate':
                user = next(e for e in payload['episode']['events']
                            if e['kind'] == 'UserPromptSubmit')
                stop = next(e for e in payload['episode']['events'] if e['kind'] == 'Stop')
                result['episode_summary'] = {
                    'intent': {'text': 'Verify Cobalt retry behavior.',
                               'quote': 'Verify the Cobalt retry policy',
                               'evidence_span_ids': [user['spans'][0]['span_id']]},
                    'open_work': [{'text': 'Inspect production telemetry.',
                                   'quote': 'Next, inspect production telemetry.',
                                   'actor': 'agent',
                                   'evidence_span_ids': [stop['spans'][0]['span_id']]}]}
            return result, usage
        run_once(self.state, self.cfg, summary_runner)
        self.assertEqual(self.runner.calls.count('durable_memory_curate'), 1)
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'goal')
        self.assertEqual(view['intent']['text'], 'Verify Cobalt retry behavior.')
        self.assertEqual(view['intent']['evidence'][0]['source_id'], 'goal-u')
        self.assertEqual(view['open_work'][0]['actor'], 'agent')
        self.assertEqual(view['open_work'][0]['evidence'][0]['source_id'], 'goal-s')
        invalidate_source(self.state,'goal-s')
        after = get_episode_view(self.state.db, 'fixture-generation',
                                 'fixture', 'session', 'goal')
        self.assertEqual(after['open_work'], [])
        self.assertEqual(after['completion'], 'partial')

    def test_fabricated_summary_quote_is_a_reported_gap(self):
        self.episode('quote')
        def bad_summary(home, config, instruction, payload, schema):
            result, usage = self.runner(home, config, instruction, payload, schema)
            if config['_purpose'] == 'durable_memory_curate':
                user = next(e for e in payload['episode']['events']
                            if e['kind'] == 'UserPromptSubmit')
                result['episode_summary'] = {'intent': {'text': 'Migrate the company database.',
                    'quote': 'Migrate the company database.',
                    'evidence_span_ids': [user['spans'][0]['span_id']]}, 'open_work': []}
            return result, usage
        run_once(self.state, self.cfg, bad_summary)
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'quote')
        self.assertIsNone(view['intent'])
        self.assertIn('summary_assertion_rejected', view['coverage_gaps'])
        self.assertEqual(len(view['assertions']), 1)

    def test_session_overview_preserves_separate_topics_and_pagination(self):
        self.episode('retry')
        self.source('willow-u', 'willow', 'UserPromptSubmit', 'Save Willow dataset.', 10)
        self.source('willow-t', 'willow', 'PostToolUse',
                    'Saved Willow dataset at datasets/willow.csv.', 11,
                    tool_name='exec_command', exit_code=0)
        self.source('willow-s', 'willow', 'Stop', 'Saved Willow dataset.', 12)
        run_once(self.state, self.cfg, self.runner)
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'fixture-generation')
        first = session_overview(self.state.db, 'fixture-generation', 'fixture', 'session', limit=1)
        self.assertEqual(len(first['episodes']), 1)
        self.assertTrue(first['has_more'])
        second = session_overview(self.state.db, 'fixture-generation', 'fixture', 'session',
                                  limit=1, offset=1)
        self.assertEqual(len(second['episodes']), 1)
        self.assertNotEqual(first['episodes'][0]['source_turn'], second['episodes'][0]['source_turn'])
        self.assertEqual(second['has_more'], False)

    def test_document_links_use_all_original_supporting_episodes(self):
        self.episode('first')
        self.episode('second', created=10)
        run_once(self.state, self.cfg, self.runner)
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'fixture-generation')
        first = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'first')
        document = first['assertions'][0]['document_id']
        revision = first['assertions'][0]['revision_id']
        with self.state.db:
            self.state.db.execute('INSERT INTO knowledge_support VALUES(?,?,?,\'supports\',\'unknown\',?)',
                (revision, 'second-t', 'second-support-span', 15))
        links = episode_links_for_document(self.state.db, 'fixture-generation',
                                           'fixture', document)
        self.assertEqual({x['source_turn'] for x in links['episodes']}, {'first', 'second'})
        self.assertTrue(all(x['session'] == 'session' and x['handle'].startswith('ep_')
                            for x in links['episodes']))
        invalidate_source(self.state,'second-t')
        links = episode_links_for_document(self.state.db, 'fixture-generation',
                                           'fixture', document)
        self.assertEqual([x['source_turn'] for x in links['episodes']], ['first'])

    def test_long_episode_stages_resume_with_original_handles_and_explicit_coverage(self):
        self.cfg['episode_curation']['max_events_per_stage'] = 4
        self.save_config()
        self.source('long-u', 'long', 'UserPromptSubmit', 'Inspect Cobalt and Willow.', 1)
        for i in range(5):
            body = ('Saved Willow dataset at datasets/willow.csv.' if i == 4 else
                    f'Cobalt retry test passed on shard {i}')
            self.source(f'long-t{i}', 'long', 'PostToolUse', body, 2+i,
                        tool_name='exec_command', exit_code=0)
        self.source('long-s', 'long', 'Stop', 'Inspection completed.', 10)
        count = 0
        def interrupt_after_first(*args):
            nonlocal count
            if args[1]['_purpose'] == 'durable_memory_curate':
                count += 1
                if count == 2:
                    raise KeyboardInterrupt
            return self.runner(*args)
        with self.assertRaises(KeyboardInterrupt):
            run_once(self.state, self.cfg, interrupt_after_first)
        self.assertEqual(self.runner.calls.count('durable_memory_curate'), 1)
        self.state.close(); self.state = State(self.home)
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls.count('durable_memory_curate'), 3)
        job_episode_id = self.state.db.execute(
            'SELECT episode_id FROM curation_episode_jobs').fetchone()[0]
        self.assertEqual(self.runner.episode_ids, [job_episode_id] * 3)
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'long')
        self.assertEqual(view['stage_count'], 3)
        self.assertEqual(view['completion'], 'complete')
        self.assertEqual({e['source_id'] for a in view['assertions'] for e in a['evidence']},
                         {'long-t0', 'long-t2', 'long-t4'})
        self.assertEqual(len([a for a in view['assertions'] if a['subject'] == 'Cobalt retry']), 1)
        self.assertIn('datasets/willow.csv', view['summary'])

    def test_oversized_event_is_a_processing_gap_without_model_dispatch(self):
        self.cfg['episode_curation']['max_chars_per_stage'] = 1000
        self.save_config()
        self.source('huge-u', 'huge', 'UserPromptSubmit', 'Inspect Cobalt.', 1)
        self.source('huge-t', 'huge', 'PostToolUse', 'Cobalt ' * 500, 2,
                    tool_name='exec_command', exit_code=0)
        self.source('huge-s', 'huge', 'Stop', 'Inspection completed.', 3)
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls, [])
        row = self.state.db.execute('SELECT error,status FROM curation_episode_jobs').fetchone()
        self.assertEqual(row['error'], 'episode_event_exceeds_stage_limit')
        self.assertEqual(row['status'], 'pending')

    def test_failed_then_successful_steps_keep_both_states_and_sources(self):
        self.cfg['episode_curation']['max_events_per_stage'] = 3
        self.save_config()
        self.source('fix-u', 'fix', 'UserPromptSubmit', 'Repair Cobalt retry.', 1)
        self.source('fix-failed', 'fix', 'PostToolUse', 'Cobalt retry command failed.', 2,
                    tool_name='exec_command', exit_code=1)
        self.source('fix-success', 'fix', 'PostToolUse', 'Cobalt retry command passed.', 3,
                    tool_name='exec_command', exit_code=0)
        self.source('fix-s', 'fix', 'Stop', 'Repair completed.', 4)
        runner = OutcomeRunner()
        run_once(self.state, self.cfg, runner)
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'fix')
        self.assertEqual({a['state'] for a in view['assertions']}, {'attempted', 'observed'})
        self.assertEqual({e['source_id'] for a in view['assertions'] for e in a['evidence']},
                         {'fix-failed', 'fix-success'})

    def test_reason_far_from_decision_keeps_user_attribution_and_both_sources(self):
        self.cfg['episode_curation']['max_events_per_stage'] = 3
        self.save_config()
        self.source('reason-u', 'reason', 'UserPromptSubmit',
                    'Use Cobalt retries because failures are transient.', 1)
        for index in range(5):
            self.source(f'reason-noise-{index}', 'reason', 'PostToolUse',
                        f'Unrelated diagnostic output {index}.', 2+index,
                        tool_name='exec_command', exit_code=0)
        self.source('reason-t', 'reason', 'PostToolUse',
                    'Cobalt retry test passed.', 8, tool_name='exec_command', exit_code=0)
        self.source('reason-s', 'reason', 'Stop', 'Cobalt retry selected.', 9)
        def decision_runner(home, config, instruction, payload, schema):
            if config['_purpose'] != 'durable_memory_curate':
                return self.runner(home, config, instruction, payload, schema)
            events = payload['episode']['events']
            tool = next(e for e in events if e['kind'] == 'PostToolUse')
            if 'Cobalt retry test passed' not in tool['spans'][0]['text']:
                return {'records': []}, {}
            user = next(e for e in events if e['kind'] == 'UserPromptSubmit')
            return {'records': [dict(title='Cobalt retry decision',
                text='Cobalt retries were selected after a passing test.',
                subject='Cobalt retries', facets=['decision', 'activity'],
                actors=['user', 'agent'], artifact=None,
                rationale={'actor': 'user', 'quote': 'because failures are transient'},
                state='observed', occurred_date='', event_id=tool['event_id'],
                evidence_span_ids=[user['spans'][0]['span_id'],
                                   tool['spans'][0]['span_id']])]}, {}
        run_once(self.state, self.cfg, decision_runner)
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'reason')
        self.assertEqual(view['assertions'][0]['rationale']['actor'], 'user')
        self.assertEqual({e['source_id'] for e in view['assertions'][0]['evidence']},
                         {'reason-u', 'reason-t'})

    def test_withdrawing_one_source_keeps_independent_topic_and_marks_gap(self):
        self.cfg['episode_curation']['max_events_per_stage'] = 3
        self.save_config()
        self.source('mixed-u', 'mixed', 'UserPromptSubmit', 'Check retry and save Willow.', 1)
        self.source('mixed-c', 'mixed', 'PostToolUse', 'Cobalt retry test passed.', 2,
                    tool_name='exec_command', exit_code=0)
        self.source('mixed-w', 'mixed', 'PostToolUse',
                    'Saved Willow dataset at datasets/willow.csv.', 3,
                    tool_name='exec_command', exit_code=0)
        self.source('mixed-s', 'mixed', 'Stop', 'Both tasks completed.', 4)
        run_once(self.state, self.cfg, self.runner)
        activate_generation(self.state.db, 'fixture-generation')
        self.assertEqual(len(get_episode_view(self.state.db, 'fixture-generation',
            'fixture', 'session', 'mixed')['assertions']), 2)
        invalidate_source(self.state,'mixed-c')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session', 'mixed')
        self.assertEqual(view['completion'], 'partial')
        self.assertEqual([a['subject'] for a in view['assertions']], ['Willow dataset'])
        self.assertIn('source_withdrawn_or_revised', view['coverage_gaps'])

    def test_incomplete_turn_and_retrieved_echo_do_not_become_summaries(self):
        self.source('partial-u', 'partial', 'UserPromptSubmit', 'Check Cobalt retry.', 1)
        self.source('partial-t', 'partial', 'PostToolUse', 'Cobalt retry test passed.', 2,
                    tool_name='exec_command', exit_code=0)
        self.source('echo-u', 'echo', 'UserPromptSubmit', 'Remember Cobalt retry.', 4)
        self.source('echo-t', 'echo', 'PostToolUse', 'Copied memory: Cobalt retry passed.', 5,
                    tool_name='read_thread', source_role='retrieved_memory')
        self.source('echo-s', 'echo', 'Stop', 'Nothing independently checked.', 6)
        def no_echo_runner(home, config, instruction, payload, schema):
            if config['_purpose'] == 'durable_memory_curate':
                self.assertFalse(any(e['kind'] == 'PostToolUse'
                                     for e in payload['episode']['events']))
                return {'records': []}, {}
            return self.runner(home, config, instruction, payload, schema)
        run_once(self.state, self.cfg, no_echo_runner)
        self.assertEqual(self.runner.calls, [])
        self.assertIsNone(get_episode_view(self.state.db, 'fixture-generation',
            'fixture', 'session', 'partial', include_building=True))
        self.assertIsNone(get_episode_view(self.state.db, 'fixture-generation',
            'fixture', 'session', 'echo', include_building=True))

    def test_stage_limit_is_explicit_before_dispatch(self):
        self.cfg['episode_curation'].update(max_events_per_stage=3, max_stages=2)
        self.save_config()
        self.source('bounded-u', 'bounded', 'UserPromptSubmit', 'Inspect all shards.', 1)
        for index in range(3):
            self.source(f'bounded-{index}', 'bounded', 'PostToolUse',
                        f'Cobalt retry test passed on shard {index}', 2+index,
                        tool_name='exec_command', exit_code=0)
        self.source('bounded-s', 'bounded', 'Stop', 'Inspection completed.', 9)
        run_once(self.state, self.cfg, self.runner)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state.db.execute(
            'SELECT error FROM curation_episode_jobs').fetchone()[0],
            'memory_stage_count_exceeds_limit')

    def test_reducer_bound_is_an_explicit_resumable_gap(self):
        self.cfg['episode_curation'].update(max_events_per_stage=3,
                                            max_reducer_chars=32_000)
        self.save_config()
        self.source('reduce-u', 'reduce', 'UserPromptSubmit', 'Inspect many shards.', 1)
        for index in range(30):
            self.source(f'reduce-{index}', 'reduce', 'PostToolUse',
                        f'Cobalt retry test passed on shard {index}', 2+index,
                        tool_name='exec_command', exit_code=0)
        self.source('reduce-s', 'reduce', 'Stop', 'Inspection completed.', 40)
        class VerboseRunner(StageRunner):
            def __call__(self, *args):
                result = super().__call__(*args)
                if args[1]['_purpose'] == 'durable_memory_curate':
                    result[0]['records'][0]['text'] = 'Cobalt retry passed. ' * 45
                return result
        runner = VerboseRunner()
        run_once(self.state, self.cfg, runner)
        row = self.state.db.execute('SELECT error,progress FROM curation_episode_jobs').fetchone()
        self.assertEqual(row['error'], 'memory_reducer_exceeds_limit')
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0], 0)
        previous_calls = len(runner.calls)
        with self.state.db:
            self.state.db.execute('UPDATE curation_episode_jobs SET next_attempt=0')
        run_once(self.state, self.cfg, runner)
        self.assertEqual(len(runner.calls), previous_calls)

    def test_episode_summary_has_bounded_text_with_explicit_more(self):
        self.cfg['episode_curation']['max_events_per_stage'] = 3
        self.save_config()
        self.source('summary-u', 'summary', 'UserPromptSubmit', 'Inspect several topics.', 1)
        for index in range(4):
            self.source(f'summary-{index}', 'summary', 'PostToolUse',
                        f'Cobalt retry test passed on shard {index}', 2+index,
                        tool_name='exec_command', exit_code=0)
        self.source('summary-s', 'summary', 'Stop', 'Inspection completed.', 9)
        class LongTextRunner(StageRunner):
            def __call__(self, *args):
                result = super().__call__(*args)
                if args[1]['_purpose'] == 'durable_memory_curate':
                    result[0]['records'][0]['text'] = 'Cobalt retry passed. ' * 45
                    result[0]['records'][0]['title'] += str(len(self.calls))
                return result
        run_once(self.state, self.cfg, LongTextRunner())
        activate_generation(self.state.db, 'fixture-generation')
        view = get_episode_view(self.state.db, 'fixture-generation', 'fixture', 'session',
                                'summary', limit=20)
        self.assertLessEqual(len(view['summary']), 2400)
        self.assertTrue(view['summary_has_more'])
        self.assertEqual(len(view['assertions']), 4)

    def test_timeline_orders_known_event_days_before_capture_only_entries(self):
        entries=[
            {'handle':'later','known_event_day':'2026-09-20','source_range':{'captured_at_start':1}},
            {'handle':'unknown','known_event_day':None,'source_range':{'captured_at_start':0}},
            {'handle':'earlier','known_event_day':'2026-09-10','source_range':{'captured_at_start':2}},
        ]
        ordered=_order_episode_links(entries)
        self.assertEqual([row['handle'] for row in ordered],['earlier','later','unknown'])

    def test_rejected_model_references_need_explicit_fresh_retry(self):
        self.source('retry-u','retry','UserPromptSubmit','Verify Cobalt retry.',1)
        self.source('retry-t','retry','PostToolUse','Cobalt retry test passed',2,
                    tool_name='exec_command',exit_code=0)
        self.source('retry-s','retry','Stop','Verification done.',3)
        class InvalidOnce(StageRunner):
            def __init__(self):
                super().__init__();self.curations=0
            def __call__(self,*args):
                output,usage=super().__call__(*args)
                if args[1]['_purpose']=='durable_memory_curate':
                    self.curations+=1
                    if self.curations==1:
                        output['records'][0]['evidence_span_ids']=['unknown-handle']
                return output,usage
        runner=InvalidOnce()
        run_once(self.state,self.cfg,runner)
        row=self.state.db.execute('SELECT status,error,progress FROM curation_episode_jobs').fetchone()
        self.assertEqual((row['status'],row['error']),('pending','memory_all_candidates_rejected'))
        self.assertEqual(len(json.loads(row['progress']).get('failed_stage_outputs',[])),1)
        for _ in range(2):
            with self.state.db:
                self.state.db.execute('UPDATE curation_episode_jobs SET next_attempt=0')
            run_once(self.state,self.cfg,runner)
        row=self.state.db.execute('SELECT id,status FROM curation_episode_jobs').fetchone()
        self.assertEqual(row['status'],'held')
        self.assertEqual(runner.curations,1)
        retry_held_job(self.state.db,row['id'])
        run_once(self.state,self.cfg,runner)
        row=self.state.db.execute('SELECT status,error FROM curation_episode_jobs').fetchone()
        self.assertEqual(row['status'],'done')
        self.assertEqual(runner.curations,2)


if __name__ == '__main__':
    unittest.main()
