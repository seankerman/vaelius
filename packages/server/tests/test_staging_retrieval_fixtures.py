"""Public frozen fixtures only; never inspect the private confirmation labels."""
import copy,hashlib,importlib.util,json,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
SPEC=importlib.util.spec_from_file_location('staging_fixture_validator',ROOT/'tools/validate_staging_retrieval_fixtures.py')
validator=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(validator)

class StagingFixtures(unittest.TestCase):
    def test_public_source_grounded_coverage_and_new_partition(self):
        report=validator.validate_public()
        self.assertTrue(report['pass']);self.assertFalse(report['sealed_questions_read'])
        self.assertEqual(report['confirmation_sources'],120)
        old=json.loads((ROOT/'tools/fixtures/retrieval_experiments_v1/corpus.json').read_text())
        new=json.loads((validator.FIXTURES/'corpus-v2.json').read_text())
        old_ids={s['id'] for s in old['sources']}
        confirmation=[s for s in new['sources'] if s['split']=='confirmation']
        self.assertFalse(old_ids & {s['id'] for s in confirmation})
        self.assertFalse({s['scenario'] for s in old['sources']} & {s['scenario'] for s in confirmation})
        self.assertFalse({s['original_sha256'] for s in old['sources']} & {s['original_sha256'] for s in confirmation})

    def test_regressions_ground_original_spans_and_private_neighbor_generation(self):
        value=json.loads((validator.FIXTURES/'regressions.json').read_text())
        cases={c['id']:c for c in value['cases']}
        self.assertEqual(len(cases['multiple_facets']['sources']),2)
        self.assertEqual(cases['forbidden_neighbors']['forbidden_neighbor_generator']['count'],1000)
        self.assertEqual(cases['forbidden_neighbors']['forbidden_neighbor_generator']['reader_ids'],['bob'])
        self.assertEqual(set(cases['return_revocation']['boundary_paths']),{'search','detail','timeline','history','original'})
        self.assertEqual(len(cases['native_history']['sources']),2)

    def test_tampering_is_rejected_before_any_sealed_read(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)
            for name in ('fixture_manifest.json','fixture_manifest-v2.json','regressions.json','corpus.json','corpus-v2.json'):
                (directory/name).write_bytes((validator.FIXTURES/name).read_bytes())
            (directory/'regressions.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'fixture_changed'):
                validator.validate_public(directory)

    def test_every_authored_curation_validates_through_canonical_packet_schema(self):
        from agenthub.processing.durable_memory import packet_for,validate_records
        corpus=json.loads((validator.FIXTURES/'corpus-v2.json').read_text())
        count=0
        for source in corpus['sources']:
            if source['kind']!='curated_memory':continue
            with self.subTest(source=source['id']):
                rows=[{'id':source['id']+':'+kind,'project':'synthetic:staging','session':source['scenario'],
                       'turn':'1','kind':kind,'body':source['text'] if kind=='UserPromptSubmit' else 'Recorded the synthetic statement.',
                       'created':1790438400.,'occurred_at':source['metadata']['occurred_at']}
                      for kind in ['UserPromptSubmit','Stop']]
                packet=packet_for(rows);event=next(e for e in packet['episode']['events'] if e['kind']=='UserPromptSubmit')
                record=copy.deepcopy(source['curation']['record'])
                record.update(event_id=event['event_id'],evidence_span_ids=[a['span_id'] for a in event['spans']])
                checked=validate_records({'records':[record]},packet,{e['event_id'] for e in packet['episode']['events']})
                self.assertFalse(checked['rejections']);self.assertEqual(len(checked['records']),1);count+=1
        self.assertEqual(count,20)
