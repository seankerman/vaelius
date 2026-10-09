from vaelius_test_support.fixtures.memory import seed_validated_records
"""Current curated rationale evidence through the canonical compiler/installation."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from agenthub.processing.durable_memory import packet_for,validate_records,observation_for
from agenthub.processing.episode_pipeline import sources_for_job
from agentclient.enterprise_capture import normalize_capture
from agentclient.enterprise_contract import VERSION
from agenthub.cloud_retrieval import supported_answer
from agenthub.cloud_runtime import CloudStore
from test_cloud_postgres import PostgresFixture,SERVICES

SHA='03b3f118eb29432e1ef8f794e541626d13429f52602f0169ae363349c6f996fa'
def cases():
    raw=(Path(__file__).resolve().parent.parent/'tests/fixtures/service/cloud_canonical_rationale_v1.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SHA:raise ValueError('frozen_curated_rationale_changed')
    return json.loads(raw)['cases']

def compile_record(case,sources):
    packet=packet_for(sources);event=next(e for e in packet['episode']['events'] if e['kind']==case['source_kind'])
    record=copy.deepcopy(case['record']);record.update(event_id=event['event_id'],evidence_span_ids=[s['span_id'] for s in event['spans']])
    checked=validate_records({'records':[record]},packet,{e['event_id'] for e in packet['episode']['events']})
    return packet,checked

class CanonicalRationalePairs(unittest.TestCase):
    def test_frozen_records_compile_and_select_from_validated_rationale(self):
        for case in cases():
            with self.subTest(case=case['id']):
                sources=[{'id':'fixture-'+case['id']+'-'+kind,'project':'enterprise:synthetic-rationale','session':'rationale','turn':'1','kind':kind,
                    'body':case['source_text'] if kind==case['source_kind'] else 'Remember the Maple task.' if kind=='UserPromptSubmit' else 'Acknowledged.',
                    'created':1790416800.0,'occurred_at':'2026-09-26T10:00:00Z'} for kind in ('UserPromptSubmit','Stop')]
                packet,checked=compile_record(case,sources)
                self.assertEqual(checked['rejections'],case['validation_rejections'])
                compiled=observation_for(checked['records'][0],packet) if checked['records'] else None
                if case['override_policy'] and compiled:compiled['memory_context']['policy']=case['override_policy']
                self.assertEqual(compiled,case['compiled_observation'])
                self.assertEqual(bool(compiled and supported_answer(case['query'],compiled)),case['expected'])

@unittest.skipUnless(SERVICES,'explicit real PostgreSQL required')
class CanonicalRationalePostgres(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.store=CloudStore(self.store.home,self.dsn,'acme')
    def tearDown(self):self.postgres_teardown()
    def test_validated_records_install_with_original_provenance_and_retrieve(self):
        for case in cases():
            with self.subTest(case=case['id']):
                project='rationale-'+case['id'];self.store.create_project('acme',project);self.store.set_membership('acme',project,'alice',True)
                ctx=self.store.authenticate(self.tokens['alice']);self.store.enroll_connection(ctx,project,'fixture',project,['agent'])
                ids=[]
                for kind in ('UserPromptSubmit','Stop'):
                    body=case['source_text'] if kind==case['source_kind'] else 'Remember the Maple task.' if kind=='UserPromptSubmit' else 'Acknowledged.'
                    value=normalize_capture({'hook_event_name':kind,'event_id':project+kind,'session_id':project,'turn_id':'1',
                        'prompt':body,'last_assistant_message':body,'timestamp':'2026-09-26T10:00:00Z'},project,project)
                    ids.append(self.store.ingest_general(ctx,value)['source_id'])
                with self.store.open() as state:
                    row=dict(state.db.execute('SELECT project,session,turn FROM memories WHERE id=?',(ids[0],)).fetchone());row['source_ids']=json.dumps(ids)
                    sources=sources_for_job(state.db,row);packet,checked=compile_record(case,sources)
                    self.assertEqual(len(checked['rejections']),len(case['validation_rejections']))
                    docs=[r['document_id'] for r in seed_validated_records(state,checked,packet)]
                    if case['override_policy']:
                        # Explicit stale-policy fault after real canonical installation;
                        # no invented claim or evidence is inserted directly.
                        for doc in docs:
                            revision=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(doc,)).fetchone()[0]
                            claim=json.loads(state.db.execute('SELECT claim_json FROM knowledge_revisions WHERE revision_id=?',(revision,)).fetchone()[0])
                            claim['memory_context']['policy']=case['override_policy']
                            with state.db:state.db.execute('UPDATE knowledge_revisions SET claim_json=? WHERE revision_id=?',(json.dumps(claim),revision))
                self.store.refresh_documents()
                with self.store.open() as state:
                    for doc in docs:
                        dependencies={r[0] for r in state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(doc,))}
                        self.assertTrue(dependencies<=set(ids));self.assertTrue(dependencies)
                result=self.store.search(ctx,{'version':VERSION,'query':case['query'],'project':project,'mode':'explicit'})
                self.assertEqual(result['answerable'],case['expected'])
                if case['expected']:self.assertEqual({c['id'] for c in result['results']},set(docs))
                else:self.assertFalse(result['results'])
