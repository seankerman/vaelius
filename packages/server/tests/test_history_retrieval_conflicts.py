"""Synthetic current-answer conflict regressions frozen before H behavior edits.

The first case reproduces the documented S4 EUR-value failure. Other cases test
the general value rule without relying on any personal source material.
"""
import unittest

from agenthub.cloud_retrieval import select_supported_cards


def candidate(ident, lesson, *, cosine=0.75):
    return {
        'document_id': ident,
        'revision_id': 'revision-' + ident,
        'claim': {'title': 'Jasper budget decision', 'lesson': lesson,
                  '_verified_source_owners': ['alice']},
        'cosine': cosine,
        'rrf': 0.03,
    }


class CurrentValueConflictTests(unittest.TestCase):
    def select(self, query, *rows):
        return select_supported_cards(query, rows, {'actor': 'alice'}, policy='facets_v2')

    def test_same_scope_approved_eur_values_do_not_produce_a_settled_answer(self):
        query = 'What is the approved Jasper budget?'
        cards, answerable = self.select(query,
            candidate('one', 'The approved Jasper budget is 25000 EUR.'),
            candidate('two', 'The approved Jasper budget is 35000 EUR.', cosine=0.82))
        self.assertFalse(answerable)
        self.assertEqual(cards, [])

    def test_conflict_detection_is_currency_independent(self):
        for unit in ('GBP', 'USD'):
            with self.subTest(unit=unit):
                cards, answerable = self.select('What is the approved Jasper budget?',
                    candidate('one', f'The approved Jasper budget is 25000 {unit}.'),
                    candidate('two', f'The approved Jasper budget is 35000 {unit}.'))
                self.assertFalse(answerable)
                self.assertEqual(cards, [])

    def test_explicitly_previous_value_does_not_block_current_value(self):
        cards, answerable = self.select('What is the approved Jasper budget now?',
            candidate('old', 'The previous approved Jasper budget was 25000 EUR.'),
            candidate('current', 'The current approved Jasper budget is 35000 EUR.'))
        self.assertTrue(answerable)
        self.assertEqual([item['id'] for item in cards], ['current'])


if __name__ == '__main__':
    unittest.main()
