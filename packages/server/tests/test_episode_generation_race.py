"""A competing worker can create the same generation after our first read."""
import unittest
from unittest.mock import patch

from agenthub.processing.episode_pipeline import ensure_generation, curator_version, SOURCE_POLICY, _hash


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _CompetingInsert:
    def __init__(self, row):
        self.row = row
        self.reads = 0

    def execute(self, sql, params=()):
        if sql.startswith('SELECT * FROM knowledge_generations'):
            self.reads += 1
            return _Result(None if self.reads == 1 else self.row)
        if sql.startswith('INSERT INTO knowledge_generations'):
            if 'ON CONFLICT(generation_id) DO NOTHING' not in sql:
                raise RuntimeError('duplicate generation from another worker')
            return _Result()
        raise AssertionError(sql)


class GenerationRaceTests(unittest.TestCase):
    def setUp(self):
        self.config = {'episode_curation': {'generation_id': 'shared-generation',
                                          'policy': 'durable_memory'}}
        bounded = {key: self.config['episode_curation'].get(key) for key in
                   ('max_events_per_stage', 'max_chars_per_stage', 'max_stages',
                    'max_reducer_chars', 'settle_seconds')}
        self.row = {'curator_version': curator_version(self.config),
                    'source_policy': SOURCE_POLICY, 'config_hash': _hash(bounded)}

    def test_other_worker_creates_identical_generation(self):
        db = _CompetingInsert(self.row)
        with patch('agenthub.processing.episode_pipeline.initialize'):
            self.assertEqual(ensure_generation(db, self.config), 'shared-generation')
        self.assertEqual(db.reads, 2)

    def test_other_worker_creates_different_generation_definition(self):
        db = _CompetingInsert({**self.row, 'config_hash': 'different'})
        with patch('agenthub.processing.episode_pipeline.initialize'):
            with self.assertRaisesRegex(ValueError, 'episode_generation_definition_changed'):
                ensure_generation(db, self.config)


if __name__ == '__main__':
    unittest.main()
