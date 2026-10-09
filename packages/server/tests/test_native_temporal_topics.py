"""Native document metadata must not hide a supported historical topic."""
import unittest
from agenthub.processing.temporal_retrieval import _relevant


class NativeTemporalTopics(unittest.TestCase):
    def group(self, title, value):
        return {'subject': title, 'predicate': 'fact',
                'qualifiers': {'origin': 'native_document'},
                'assertions': [{'value': value}], 'unknown_time': []}

    def test_descriptive_title_can_match_source_topic_and_named_subject(self):
        group = self.group('Cedar deployment safety threshold supporting review',
                          'Cedar allows rollout below 4 percent error rate.')
        self.assertTrue(_relevant(group,
            'What error threshold did Cedar use on 2026-09-10?', 'fact'))

    def test_another_named_subject_does_not_match_shared_topics(self):
        group = self.group('Cedar deployment safety threshold supporting review',
                          'Cedar allows rollout below 4 percent error rate.')
        self.assertFalse(_relevant(group,
            'What error threshold did Birch use on 2026-09-10?', 'fact'))

    def test_matching_entity_without_requested_topic_is_insufficient(self):
        group = self.group('Cedar deployment safety threshold supporting review',
                          'Cedar allows rollout below 4 percent error rate.')
        self.assertFalse(_relevant(group,
            'What customer email did Cedar use on 2026-09-10?', 'fact'))

    def test_date_and_generic_words_do_not_establish_a_subject(self):
        group = self.group('Deployment supporting review',
                          'The deployment review on 2026-09-10 used 4 percent.')
        self.assertFalse(_relevant(group,
            'What customer email was used on 2026-09-10?', 'fact'))
