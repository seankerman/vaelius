"""Invented cited detail, permission and pagination controls; no model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from agenthub.processing.episode_curator import _spans
from vaelius_test_support.fixtures.enterprise import EnterpriseStore
from agenthub.enterprise import Denied


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=EnterpriseStore(Path(self.tmp.name)/'hub')
        self.store.create_organization('orchard')
        self.store.create_project('orchard','maple')
        for name in ('alice','bob'):
            self.store.create_principal('orchard',name)
            self.store.set_membership('orchard','maple',name,True)
        self.alice=self.store.authenticate(self.store.enroll('orchard','alice','owner',
            ['ingest','read','source_read','correct','policy','withdraw']))
        self.bob=self.store.authenticate(self.store.enroll('orchard','bob','reader',['read','source_read']))
        self.readonly=self.store.authenticate(self.store.enroll('orchard','alice','read-only',['read']))
        self.body=('Unrelated historical setup. '*40)+'\nAlice saved Copper in /fixtures/copper.csv because the importer requires CSV.\n'
        self.source=self.store.ingest(self.alice,{'version':'enterprise-local-1','external_id':'first','session':'chat',
            'turn':'first','project':'maple','kind':'Stop','body':self.body,'visibility':'private',
            'occurred_at':'2026-09-25T10:00:00Z'})['source_id']
        self.doc=self.store.accept_reviewed_note(self.alice,self.source,'Copper export','Alice saved the Copper dataset.')
        self.spans=_spans(self.source,self.body)
        with self.store.open() as st,st.db:
            st.db.execute('DELETE FROM knowledge_support WHERE revision_id=?',(self.doc['revision_id'],))
            for span in self.spans:
                st.db.execute("INSERT INTO knowledge_support VALUES(?,?,?,'supports','unknown',0)",
                    (self.doc['revision_id'],self.source,span['span_id']))

    def tearDown(self):self.tmp.cleanup()

    def fetch(self,ctx=None,**kw):
        return self.store.document_evidence(ctx or self.alice,self.doc['document_id'],
            revision=kw.pop('revision',self.doc['revision_id']),**kw)

    def test_expands_cited_originals_and_preserves_speaker_time_and_offsets(self):
        pages=[];offset=0
        while True:
            result=self.fetch(offset=offset);pages.extend(result['sources'])
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=True)),4000)
            if result['next_offset'] is None:break
            self.assertGreater(result['next_offset'],offset);offset=result['next_offset']
        self.assertEqual(len(pages),len(self.spans))
        for row in pages:
            self.assertEqual(row['text'],self.body[row['start']:row['end']])
            self.assertEqual(row['kind'],'Stop');self.assertEqual(row['owner'],'alice')
            self.assertEqual(row['occurred_at'],'2026-09-25T10:00:00Z')
        self.assertTrue(any('/fixtures/copper.csv' in row['text'] for row in pages))

    def test_requires_both_read_and_raw_source_permission(self):
        with self.assertRaises(Denied):self.fetch(self.readonly)
        with self.assertRaises(Denied):self.fetch(self.bob)

    def test_withdrawal_stale_revision_and_invalid_offset_fail_closed(self):
        self.assertEqual(self.fetch(revision='old')['status'],'invalidated')
        for offset in [-1,True,1.5,10001]:
            with self.assertRaises(ValueError):self.fetch(offset=offset)
        self.store.lifecycle(self.alice,{'version':'enterprise-local-1','target_id':self.source,
            'operation':'withdraw','expected_revision':'1','idempotency_key':'withdraw','reason':'fixture'})
        with self.assertRaises(Denied):self.fetch()

    def test_unknown_citation_is_a_gap_not_arbitrary_raw_text(self):
        with self.store.open() as st,st.db:
            st.db.execute("DELETE FROM knowledge_support WHERE revision_id=?", (self.doc['revision_id'],))
            st.db.execute("INSERT INTO knowledge_support VALUES(?,?,'missing','supports','unknown',0)",
                (self.doc['revision_id'],self.source))
        result=self.fetch();self.assertEqual(result['sources'],[])
        self.assertIn('source_span_unavailable',result['coverage_gaps'])

    def test_escaped_unicode_excerpt_is_bounded_and_explicitly_partial(self):
        body='Unicode fixture:'+'🙂'*464
        source=self.store.ingest(self.alice,{'version':'enterprise-local-1','external_id':'unicode',
            'session':'chat','turn':'unicode','project':'maple','kind':'Stop','body':body,
            'visibility':'private','occurred_at':'2026-09-25T10:00:00Z'})['source_id']
        doc=self.store.accept_reviewed_note(self.alice,source,'Unicode fixture','A source with escaped Unicode.')
        span=_spans(source,body)[0]
        with self.store.open() as st,st.db:
            st.db.execute('DELETE FROM knowledge_support WHERE revision_id=?',(doc['revision_id'],))
            st.db.execute("INSERT INTO knowledge_support VALUES(?,?,?,'supports','unknown',0)",
                (doc['revision_id'],source,span['span_id']))
        value=self.store.document_evidence(self.alice,doc['document_id'],revision=doc['revision_id'])
        self.assertLessEqual(len(json.dumps(value,ensure_ascii=True)),4000)
        self.assertIn('source_excerpt_truncated',value['coverage_gaps'])
        card=value['sources'][0]
        self.assertEqual(card['text'],body[card['start']:card['end']])
        self.assertEqual(card['span_end'],480)

    def test_shared_derived_release_does_not_grant_source_access(self):
        release=self.store.reviewed_release(self.alice,self.doc['document_id'],self.doc['revision_id'],
            'maple',['bob'],'release')
        with self.assertRaises(Denied):
            self.store.document_evidence(self.bob,release['document_id'],revision=release['revision'])

class CitationProjectionTests(unittest.TestCase):
    def test_citation_resolution_excludes_encoded_media_and_unknown_spans(self):
        from agenthub.source_evidence import cited_span
        from agenthub.processing.media_projection import project_media
        body='A useful sentence. data:image/png;base64,'+'A'*2048+' Final useful sentence.'
        source={'id':'synthetic','body':body};ranges,_=project_media(body)
        self.assertTrue(ranges)
        for span in _spans('synthetic',body):
            found=cited_span(source,span['span_id'])
            if found:self.assertNotIn('A'*100,found[2])
        self.assertIsNone(cited_span(source,'unknown'))
