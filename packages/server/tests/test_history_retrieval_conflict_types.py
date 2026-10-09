"""Development regressions for typed current-answer conflicts; no model or DB."""
import unittest

from agenthub.cloud_retrieval import select_supported_cards


def candidate(ident, lesson, *, title='Jasper record', owner='alice'):
    return {'document_id':ident,'revision_id':'revision-'+ident,
            'claim':{'title':title,'lesson':lesson,'_verified_source_owners':[owner]},
            'cosine':0.75,'rrf':0.03}


class TypedCurrentConflictTests(unittest.TestCase):
    def select(self, query, *rows):
        return select_supported_cards(query,rows,{'actor':'alice'},policy='facets_v2')

    def test_same_amount_spelling_is_not_a_conflict(self):
        cards,complete=self.select('What is the approved Jasper budget?',
            candidate('one','The approved Jasper budget is 25,000 EUR.'),
            candidate('two','The approved Jasper budget is 25000 euros.'))
        self.assertTrue(complete);self.assertEqual(len(cards),1)

    def test_different_currency_is_not_silently_resolved(self):
        cards,complete=self.select('What is the approved Jasper budget?',
            candidate('one','The approved Jasper budget is 25000 EUR.'),
            candidate('two','The approved Jasper budget is 25000 USD.'))
        self.assertFalse(complete);self.assertEqual(cards,[])

    def test_same_date_in_iso_and_month_spelling_is_not_a_conflict(self):
        cards,complete=self.select('What is the Jasper launch date?',
            candidate('one','The Jasper launch date is 2026-10-02.'),
            candidate('two','The Jasper launch date is October 2, 2026.'))
        self.assertTrue(complete);self.assertEqual(len(cards),1)

    def test_unrelated_quantity_in_same_card_is_not_a_budget_conflict(self):
        cards,complete=self.select('What is the approved Jasper budget?',
            candidate('one','The approved Jasper budget is 25000 EUR. The request quota is 100 requests.'),
            candidate('two','Jasper has an approved budget of 25,000 EUR.'))
        self.assertTrue(complete);self.assertEqual(len(cards),1)

    def test_another_measure_in_the_same_card_is_not_the_requested_pressure(self):
        cards,complete=self.select('What is the approved Jasper operating pressure?',
            candidate('one','The approved Jasper operating pressure is 42 kPa. The Jasper alarm threshold is 57 kPa.'),
            candidate('two','The approved Jasper operating pressure is 42 kPa.'))
        self.assertTrue(complete);self.assertEqual(len(cards),1)

    def test_named_budget_scope_selects_its_value(self):
        cards,complete=self.select('What is the approved Jasper finance budget?',
            candidate('finance','The approved Jasper finance budget is 25000 EUR.'),
            candidate('operations','The approved Jasper operations budget is 35000 EUR.'))
        self.assertTrue(complete);self.assertEqual([card['id'] for card in cards],['finance'])

    def test_named_actor_does_not_borrow_another_actors_budget(self):
        cards,complete=self.select('What budget did Alice approve for Jasper?',
            candidate('alice','Alice approved the Jasper budget of 25000 EUR.'),
            candidate('bob','Bob approved the Jasper budget of 35000 EUR.'))
        self.assertTrue(complete);self.assertEqual([card['id'] for card in cards],['alice'])

    def test_current_paths_dates_owners_and_versions_conflict(self):
        cases=(
            ('Where is the Jasper report path?',
             'The Jasper report path is /reports/jasper/v1.csv.',
             'The Jasper report path is /reports/jasper/v2.csv.'),
            ('What is the Jasper launch date?',
             'The Jasper launch date is 2026-10-02.',
             'The Jasper launch date is 2026-10-09.'),
            ('Who owns the Jasper register?',
             'Alice owns the Jasper register.',
             'Bob owns the Jasper register.'),
            ('What is the Jasper version?',
             'The Jasper version is v2.1.',
             'The Jasper version is v2.2.'),
            ('Where is the Jasper budget stored?',
             'The Jasper budget is stored at /reports/jasper/budget-one.csv.',
             'The Jasper budget is stored at /reports/jasper/budget-two.csv.'),
            ('Who owns the Jasper budget?',
             'The Jasper budget owner is Alice Smith.',
             'The Jasper budget owner is Alice Jones.'),
        )
        for query,first,second in cases:
            with self.subTest(query=query):
                cards,complete=self.select(query,candidate('one',first),candidate('two',second))
                self.assertFalse(complete);self.assertEqual(cards,[])

    def test_previous_path_does_not_displace_current_path(self):
        cards,complete=self.select('Where is the current Jasper report path?',
            candidate('old','The previous Jasper report path was /reports/jasper/v1.csv.'),
            candidate('current','The current Jasper report path is /reports/jasper/v2.csv.'))
        self.assertTrue(complete);self.assertEqual([card['id'] for card in cards],['current'])

    def test_path_component_named_old_is_not_a_historical_qualifier(self):
        cards,complete=self.select('Where is the current Jasper report path?',
            candidate('current','The current Jasper report path is /archives/old/jasper.csv.'))
        self.assertTrue(complete);self.assertEqual([card['id'] for card in cards],['current'])


if __name__=='__main__':unittest.main()
