from vaelius_test_support.fixtures.state import invalidate_source
'Frozen synthetic effective-at versus known-at regression for H3.'
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from agenthub.processing.knowledge import ingest_observation
from vaelius_test_support.fixtures.state import State
from agenthub.processing.temporal import record_assertion, select_assertions
from agenthub.processing.temporal_retrieval import historical_cards, recheck_card
from agentclient.enterprise_contract import VERSION, validate_search

def when(day):
    return datetime(2026, 9, day, 12, tzinfo=timezone.utc).timestamp()

class KnownAtTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        home = Path(self.temp.name)
        (home / 'config.json').write_text(json.dumps({'observer': {'enabled': False}}))
        self.state = State(home)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def add(self, name, location, *, effective_day, recorded_day, old_assertion=None):
        db = self.state.db
        source = 'source-' + name
        observation = {'title': name, 'lesson': f'Dataset location is {location}', 'knowledge_type': 'fact', 'subjects': ['dataset'], 'domain': 'private_memory', 'evidence_status': 'execution_result'}
        with db:
            db.execute('INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)', (source, 'synthetic-session', 'synthetic-project', f'Dataset moved to {location} effective 2026-09-{effective_day:02d}; ongoing.', 'UserPromptSubmit', when(recorded_day)))
            db.execute('INSERT INTO memories(id,session,project,body,kind,created) VALUES(?,?,?,?,?,?)', (name, 'curation-session', 'synthetic-project', json.dumps(observation), 'Observation', when(recorded_day)))
            db.execute('INSERT INTO observation_sources VALUES(?,?)', (name, source))
            doc = ingest_observation(db, name, 'synthetic-project', 'curation-session', {**observation, 'evidence': [{'source_id': source}]}, force_new=True)
            value = record_assertion(db, revision_id=doc['revision_id'], subject='dataset', predicate='location', value=location, actor='agent', validity={'from': f'2026-09-{effective_day:02d}', 'to_status': 'ongoing', 'precision': 'day', 'timezone': 'UTC', 'basis': 'explicit_source'}, evidence_source_ids=[source], recorded_at=when(recorded_day), change={'relation': 'supersedes', 'assertion_id': old_assertion, 'reviewed': True} if old_assertion else None)
        return (value['assertion_id'], source)

    def values(self, *, known_at=None):
        result = select_assertions(self.state.db, 'synthetic-project', subject='dataset', predicate='location', as_of='2026-09-10T12:00:00Z', known_at=known_at)
        return (result, [item['value'] for item in result['assertions']])

    def test_late_import_changes_effective_answer_but_not_earlier_knowledge(self):
        old, _ = self.add('first', '/data/old', effective_day=1, recorded_day=1)
        self.add('second', '/data/new', effective_day=5, recorded_day=20, old_assertion=old)
        effective, values = self.values()
        self.assertEqual(effective['status'], 'supported')
        self.assertEqual(values, ['/data/new'])
        known, values = self.values(known_at='2026-09-10T12:00:00Z')
        self.assertEqual(known['status'], 'supported')
        self.assertEqual(values, ['/data/old'])
        _, values = self.values(known_at='2026-09-25T12:00:00Z')
        self.assertEqual(values, ['/data/new'])

    def test_known_at_does_not_restore_withdrawn_source_today(self):
        _, source = self.add('first', '/data/old', effective_day=1, recorded_day=1)
        invalidate_source(self.state, source)
        _, values = self.values(known_at='2026-09-10T12:00:00Z')
        self.assertEqual(values, [])

    def test_known_at_requires_a_timezone(self):
        with self.assertRaisesRegex(ValueError, 'requires_offset'):
            self.values(known_at='2026-09-10T12:00:00')

    def test_question_card_and_recheck_preserve_known_cutoff(self):
        old, _ = self.add('first', '/data/old', effective_day=1, recorded_day=1)
        self.add('second', '/data/new', effective_day=5, recorded_day=20, old_assertion=old)
        cards = historical_cards(self.state, 'Where was dataset as of 2026-09-10?', 'synthetic-project', reference_clock=datetime(2026, 9, 25, tzinfo=timezone.utc), known_at='2026-09-10T12:00:00Z')
        self.assertEqual([card['item']['assertion_id'] for card in cards], [old])
        self.assertIn('Known by', cards[0]['card']['text'])
        self.assertEqual(recheck_card(self.state, cards[0], 'synthetic-project')['item']['assertion_id'], old)

    def test_explicit_request_mode_uses_same_day_for_effective_and_known_cutoffs(self):
        request = {'version': VERSION, 'query': 'Where was dataset as of 2026-09-10?', 'project': 'synthetic-project', 'as_of': '2026-09-10', 'time_mode': 'known_at'}
        self.assertEqual(validate_search(request), request)
        with self.assertRaises(ValueError):
            validate_search({**request, 'time_mode': 'unknown'})
        with self.assertRaises(ValueError):
            validate_search({k: v for k, v in request.items() if k != 'as_of'})
        old, _ = self.add('first', '/data/old', effective_day=1, recorded_day=1)
        self.add('second', '/data/new', effective_day=5, recorded_day=20, old_assertion=old)
        cards = historical_cards(self.state, request['query'], request['project'], reference_clock=datetime(2026, 9, 25, tzinfo=timezone.utc), time_mode=request['time_mode'])
        self.assertEqual([card['item']['assertion_id'] for card in cards], [old])

    def test_cloud_mcp_forwards_known_at_mode_and_date(self):
        from agenthub.mcp_tools import MemoryTools, enterprise_tools
        schema = next((item for item in enterprise_tools() if item['name'] == 'search_memory'))['inputSchema']
        self.assertEqual(schema['properties']['time_mode']['enum'], ['current', 'effective_at', 'known_at'])
        home = Path(self.temp.name)
        calls = []

        class Backend:

            def request(self, path, value):
                calls.append((path, value))
                return {'results': [], 'answerable': False}
        _transport = Backend()
        result = MemoryTools('synthetic-project', _transport).call('search_memory', {'query': 'Where was dataset as of 2026-09-10?', 'as_of': '2026-09-10', 'time_mode': 'known_at'})
        self.assertFalse(result['answerable'])
        self.assertEqual(calls[0][0], '/enterprise/v3/search')
        self.assertEqual(calls[0][1]['time_mode'], 'known_at')
        self.assertEqual(calls[0][1]['as_of'], '2026-09-10')

    def test_cloud_mcp_fetch_preserves_known_at_cutoff(self):
        from agenthub.mcp_tools import MemoryTools
        home = Path(self.temp.name)
        calls = []

        class Backend:

            def request(self, path, value):
                calls.append((path, value))
                return {'id': 'ta_old', 'revision': 'r1', 'claim': {'text': 'old location'}}
        _transport = Backend()
        result = MemoryTools('synthetic-project', _transport).call('fetch_memory', {'id': 'ta_old', 'revision': 'r1', 'as_of': '2026-09-10', 'time_mode': 'known_at'})
        self.assertEqual(result['claim']['text'], 'old location')
        self.assertEqual(calls, [('/enterprise/v3/temporal-detail', {'id': 'ta_old', 'as_of': '2026-09-10', 'time_mode': 'known_at'})])

    def test_cloud_timeline_uses_opaque_continuation(self):
        from agenthub.mcp_tools import MemoryTools
        home = Path(self.temp.name)
        calls = []

        class Backend:

            def request(self, path, value):
                calls.append((path, value))
                return {'id': value['id'], 'episodes': [], 'coverage_gaps': [], 'has_more': False, 'next_cursor': None}
        _transport = Backend()
        tools = MemoryTools('synthetic-project', _transport)
        tools.call('expand_memory_timeline', {'id': 'doc', 'limit': 2})
        tools.call('expand_memory_timeline', {'id': 'doc', 'limit': 2, 'cursor': 'tc1_' + '0' * 64})
        with self.assertRaisesRegex(ValueError, 'timeline_cursor_request'):
            tools.call('expand_memory_timeline', {'id': 'doc', 'offset': 0, 'cursor': 'tc1_' + '0' * 64})
        self.assertEqual(calls, [('/enterprise/v3/timeline', {'id': 'doc', 'limit': 2}), ('/enterprise/v3/timeline', {'id': 'doc', 'limit': 2, 'cursor': 'tc1_' + '0' * 64})])

    def test_cloud_project_history_and_detail_forward_scoped_request(self):
        from agenthub.mcp_tools import MemoryTools
        home = Path(self.temp.name)
        calls = []

        class Backend:

            def request(self, path, value):
                calls.append((path, value))
                return {'project': value['project'], 'episodes': [], 'coverage_gaps': [], 'phase_anchors': [], 'summary_revision': None, 'has_more': False, 'next_cursor': None} if path.endswith('project-history') else {'episode_id': value['episode_id'], 'assertions': [], 'coverage_gaps': []}
        _transport = Backend()
        tools = MemoryTools('synthetic-project', _transport)
        tools.call('project_memory_history', {'limit': 2})
        tools.call('fetch_memory_episode', {'episode_id': 'occ_1'})
        self.assertEqual(calls, [('/enterprise/v3/project-history', {'project': 'synthetic-project', 'limit': 2}), ('/enterprise/v3/project-episode', {'project': 'synthetic-project', 'episode_id': 'occ_1', 'offset': 0, 'limit': 8, 'text_offset': 0})])
if __name__ == '__main__':
    unittest.main()
