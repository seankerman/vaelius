"""Synthetic document-timeline paging and current-authorization regressions.

These exercise Hub serialization with a deterministic Client link helper. They
make no database, model, provider or private-fixture calls.
"""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agenthub.cloud_retrieval import HybridRetrievalMixin
from agenthub.enterprise import Denied


def entry(number, summary, *, gap=None):
    return {'episode_id':str(number),'session':'synthetic','source_turn':str(number),
            'supporting_source_ids':['source-'+str(number)],'summary':summary,
            'summary_revision':'summary-'+str(number),
            'source_range':{'captured_at_start':number,'captured_at_end':number},
            'coverage_gaps':gap or []}


class TimelineStore(HybridRetrievalMixin):
    def __init__(self, rows):
        self.rows=rows;self.allowed={row['episode_id'] for row in rows}
        self.revision='revision-one';self.generation='generation-one'
        self.document_allowed=True;self.revoke_on_recheck=None;self.checks={}

    @contextmanager
    def open(self):yield SimpleNamespace(db=object())

    def _need(self,ctx,action):
        if action!='read':raise AssertionError(action)

    def _document_allowed(self,db,ctx,document_id):
        if not self.document_allowed:return None
        return {'active_revision_id':self.revision,'internal_project':'synthetic-project'}

    def _timeline_episode_allowed(self,db,ctx,generation,row):
        ident=row['episode_id'];self.checks[ident]=self.checks.get(ident,0)+1
        if ident==self.revoke_on_recheck and self.checks[ident]>1:return False
        return ident in self.allowed

    def page(self,*,offset=0,limit=3,cursor=None,cursor_mode=False,actor='alice'):
        def links(db,generation,project,document_id,*,offset,limit):
            return {'episodes':self.rows[offset:offset+limit],
                    'has_more':offset+limit<len(self.rows),'coverage_gaps':[]}
        with patch('agenthub.processing.knowledge.active_generation_id',return_value=self.generation),\
             patch('agenthub.processing.episode_pipeline.episode_links_for_document',side_effect=links):
            return self.timeline({'tenant':'synthetic','actor':actor,'enrollment':'synthetic'},
                'document-one',offset=offset,limit=limit,cursor=cursor,cursor_mode=cursor_mode)


