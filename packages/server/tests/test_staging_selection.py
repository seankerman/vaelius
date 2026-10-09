"""Source-grounded authored selector regressions frozen before facets_v1 changes.

These are synthetic development examples, not held-out or receiving-agent proof.
The production caller supplies candidates only after canonical authorization.
"""
import json
from pathlib import Path
import unittest

from agenthub.cloud_retrieval import supported_answer

FIXTURE = Path(__file__).parents[1]/'tools/fixtures/local_staging_readiness_v1/retrieval_lane.json'


def claim(title, body, owner='alice', **extra):
    return dict(title=title, lesson=body, _verified_source_owners=[owner], **extra)


class GroundedSelectorTests(unittest.TestCase):
    def test_frozen_questions_use_exact_original_body_and_verified_requester(self):
        fixture=json.loads(FIXTURE.read_text())
        for case in fixture['selection_cases']:
            for question in case['questions']:
                with self.subTest(case=case['id'], question=question['query']):
                    item=claim(case['title'],case['body'],case['source_owner'])
                    answer=supported_answer(question['query'],item,
                        ctx={'actor':question['principal']},policy='facets_v1')
                    self.assertEqual(answer,question['answerable'])
                    for facet in question['facets']:
                        self.assertIn(facet['value'],case['body'])

    def test_source_owner_cannot_replace_described_actor(self):
        item=claim('Mixer repair','Bob repaired the mixer because its motor overheated.','alice')
        self.assertFalse(supported_answer('Why did I repair the mixer?',item,ctx={'actor':'alice'},policy='facets_v1'))
        self.assertTrue(supported_answer('Why did I repair the mixer?',item,ctx={'actor':'bob'},policy='facets_v1'))

    def test_opaque_identity_cannot_guess_named_person(self):
        item=claim('Mixer repair','Alice repaired the mixer because its motor overheated.','alice')
        self.assertFalse(supported_answer('Why did I repair the mixer?',item,ctx={'actor':'idp:unknown:123'},policy='facets_v1'))

    def test_prose_is_required_and_title_is_not_evidence(self):
        self.assertFalse(supported_answer('Where is the mixer repair ledger?',
            claim('Mixer repair ledger','# Mixer repair ledger\n\n'),ctx={'actor':'alice'},policy='facets_v1'))
        self.assertFalse(supported_answer('Where is the mixer repair ledger?',
            claim('Mixer repair ledger','A related review does not establish its recorded location.'),ctx={'actor':'alice'},policy='facets_v1'))

    def test_compound_needs_complementary_current_evidence(self):
        from agenthub.cloud_retrieval import select_supported_cards
        fixture=json.loads(FIXTURE.read_text())['compound'];candidates=[]
        for i,source in enumerate(fixture['sources']):
            candidates.append(dict(document_id=str(i),revision_id='1',claim=claim(source['title'],source['body']),rrf=1))
        cards,complete=select_supported_cards(fixture['query'],candidates,{'actor':'alice'})
        self.assertTrue(complete);self.assertEqual({c['id'] for c in cards},{'0','1'})
        cards,complete=select_supported_cards(fixture['query'],candidates[:1],{'actor':'alice'})
        self.assertFalse(complete);self.assertEqual(len(cards),1)

    def test_duplicate_evidence_does_not_fill_context(self):
        from agenthub.cloud_retrieval import select_supported_cards
        body='The mixer repair ledger is stored at /records/mixer/ledger.csv.'
        rows=[dict(document_id=str(i),revision_id='1',claim=claim('Mixer repair ledger',body),rrf=1) for i in range(5)]
        cards,complete=select_supported_cards('Where is the mixer repair ledger?',rows,{'actor':'alice'})
        self.assertTrue(complete);self.assertEqual(len(cards),1)

    def test_conflicting_current_value_needs_clarification(self):
        from agenthub.cloud_retrieval import select_supported_cards
        rows=[dict(document_id=str(i),revision_id='1',claim=claim('Mixer operating pressure',
            f'The approved mixer operating pressure is {value} kPa.'),rrf=1) for i,value in enumerate((35,42))]
        cards,complete=select_supported_cards('What is the approved mixer operating pressure?',rows,{'actor':'alice'})
        self.assertFalse(complete);self.assertEqual(cards,[])

    def test_exact_identifier_is_required_but_quantity_is_not(self):
        item=claim('Incident registry','Ticket MAINT-218 requires replacing the leaking rotor seal.')
        self.assertFalse(supported_answer('What is the remedy for MAINT-219?',item,ctx={'actor':'alice'},policy='facets_v1'))
        self.assertTrue(supported_answer('What is the remedy for MAINT-218?',item,ctx={'actor':'alice'},policy='facets_v1'))

    def test_reported_preference_does_not_become_operative_private_rule(self):
        item=claim('Checklist discussion','Alice heard that Bob prefers an unsigned checklist. Alice prefers verifying the access list.')
        self.assertFalse(supported_answer("What is Bob's private checklist preference?",item,ctx={'actor':'alice'},policy='facets_v1'))
        self.assertTrue(supported_answer('What did Alice hear about Bob\'s checklist preference?',item,ctx={'actor':'alice'},policy='facets_v1'))

    def test_missing_actor_requires_clarification(self):
        item=claim('Mixer reviews','Bob reviewed mixer pressure; Charlie reviewed its temperature.')
        self.assertFalse(supported_answer('What did my coworker review for the mixer?',item,ctx={'actor':'alice'},policy='facets_v1'))

    def test_unknown_policy_fails_closed(self):
        with self.assertRaises(ValueError):
            supported_answer('Where is the ledger?',claim('Ledger','The ledger is at /records/ledger.csv.'),ctx={'actor':'alice'},policy='invented')


if __name__=='__main__':unittest.main()
