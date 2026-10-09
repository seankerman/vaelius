"""Synthetic contracts frozen before context retrieval implementation."""
import json
import unittest


def sources():
    return {
        'a':dict(id='a',body='Proposal: use the red route. '*60,version=1,
                 project='orchard',session='chat',turn='1',speaker='user',occurred_at=10),
        'b':dict(id='b',body='Implemented the red route.',version=1,
                 project='orchard',session='chat',turn='1',speaker='assistant',occurred_at=11),
        'c':dict(id='c',body='Review: red route reverted; blue route remains experimental.',version=1,
                 project='orchard',session='chat',turn='2',speaker='assistant',occurred_at=20),
        'd':dict(id='d',body='Red route for an unrelated project.',version=1,
                 project='other',session='chat',turn='2',speaker='user',occurred_at=21),
        'e':dict(id='e',body='Future red route result.',version=1,
                 project='orchard',session='chat',turn='3',speaker='assistant',occurred_at=40),
    }


class ConversationContextTests(unittest.TestCase):
    def test_original_turn_and_later_related_update_without_supersession_inference(self):
        from agenthub.conversation_context import select_context
        rows=select_context(['a'],list(sources().values()),query='red route',cutoff=30)
        self.assertEqual([r['id'] for r in rows],['a','b','c'])
        self.assertEqual(rows[-1]['context_relation'],'related_later_message')
        self.assertFalse(any('supersedes' in r for r in rows))

    def test_no_unknown_session_expansion_or_same_word_cross_project(self):
        from agenthub.conversation_context import select_context
        data=sources();data['a']['session']=None
        self.assertEqual([r['id'] for r in select_context(['a'],list(data.values()),query='red route',cutoff=30)],['a'])

    def test_full_source_pages_are_exact_nonoverlapping_and_bounded(self):
        from agenthub.conversation_context import context_page
        row=sources()['a'];row['body']='🙂'*700+' user says do not infer why.'*80
        offset=0;text=''
        while True:
            result=context_page('doc','rev',[row],offset=offset,max_chars=4000)
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=True)),4000)
            for p in result['sources']:
                self.assertEqual(p['text'],row['body'][p['start']:p['end']]);text+=p['text']
                self.assertEqual(p['speaker'],'user')
            if result['next_offset'] is None:break
            self.assertGreater(result['next_offset'],offset);offset=result['next_offset']
        self.assertEqual(text,row['body'])

    def test_search_groups_same_parent_but_keeps_other_turns(self):
        from agenthub.conversation_context import discovery_cards
        rows=[dict(id=str(n),revision='v1',title='route',text='red route '*150,
                   parent_ids=[parent],event_time=n) for n,parent in enumerate(['a','a','b','c'])]
        out=discovery_cards(rows,limit=3,max_chars=4000)
        self.assertEqual([r['id'] for r in out['records']],['0','2','3'])
        self.assertFalse(out['answerable'])
        self.assertLessEqual(len(json.dumps(out)),4000)

    def test_future_anchor_is_not_delivered(self):
        from agenthub.conversation_context import select_context
        self.assertEqual(select_context(['e'],list(sources().values()),query='route',cutoff=30),[])

    def test_unknown_actor_stays_unknown(self):
        from agenthub.conversation_context import context_page
        row=sources()['a'];row.pop('speaker');row['owner']='alice'
        self.assertEqual(context_page('d','r',[row])['sources'][0]['speaker'],'unknown')

    def test_invalid_page_offset_rejected(self):
        from agenthub.conversation_context import context_page
        for bad in [-1,True,1.5,10001]:
            with self.assertRaises(ValueError):context_page('d','r',[],offset=bad)
