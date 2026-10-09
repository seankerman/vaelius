"""H4 synthetic regressions frozen before candidate/fencing behavior changes.

The H1 development story bank is SHA-256
6679c5f321517c31b4352d9ddedf9830bb1c8c32e39b2918c8da82fac42e4d2d.
No provider calls or personal source contents are used here.
"""
import json
import tempfile
import unittest
from pathlib import Path

from agenthub.processing.episode_pipeline import related_artifacts
from agenthub.processing.knowledge import apply_resolved_observation, ingest_observation
from vaelius_test_support.fixtures.state import State


class HistoryConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)
        (self.home/'config.json').write_text(json.dumps({'observer':{'enabled':False}}))
        self.state=State(self.home)
        with self.state.db:
            self.state.db.execute('''INSERT INTO knowledge_generations
                (generation_id,curator_version,status,source_policy,config_hash,created)
                VALUES('history-fixture','synthetic','active','synthetic','synthetic',1)''')

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def add(self, ident, project, title, lesson, subject, at):
        observation={'title':title,'lesson':lesson,'problem':'','action':'',
            'outcome_text':'','knowledge_type':'procedure','domain':'synthetic',
            'subjects':[subject],'aliases':[],'tags':[],
            'applicability_constraints':{},'evidence_status':'directly_observed',
            'evidence':[{'source_id':'source-'+ident,'segment_id':'span-'+ident}]}
        with self.state.db:
            self.state.db.execute('''INSERT INTO memories
                (id,session,project,body,kind,created,active)
                VALUES(?,?,?,?,?,?,1)''',
                ('source-'+ident,'session',project,'synthetic verified output',
                 'PostToolUse',at))
            self.state.db.execute('''INSERT INTO memories
                (id,session,project,body,kind,created,active)
                VALUES(?,?,?,?,?,?,1)''',
                (ident,'session',project,json.dumps(observation),'Observation',at))
            self.state.db.execute('INSERT INTO observation_sources VALUES(?,?)',
                                  (ident,'source-'+ident))
            result=ingest_observation(self.state.db,ident,project,'session',
                                      observation,now=at,force_new=True)
            self.state.db.execute('''INSERT INTO knowledge_generation_documents
                VALUES(?,?,?)''',('history-fixture',result['document_id'],at))
        return result,observation

    def test_older_paraphrased_same_entity_survives_200_newer_documents(self):
        old,_=self.add('old','cedar','Set bounded queue width',
            'Keep at most seven queued sends','router-k',1)
        for i in range(205):
            self.add('filler-'+str(i),'cedar','Unrelated checkpoint '+str(i),
                'Record an unrelated synthetic detail '+str(i),
                'other-'+str(i),i+2)
        cross,_=self.add('cross','another-project','Limit pending sends',
            'Cap pending sends at seven','router-k',500)
        candidate={'title':'Limit pending sends','claim':'Cap pending sends at seven',
            'problem':'','action':'','outcome':'','subjects':['router-k'],
            'aliases':[]}
        found=related_artifacts(self.state.db,'history-fixture','cedar',candidate,
                                limit=8)
        ids=[item['artifact_id'] for item in found]
        self.assertIn(old['document_id'],ids)
        self.assertNotIn(cross['document_id'],ids)
        self.assertLessEqual(len(ids),8)

    def test_stale_writer_cannot_replace_newly_accepted_revision(self):
        first,_=self.add('first','cedar','Retry decision A',
            'Use retry decision A','router-k',1)
        _,new_a=self.add('new-a','cedar','Retry decision B',
            'Use retry decision B','router-k',2)
        _,new_b=self.add('new-b','cedar','Retry decision C',
            'Use retry decision C','router-k',3)
        expected=first['revision_id']
        with self.state.db:
            accepted=apply_resolved_observation(self.state.db,'new-a','cedar',
                'session',new_a,'SUPERSEDE',first['document_id'],
                expected_revision_id=expected)
        with self.assertRaisesRegex(ValueError,'stale_resolution_revision'):
            with self.state.db:
                apply_resolved_observation(self.state.db,'new-b','cedar','session',
                    new_b,'SUPERSEDE',first['document_id'],
                    expected_revision_id=expected)
        document=self.state.db.execute('''SELECT active_revision_id FROM knowledge_documents
            WHERE document_id=?''',(first['document_id'],)).fetchone()
        self.assertEqual(document['active_revision_id'],accepted['revision_id'])
        self.assertEqual(self.state.db.execute('''SELECT count(*) FROM knowledge_revisions
            WHERE document_id=?''',(first['document_id'],)).fetchone()[0],2)
        self.assertEqual(self.state.db.execute('''SELECT count(*) FROM memories
            WHERE id IN ('source-new-a','source-new-b') AND active=1''').fetchone()[0],2)


if __name__=='__main__':
    unittest.main()