class TimelinePaginationTests(unittest.TestCase):
    def test_packing_two_large_authorized_episodes_keeps_continuation(self):
        store=TimelineStore([entry(1,'FIRST '+('a'*2200)),entry(2,'SECOND '+('b'*2200))])
        first=store.page(limit=2)
        self.assertLessEqual(len(json.dumps(first,ensure_ascii=True)),4000)
        self.assertEqual([item['summary_revision'] for item in first['episodes']],['summary-1'])
        self.assertTrue(first['has_more'])
        second=store.page(offset=len(first['episodes']),limit=2)
        self.assertEqual([item['summary_revision'] for item in second['episodes']],['summary-2'])
        self.assertFalse(second['has_more'])

    def test_denied_episode_has_no_content_gap_or_count_signal(self):
        store=TimelineStore([entry(1,'FIRST'),entry(2,'RESTRICTED SECRET',gap=['secret-gap']),entry(3,'THIRD')])
        store.allowed.remove('2')
        first=store.page(limit=1);second=store.page(offset=1,limit=1)
        self.assertEqual([item['summary'] for item in first['episodes']],['FIRST'])
        self.assertEqual([item['summary'] for item in second['episodes']],['THIRD'])
        self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
        self.assertNotIn('RESTRICTED',json.dumps([first,second]))
        self.assertNotIn('secret-gap',json.dumps([first,second]))
        self.assertEqual(first['coverage_gaps'],[]);self.assertEqual(second['coverage_gaps'],[])

    def test_stable_visible_offset_reaches_every_episode_once(self):
        store=TimelineStore([entry(i,'EPISODE '+str(i)) for i in range(1,5)])
        offset=0;seen=[]
        for _ in range(3):
            page=store.page(offset=offset,limit=2)
            seen.extend(item['summary_revision'] for item in page['episodes'])
            if not page['has_more']:break
            self.assertTrue(page['episodes']);offset+=len(page['episodes'])
        self.assertEqual(seen,['summary-1','summary-2','summary-3','summary-4'])

    def test_numeric_offset_response_keeps_the_v1_contract(self):
        from agentclient.enterprise_contract import validate_response
        store=TimelineStore([entry(1,'FIRST')]);page=store.page()
        self.assertNotIn('next_cursor',page)
        self.assertEqual(validate_response('/enterprise/v1/timeline/document-one',page),page)

    def test_revocation_before_page_or_during_delivery_denies_source(self):
        store=TimelineStore([entry(1,'FIRST'),entry(2,'SECOND'),entry(3,'THIRD')])
        self.assertEqual(store.page(limit=1)['episodes'][0]['summary'],'FIRST')
        store.allowed.remove('2')
        page=store.page(offset=1,limit=1)
        self.assertEqual([item['summary'] for item in page['episodes']],['THIRD'])
        self.assertNotIn('SECOND',json.dumps(page))
        store=TimelineStore([entry(1,'FIRST')]);store.revoke_on_recheck='1'
        with self.assertRaises(Denied):store.page()

    def test_oversized_single_episode_remains_reachable_with_gap(self):
        store=TimelineStore([entry(1,'FIRST '+('a'*9000)),entry(2,'SECOND')])
        first=store.page(limit=2)
        self.assertEqual(len(first['episodes']),1)
        self.assertLessEqual(len(json.dumps(first,ensure_ascii=True)),4000)
        self.assertIn('episode_summary_truncated',first['episodes'][0]['coverage_gaps'])
        self.assertTrue(first['has_more'])
        second=store.page(offset=1,limit=2)
        self.assertEqual([item['summary'] for item in second['episodes']],['SECOND'])

    def test_opaque_cursor_traverses_unchanged_authorized_history(self):
        store=TimelineStore([entry(i,'EPISODE '+str(i)) for i in range(1,5)])
        first=store.page(limit=2,cursor_mode=True)
        self.assertEqual([item['summary'] for item in first['episodes']],['EPISODE 1','EPISODE 2'])
        self.assertTrue(first['has_more']);self.assertRegex(first['next_cursor'],r'^tc1_[0-9a-f]{64}$')
        self.assertNotIn('source-',first['next_cursor'])
        second=store.page(limit=2,cursor=first['next_cursor'],cursor_mode=True)
        self.assertEqual([item['summary'] for item in second['episodes']],['EPISODE 3','EPISODE 4'])
        self.assertFalse(second['has_more']);self.assertIsNone(second['next_cursor'])

    def test_cursor_resumes_after_byte_packing_and_hidden_episode(self):
        store=TimelineStore([entry(1,'FIRST '+('a'*2200)),
                             entry(2,'RESTRICTED',gap=['restricted-gap']),
                             entry(3,'THIRD '+('b'*2200))])
        store.allowed.remove('2')
        first=store.page(limit=2,cursor_mode=True)
        self.assertLessEqual(len(json.dumps(first,ensure_ascii=True)),4000)
        self.assertEqual([item['summary_revision'] for item in first['episodes']],['summary-1'])
        self.assertTrue(first['has_more']);self.assertEqual(first['coverage_gaps'],[])
        second=store.page(limit=2,cursor=first['next_cursor'],cursor_mode=True)
        self.assertEqual([item['summary_revision'] for item in second['episodes']],['summary-3'])
        self.assertFalse(second['has_more']);self.assertNotIn('RESTRICTED',json.dumps([first,second]))

    def test_cursor_rejects_stale_authorization_and_generation(self):
        store=TimelineStore([entry(1,'FIRST'),entry(2,'SECOND'),entry(3,'THIRD')])
        first=store.page(limit=1,cursor_mode=True)
        store.allowed.remove('1')
        with self.assertRaisesRegex(ValueError,'timeline_cursor_stale'):
            store.page(limit=1,cursor=first['next_cursor'],cursor_mode=True)
        store=TimelineStore([entry(1,'FIRST'),entry(2,'SECOND')])
        first=store.page(limit=1,cursor_mode=True)
        store.generation='generation-two'
        with self.assertRaisesRegex(ValueError,'timeline_cursor_stale'):
            store.page(limit=1,cursor=first['next_cursor'],cursor_mode=True)
        store=TimelineStore([entry(1,'FIRST'),entry(2,'SECOND')])
        first=store.page(limit=1,cursor_mode=True)
        store.rows[1]['summary']='CORRECTED'
        with self.assertRaisesRegex(ValueError,'timeline_cursor_stale'):
            store.page(limit=1,cursor=first['next_cursor'],cursor_mode=True)

    def test_cursor_is_actor_bound_and_cannot_mix_with_numeric_offset(self):
        store=TimelineStore([entry(1,'FIRST'),entry(2,'SECOND')])
        first=store.page(limit=1,cursor_mode=True)
        with self.assertRaisesRegex(ValueError,'timeline_cursor_stale'):
            store.page(limit=1,cursor=first['next_cursor'],cursor_mode=True,actor='bob')
        with self.assertRaisesRegex(ValueError,'timeline_cursor_request'):
            store.page(offset=1,limit=1,cursor=first['next_cursor'],cursor_mode=True)


if __name__=='__main__':unittest.main()
