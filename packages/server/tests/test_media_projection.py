"""Synthetic media cases frozen before changing the curator's text projection.

Encoded bytes are original evidence objects, not readable text. Text on either
side must remain citable at its original offsets, including after continuation.
"""
import copy
import json
import unittest

from agenthub.processing.continuous_observer import _whole_turn, durable_continuation
from agenthub.processing.durable_memory import packets_for, validate_records
from agenthub.processing.episode_curator import prepare_episode
from agenthub.processing.evidence_references import EvidenceReferences
import test_episode_pipeline as fixtures


def rows(body):
    common = dict(project='enterprise:media-fixture', session='chat', turn='one',
                  source_role='episode_evidence', created=1)
    return [dict(common, id='goal', source_order=1, kind='UserPromptSubmit', body='Where did Mira save Cobalt?'),
            dict(common, id='tool', source_order=2, kind='PostToolUse', tool_name='exec_command', exit_code=0,
                 body=body),
            dict(common, id='answer', source_order=3, kind='Stop', body='The dataset was saved.')]


class MediaProjectionTests(unittest.TestCase):
    def test_large_data_uri_is_metadata_and_original_offsets_survive(self):
        encoded='data:image/png;base64,'+'ABCD'*100_000
        body='Screenshot follows: '+encoded+'\nMira saved Cobalt at /data/cobalt.csv.'
        sources=rows(body); before=copy.deepcopy(sources)
        packet=prepare_episode(sources, media_policy='references-v1')
        event=packet['episode']['events'][1]
        self.assertEqual(sources,before)
        self.assertLess(len(json.dumps(packet)),10_000)
        self.assertEqual(len(event['media_references']),1)
        ref=event['media_references'][0]
        self.assertEqual(ref['mime_type'],'image/png')
        self.assertFalse(ref['content_is_evidence'])
        for span in event['spans']:
            self.assertEqual(span['text'],body[span['start']:span['end']])
            self.assertNotIn(encoded,span['text'])
        self.assertIn('Mira saved Cobalt',event['spans'][-1]['text'])
        self.assertEqual(event['spans'][-1]['start'],body.index('\nMira'))

    def test_nested_typed_media_and_audio_video_uris_never_become_text(self):
        encoded='ABCD'*20_000
        body=json.dumps({'content':[{'type':'image','data':encoded,'mimeType':'image/jpeg'},
            {'type':'text','text':'Mira chose Cobalt.'},
            {'type':'input_audio','input_audio':{'data':encoded,'format':'wav'}}]})
        packet=prepare_episode(rows(body),media_policy='references-v1')
        event=packet['episode']['events'][1]
        self.assertEqual(len(event['media_references']),2)
        self.assertNotIn(encoded,json.dumps(packet))
        for mime in ('audio/wav','video/mp4','application/pdf'):
            part=prepare_episode(rows('data:'+mime+';base64,'+encoded),media_policy='references-v1')
            self.assertEqual(part['episode']['events'][1]['media_references'][0]['mime_type'],mime)

    def test_normal_base64_dataset_text_and_old_generation_remain_unchanged(self):
        body=json.dumps({'data':'ABCD'*2000,'description':'Encoded dataset column'})
        packet=prepare_episode(rows(body),media_policy='references-v1')
        self.assertEqual(''.join(s['text'] for s in packet['episode']['events'][1]['spans']),body)
        media='data:image/png;base64,'+'ABCD'*2000
        legacy=prepare_episode(rows(media))
        self.assertNotIn('media_references',legacy['episode']['events'][1])
        self.assertEqual(''.join(s['text'] for s in legacy['episode']['events'][1]['spans']),media)
        with self.assertRaisesRegex(ValueError,'invalid_media_policy'):
            prepare_episode(rows(body),media_policy='silent-truncation')

    def test_staging_continuation_and_citation_validate_against_original_text(self):
        encoded='data:image/png;base64,'+'ABCD'*40_000
        body='Mira verified Cobalt.\n'+encoded+'\nMira saved Cobalt at /data/cobalt.csv.\n'+('More checks.\n'*150)
        packets,_=packets_for(rows(body),max_chars=1400,max_stages=16,
                             split_oversized_events=True,media_policy='references-v1')
        whole=_whole_turn(packets); refs=EvidenceReferences(whole)
        sent=durable_continuation(packets[-1],whole,refs)
        self.assertNotIn(encoded,json.dumps(sent))
        event=whole['episode']['events'][1]
        span=next(s for s in event['spans'] if '/data/cobalt.csv' in s['text'])
        short=refs.packet(whole)['episode']['events'][1]
        short_span=next(s for s in short['spans'] if '/data/cobalt.csv' in s['text'])
        raw=dict(title='Cobalt location',text='Mira saved Cobalt at /data/cobalt.csv.',
            subject='Cobalt',facets=['activity'],actors=['Mira'],
            artifact={'name':'Cobalt','location':'/data/cobalt.csv'},rationale=None,
            state='observed',occurred_date='',event_id=short['event_id'],
            evidence_span_ids=[short_span['span_id']])
        result=validate_records({'records':[raw]},whole,{'tool'},references=refs)
        self.assertEqual(len(result['records']),1)
        self.assertEqual(span['text'],body[span['start']:span['end']])
        raw['evidence_span_ids']=['media-reference']
        self.assertFalse(validate_records({'records':[raw]},whole,{'tool'},references=refs)['records'])

    def test_media_only_does_not_support_an_invented_visual_fact(self):
        packet=prepare_episode(rows('data:image/png;base64,'+'ABCD'*1000),media_policy='references-v1')
        self.assertEqual(packet['episode']['events'][1]['spans'],[])
        refs=EvidenceReferences(packet)
        self.assertFalse(any(entry['kind']=='s' and entry['original_id'].startswith('media')
                             for entry in refs.manifest['entries'].values()))


class MediaEpisodePipelineTests(unittest.TestCase):
    setUp=fixtures.EpisodePipelineTests.setUp
    tearDown=fixtures.EpisodePipelineTests.tearDown
    source=fixtures.EpisodePipelineTests.source
    episode=fixtures.EpisodePipelineTests.episode

    def test_stateless_episode_mode_uses_the_same_original_media_projection(self):
        from agenthub.processing.episode_pipeline import run_once,generation_status
        encoded='data:image/png;base64,'+'ABCD'*20000
        self.cfg['episode_curation']['media_policy']='references-v1'
        self.episode('media','Cobalt retry test passed.\n'+encoded+'\nCobalt retry verification passed.')
        sent=[]
        def runner(home,config,instruction,payload,schema):
            sent.append(payload)
            self.assertNotIn(encoded,json.dumps(payload))
            return self.runner(home,config,instruction,payload,schema)
        run_once(self.state,self.cfg,runner)
        self.assertEqual(generation_status(self.state.db,'fixture-generation')['documents'],1)
        self.assertTrue(sent)
        self.assertIn(encoded,self.state.db.execute('SELECT body FROM memories WHERE id=?',('media-t',)).fetchone()[0])


if __name__=='__main__':unittest.main()
