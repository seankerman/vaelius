import unittest
from agenthub.processing.durable_memory import VERSION
from agenthub.cloud_retrieval import supported_answer

def claim(text):
 return {'title':'Boreal first trial','lesson':text,'memory_context':{'policy':VERSION,'actors':['agent'],'attribution':'agent_reported','state':'reported','facets':['activity','fact']}}
class NegativeTrialPairs(unittest.TestCase):
 def test_reported_inability_is_a_trial_result(self):
  for phrase in ['could not install the build','couldn’t install the build','could not connect to the device']:
   with self.subTest(phrase=phrase):
    self.assertTrue(supported_answer('What was the result of the first Boreal trial?',claim('The agent reported that the first Boreal trial '+phrase+' because the device was unavailable.'),ctx={'actor':'alice'},policy='facets_v2'))
 def test_proposed_trial_is_not_a_completed_trial(self):
  self.assertFalse(supported_answer('What was the result of the first Boreal trial?',claim('The agent proposed a first Boreal trial that could install the build on a future connected device.'),ctx={'actor':'alice'},policy='facets_v2'))
if __name__=='__main__':unittest.main()
