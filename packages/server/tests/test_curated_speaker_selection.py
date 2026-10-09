"""Original speaker roles survive curation without becoming account names."""
import unittest

from agenthub.processing.durable_memory import VERSION
from agenthub.cloud_retrieval import supported_answer, select_supported_cards


def memory(text, *, attribution='user_reported', owner='account-17', state='reported'):
    return {'title': 'Billing preference', 'lesson': text,
            '_verified_source_owners': [owner],
            'memory_context': {'policy': VERSION, 'actors': ['user'],
                'attribution': attribution, 'state': state, 'facets': ['fact'],
                'reason_actor': '', 'reason_quote': ''}}


class CuratedSpeakerSelection(unittest.TestCase):
    def test_substantive_canonical_reason_and_speaker_pairs(self):
        import copy
        claim = memory('The user chose CSV because its header remains stable.')
        claim['title'] = 'CSV format decision'
        claim['memory_context'].update(subject='CSV format', facets=['decision'],
            reason_actor='user', reason_quote='it preserves a stable header.')
        for query in ['Why did I choose CSV?', 'Why did the user choose CSV?']:
            with self.subTest(query=query):
                self.assertTrue(supported_answer(query, claim,
                    ctx={'actor':'account-17'}, policy='facets_v2'))
        for query in ['Why did I choose CSV?', 'Why did the user choose CSV?']:
            self.assertFalse(supported_answer(query, claim,
                ctx={'actor':'account-18'}, policy='facets_v2'))
        for change in [dict(reason_quote='', reason_actor=''),
                       dict(reason_quote='The reason is unknown.'),
                       dict(reason_actor='agent'), dict(policy='stale')]:
            bad=copy.deepcopy(claim);bad['memory_context'].update(change)
            with self.subTest(change=change):
                self.assertFalse(supported_answer('Why did I choose CSV?', bad,
                    ctx={'actor':'account-17'}, policy='facets_v2'))
        # A native document with no compiled reason relation cannot borrow it.
        self.assertFalse(supported_answer('Why CSV?',
            {'title':'CSV format', 'lesson':'CSV preserves a stable header.'},
            policy='facets_v2'))

    def test_original_user_role_resolves_only_through_verified_owner(self):
        claim = memory('User prefers central billing for family apps when feasible.')
        query = 'Which billing arrangement would I prefer for family apps?'
        self.assertTrue(supported_answer(query, claim, ctx={'actor': 'account-17'}, policy='facets_v2'))
        self.assertFalse(supported_answer(query, claim, ctx={'actor': 'account-18'}, policy='facets_v2'))

    def test_agent_report_cannot_supply_the_users_preference(self):
        claim = memory('Agent recommends central billing for family apps.', attribution='agent_reported')
        self.assertFalse(supported_answer('Which billing arrangement would I prefer?',
                         claim, ctx={'actor': 'account-17'}, policy='facets_v2'))

    def test_reporting_someone_elses_preference_does_not_make_it_mine(self):
        claim = memory('User reported that Blair prefers central billing for family apps.')
        self.assertFalse(supported_answer('Which billing arrangement would I prefer for family apps?',
                         claim, ctx={'actor': 'account-17'}, policy='facets_v2'))

    def test_requested_work_is_not_completed_work(self):
        claim = memory('User asked to implement the copper workflow.', state='attempted')
        self.assertFalse(supported_answer('What did I implement for the copper workflow?',
                         claim, ctx={'actor': 'account-17'}, policy='facets_v2'))

    def test_first_trial_report_wins_over_a_later_proposal(self):
        report = memory('User reported that the first trial replaced damage combat with morale checks.')
        proposal = memory('Agent proposed replacing damage combat with a hazard track for the next trial.',
                          attribution='agent_reported')
        rows = [{'document_id': ident, 'revision_id': ident + '-r', 'claim': claim,
                 'cosine': score, 'rrf': 1} for ident, claim, score in
                [('report', report, .65), ('proposal', proposal, .9)]]
        cards, answerable = select_supported_cards('What replaced damage combat in the first trial?',
            rows, {'actor': 'account-17'}, policy='facets_v2')
        self.assertTrue(answerable)
        self.assertEqual([card['id'] for card in cards], ['report'])


if __name__ == '__main__':
    unittest.main()
