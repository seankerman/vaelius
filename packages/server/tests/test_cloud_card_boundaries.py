"""Frozen regressions: full evidence beats display-title length; roles stay distinct."""
import json
import unittest
from agenthub.processing.durable_memory import VERSION
from agenthub.cloud_retrieval import select_supported_cards
CTX={'actor':'alice'}
def candidate(ident,lesson,*,title='Maple settings',attribution='agent_reported',actors=None):
    claim={'title':title,'lesson':lesson,'evidence_status':attribution,
        'memory_context':{'policy':VERSION,'actors':actors or ['agent'],
            'attribution':attribution,'state':'reported','reason_actor':'','reason_quote':'',
            'facets':['fact'],'subject':'Maple settings'},'_verified_source_owners':['alice']}
    return {'document_id':ident,'revision_id':'rev_'+('b'*48),'claim':claim,'cosine':.8,'rrf':.02}
class CardBoundaryPairs(unittest.TestCase):
    def test_long_display_title_does_not_discard_complete_readable_evidence(self):
        lesson='Maple training control is the separate “Include projects” setting. '+('This report describes settings and does not verify the installed account. '*12)
        row=candidate('doc_'+('a'*48),lesson,title='Maple '+('descriptive display metadata '*6))
        original={'id':row['document_id'],'revision':row['revision_id'],'title':row['claim']['title'],'lesson':lesson,'evidence_status':'agent_reported'}
        self.assertGreater(len(json.dumps(original,ensure_ascii=True)),1300)
        self.assertLess(len(json.dumps(dict(original,title='Maple'),ensure_ascii=True)),1300)
        cards,complete=select_supported_cards('Which separate Maple training setting was reported?', [row], CTX,policy='facets_v2')
        self.assertTrue(complete);self.assertEqual(len(cards),1);self.assertEqual(cards[0]['lesson'],lesson)
        self.assertLessEqual(len(json.dumps(cards[0],ensure_ascii=True)),1300)
    def test_explicit_assistant_preference_is_not_the_callers_private_preference(self):
        row=candidate('agent','The agent reported preferring Harbor for the shared subscription service, paired with a Postgres database.')
        cards,complete=select_supported_cards('What was the assistant’s stated preference for the shared subscription service?', [row], CTX,policy='facets_v2')
        self.assertTrue(complete);self.assertEqual([c['id']for c in cards],['agent'])
        cards,complete=select_supported_cards('What is my preference for the shared subscription service?', [row], CTX,policy='facets_v2')
        self.assertFalse(complete);self.assertFalse(cards)
    def test_user_preference_does_not_supply_an_assistant_preference(self):
        row=candidate('user','The user prefers Harbor for the shared subscription service.',attribution='user_reported',actors=['user'])
        cards,complete=select_supported_cards('What was the assistant’s stated preference for the shared subscription service?', [row], CTX,policy='facets_v2')
        self.assertFalse(complete);self.assertFalse(cards)
        cards,complete=select_supported_cards('What is my preference for the shared subscription service?', [row], CTX,policy='facets_v2')
        self.assertTrue(complete);self.assertEqual([c['id']for c in cards],['user'])
