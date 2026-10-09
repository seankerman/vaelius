import unittest
from agenthub.processing.durable_memory import VERSION
from agenthub.cloud_retrieval import select_supported_cards

def row(ident,text,*,cosine=.8,rrf=.03,role='agent'):
 return {'document_id':ident,'revision_id':'revision-'+ident,'cosine':cosine,'rrf':rrf,'claim':{'title':'Harbor service','lesson':text,'evidence_status':'agent_reported' if role=='agent' else 'user_reported','_verified_source_owners':['alice'],'memory_context':{'policy':VERSION,'actors':[role],'attribution':'agent_reported' if role=='agent' else 'user_reported','state':'reported','facets':['fact']}}}
class FrozenFacetPairs(unittest.TestCase):
 def test_compound_assistant_preference_keeps_original_speaker(self):
  good=row('agent','The agent reported preferring Harbor for the shared subscription service, paired with a Postgres database.')
  user=row('user','The user prefers Haven for the shared subscription service, paired with an SQLite database.',role='user')
  cards,complete=select_supported_cards('What was the assistant’s stated preference for the shared subscription service, and what database did it pair with that preference?', [good,user], {'actor':'alice'},policy='facets_v2')
  self.assertTrue(complete);self.assertEqual([c['id']for c in cards],['agent'])
 def test_explicit_second_speaker_is_not_overwritten(self):
  good=row('agent','The agent reported preferring Harbor for the shared subscription service, paired with a Postgres database.')
  cards,complete=select_supported_cards('What was the assistant’s stated preference for the shared subscription service, and what database did Bob pair with his preference?', [good], {'actor':'alice'},policy='facets_v2')
  self.assertFalse(complete)
 def test_fused_relevance_is_not_discarded_for_cosine(self):
  good=row('target','The agent recommended Harbor Workers with a Postgres database for the shared subscription service.',cosine=.72,rrf=.032)
  other=row('related','The agent recommended Cove with a Postgres database for a document archive service.',cosine=.9,rrf=.016)
  cards,complete=select_supported_cards('What database was recommended for the shared subscription service?', [other,good], {'actor':'alice'},policy='facets_v2')
  self.assertTrue(complete);self.assertEqual([c['id']for c in cards],['target'])
if __name__=='__main__':unittest.main()
