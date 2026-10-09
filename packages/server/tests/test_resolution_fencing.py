"""Provider-free resolver checkpoint and concurrent-writer regression."""
import json
import unittest
from unittest.mock import patch

import test_episode_pipeline as fixtures
from agenthub.processing.episode_pipeline import run_once
from agenthub.processing.knowledge import apply_resolved_observation, backfill_candidate_keys


class ResolutionFencingTests(unittest.TestCase):
    tearDown=fixtures.EpisodePipelineTests.tearDown
    source=fixtures.EpisodePipelineTests.source
    episode=fixtures.EpisodePipelineTests.episode

    def setUp(self):
        fixtures.EpisodePipelineTests.setUp(self)

    def test_stale_resolver_checkpoint_re_resolves_without_recurating(self):
        self.episode('first',created=1)
        self.assertTrue(run_once(self.state,self.cfg,self.runner))
        document=self.state.db.execute('SELECT document_id,active_revision_id FROM knowledge_documents').fetchone()
        old_revision=document['active_revision_id']
        self.episode('second','Corrected Cobalt retry test passed',created=10)
        import agenthub.processing.episode_pipeline as episode_pipeline
        original_install=episode_pipeline._install
        external_revision=[]

        def concurrent_writer(*args,**kwargs):
            # A separate accepted writer changes the target between model
            # resolution and the pending episode's installation transaction.
            source=self.state.db.execute('''SELECT m.body FROM memories m
                JOIN knowledge_document_members mm ON mm.memory_id=m.id
                WHERE mm.document_id=? AND m.kind='KnowledgeCandidate' LIMIT 1''',
                (document['document_id'],)).fetchone()
            observation=json.loads(source['body'])
            with self.state.db:
                self.state.db.execute('''INSERT INTO memories
                    (id,session,project,body,kind,created,active)
                    VALUES('external-writer','session','fixture',?,'KnowledgeCandidate',20,1)''',
                    (json.dumps(observation),))
                result=apply_resolved_observation(self.state.db,'external-writer',
                    'fixture','session',observation,'SUPERSEDE',document['document_id'],
                    expected_revision_id=old_revision)
            external_revision.append(result['revision_id'])
            return original_install(*args,**kwargs)

        with patch('agenthub.processing.episode_pipeline._install',side_effect=concurrent_writer):
            self.assertTrue(run_once(self.state,self.cfg,self.runner))
        job=self.state.db.execute('''SELECT status,error,progress FROM curation_episode_jobs
            WHERE turn='second' ''').fetchone()
        self.assertEqual((job['status'],job['error']),('pending','stale_resolution_revision'))
        progress=json.loads(job['progress'])
        self.assertEqual(len(progress['stage_outputs']),1)
        self.assertNotIn('resolutions',progress)
        current=self.state.db.execute('''SELECT active_revision_id FROM knowledge_documents
            WHERE document_id=?''',(document['document_id'],)).fetchone()[0]
        self.assertEqual(current,external_revision[0])
        self.assertEqual(self.state.db.execute('''SELECT count(*) FROM episode_candidates
            WHERE job_id=(SELECT id FROM curation_episode_jobs WHERE turn='second')''').fetchone()[0],0)
        with self.state.db:
            self.state.db.execute("UPDATE curation_episode_jobs SET next_attempt=0 WHERE turn='second'")
        self.assertTrue(run_once(self.state,self.cfg,self.runner))
        self.assertEqual(self.runner.calls.count('durable_memory_curate'),2)
        self.assertEqual(self.runner.calls.count('episode_resolve'),3)
        self.assertEqual(self.state.db.execute('''SELECT status FROM curation_episode_jobs
            WHERE turn='second' ''').fetchone()[0],'done')

    def test_old_document_keys_backfill_in_bounded_pages(self):
        self.episode('first',created=1)
        run_once(self.state,self.cfg,self.runner)
        with self.state.db:
            self.state.db.execute('DELETE FROM knowledge_candidate_keys')
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM knowledge_candidate_keys').fetchone()[0],0)
        page=backfill_candidate_keys(self.state.db,limit=1)
        self.assertEqual((page['scanned'],page['model_calls']),(1,0))
        keys=self.state.db.execute('''SELECT key_kind,key_value FROM knowledge_candidate_keys''').fetchall()
        self.assertIn(('subject','cobalt retry'),[tuple(row.values()) for row in keys])
        again=backfill_candidate_keys(self.state.db,after_document_id=page['next_after_document_id'],limit=1)
        self.assertEqual((again['scanned'],again['has_more']),(0,False))


if __name__=='__main__':
    unittest.main()
