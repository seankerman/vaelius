"""Frozen adversarial same-entity shortlist regression before rank ordering."""
import unittest

import test_history_consolidation as fixtures
from agenthub.processing.episode_pipeline import related_artifacts


class SameEntityRivalTests(unittest.TestCase):
    setUp=fixtures.HistoryConsolidationTests.setUp
    tearDown=fixtures.HistoryConsolidationTests.tearDown
    add=fixtures.HistoryConsolidationTests.add

    def test_old_specific_match_survives_ninety_newer_same_entity_rivals(self):
        old,_=self.add('old','cedar','Set bounded queue width',
            'Keep at most seven queued sends','router-k',1)
        for i in range(90):
            self.add('rival-'+str(i),'cedar','Routine router checkpoint '+str(i),
                'Record synthetic router checkpoint '+str(i),'router-k',i+2)
        candidate={'title':'Limit pending sends','claim':'Cap pending sends at seven',
            'problem':'','action':'','outcome':'','subjects':['router-k'],
            'aliases':[]}
        found=related_artifacts(self.state.db,'history-fixture','cedar',candidate,
                                limit=8)
        self.assertIn(old['document_id'],[item['artifact_id'] for item in found])
        self.assertLessEqual(len(found),8)


if __name__=='__main__':
    unittest.main()
