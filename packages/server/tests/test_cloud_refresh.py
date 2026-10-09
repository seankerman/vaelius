"""The batch registration path must equal the previous source-set algorithm."""
import hashlib,json
from pathlib import Path
import unittest
from agentclient.enterprise_contract import VERSION
from test_cloud_postgres import PostgresFixture,SERVICES


class RefreshFrozenFixtureTests(unittest.TestCase):
    def test_frozen_source_rules(self):
        root=Path(__file__).resolve().parents[1]/'tests/fixtures/service';path=root/'cloud_refresh_v1.json'
        manifest=json.loads(path.with_name('cloud_refresh_v1_manifest.json').read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),manifest['sha256'])


@unittest.skipUnless(SERVICES,'explicit PostgreSQL fixture service required')
class BatchedRefreshPostgresTests(PostgresFixture,unittest.TestCase):
    def setUp(self):
        self.postgres_setup();self.sources=[];self.documents=[]
        for index in range(4):
            source=self.store.ingest(self.ctx,{'version':VERSION,'external_id':'refresh-'+str(index),
                'session':'refresh','turn':str(index),'project':'maple','kind':'Stop','visibility':'private',
                'occurred_at':100,
                'body':'Synthetic registration source '+str(index)+' retains its distinct dataset location.'})['source_id']
            document=self.store.accept_reviewed_note(self.ctx,source,'Distinct refresh '+str(index),
                'Synthetic distinct dataset '+str(index)+' is saved at /synthetic/refresh/'+str(index)+'.csv.')['document_id']
            self.sources.append(source);self.documents.append(document)
    def tearDown(self):self.postgres_teardown()

    def oracle(self,db):
        result={}
        for doc in db.execute("""SELECT document_id,project,active_revision_id FROM knowledge_documents d
            WHERE lifecycle='active' AND active_revision_id IS NOT NULL AND NOT EXISTS(
              SELECT 1 FROM knowledge_generation_documents gd JOIN knowledge_generations g USING(generation_id)
              WHERE gd.document_id=d.document_id AND g.status!='active')"""):
            members=[r[0] for r in db.execute('SELECT memory_id FROM knowledge_document_members WHERE document_id=?',(doc['document_id'],))]
            if not members:continue
            sources=set()
            for member in members:
                known=db.execute('SELECT id FROM enterprise_sources WHERE id=?',(member,)).fetchone()
                if known:sources.add(known[0])
                sources.update(r[0] for r in db.execute('SELECT source_id FROM observation_sources WHERE memory_id=?',(member,)))
            sources.update(r[0] for r in db.execute('SELECT source_memory_id FROM knowledge_support WHERE revision_id=?',(doc['active_revision_id'],)))
            for job in db.execute('SELECT job_id FROM episode_candidates WHERE document_id=?',(doc['document_id'],)):
                record=db.execute('SELECT source_ids FROM curation_episode_jobs WHERE id=?',(job[0],)).fetchone()
                if record:sources.update(json.loads(record[0]))
                sources.update(r[0] for r in db.execute('SELECT source_id FROM backend_processing_dependencies WHERE episode_job=?',(job[0],)))
                for related in db.execute('SELECT related_document_id FROM enterprise_model_inputs WHERE job_id=?',(job[0],)):
                    sources.update(r[0] for r in db.execute('SELECT source_memory_id FROM knowledge_support s JOIN knowledge_documents d ON d.active_revision_id=s.revision_id WHERE document_id=?',(related[0],)))
                    sources.update(r[0] for r in db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(related[0],)))
            if not sources:continue
            rows=db.execute('SELECT * FROM enterprise_sources WHERE id=ANY(?::text[])',(list(sources),)).fetchall()
            if len(rows)!=len(sources) or any(row['internal_project']!=doc['project'] or not row['active'] for row in rows):continue
            if len({row['tenant'] for row in rows})!=1:continue
            result[doc['document_id']]=sources
        return result

    def compare(self):
        with self.store.open() as state,state.db:
            expected=self.oracle(state.db)
            state.db.execute('DELETE FROM enterprise_dependencies');state.db.execute('DELETE FROM enterprise_documents')
        self.assertEqual(self.store.refresh_documents(),len(expected))
        with self.store.open() as state:
            actual={row[0]:set() for row in state.db.execute('SELECT id FROM enterprise_documents')}
            for row in state.db.execute('SELECT document_id,source_id FROM enterprise_dependencies'):actual[row[0]].add(row[1])
        self.assertEqual(actual,expected)
        self.assertEqual(self.store.refresh_documents(),len(expected))

    def test_direct_observation_and_support_provenance(self):self.compare()

    def test_missing_inactive_scope_and_tenant_sources_fail_closed(self):
        with self.store.open() as state,state.db:
            # Corrupted mixed-tenant relational input is injected below the
            # correctly tenant-bound API to verify batch registration denial.
            state.db.execute("INSERT INTO enterprise_organizations VALUES('other')")
            state.db.execute("INSERT INTO enterprise_principals(tenant,id,active) VALUES('other','alice',1)")
            state.db.execute('UPDATE enterprise_sources SET active=0 WHERE id=?',(self.sources[0],))
            state.db.execute("UPDATE enterprise_sources SET internal_project='other' WHERE id=?",(self.sources[1],))
            state.db.execute("UPDATE enterprise_sources SET tenant='other' WHERE id=?",(self.sources[2],))
            mixed=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(self.documents[2],)).fetchone()[0]
            state.db.execute("INSERT INTO knowledge_support VALUES(?,?,NULL,'supports','unknown',0)",(mixed,self.sources[3]))
            revision=state.db.execute('SELECT active_revision_id FROM knowledge_documents WHERE document_id=?',(self.documents[3],)).fetchone()[0]
            state.db.execute("INSERT INTO memories(id,session,project,body,kind,created) SELECT 'unknown-source',session,project,'Synthetic missing enterprise provenance','Stop',0 FROM memories WHERE id=?",(self.sources[3],))
            state.db.execute("INSERT INTO knowledge_support VALUES(?,'unknown-source',NULL,'supports','unknown',0)",(revision,))
        self.compare()

    def test_no_members_or_building_generation_do_not_register(self):
        with self.store.open() as state,state.db:
            state.db.execute('DELETE FROM knowledge_document_members WHERE document_id=?',(self.documents[0],))
            state.db.execute("INSERT INTO knowledge_generations VALUES('fixture-building','v1','building','private','fixture',0,NULL,NULL)")
            state.db.execute("INSERT INTO knowledge_generation_documents VALUES('fixture-building',?,0)",(self.documents[1],))
        self.compare()

    def test_episode_related_and_backend_dependencies_are_all_retained(self):
        with self.store.open() as state,state.db:
            scope=state.db.execute('SELECT internal_project FROM enterprise_sources WHERE id=?',(self.sources[0],)).fetchone()[0]
            state.db.execute("INSERT INTO curation_episode_jobs(id,generation_id,episode_id,project,session,turn,source_ids,source_hash,created,updated,version) VALUES('fixture-job','fixture','episode',?,'session','1',?,'sha',0,0,'v1')",(scope,json.dumps([self.sources[1]])))
            state.db.execute("INSERT INTO episode_candidates VALUES('candidate','fixture-job','fixture','key','{}','{}','accepted',?,0)",(self.documents[0],))
            state.db.execute("INSERT INTO backend_processing_dependencies VALUES('fixture-job',?,1)",(self.sources[2],))
            state.db.execute("INSERT INTO enterprise_model_inputs VALUES('fixture-job',?)",(self.documents[1],))
            state.db.execute('INSERT INTO enterprise_dependencies VALUES(?,?) ON CONFLICT DO NOTHING',(self.documents[1],self.sources[3]))
        # Preserve prior related dependencies as input: compare() clears output
        # registrations, so capture oracle then check actual without clearing.
        with self.store.open() as state:expected=self.oracle(state.db)
        self.assertEqual(expected[self.documents[0]],set(self.sources))
        self.store.refresh_documents()
        with self.store.open() as state:
            actual={r[0] for r in state.db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(self.documents[0],))}
        self.assertEqual(actual,set(self.sources))

    def test_only_building_block_reactivates_after_valid_registration(self):
        with self.store.open() as state,state.db:
            state.db.execute("UPDATE enterprise_documents SET active=0,blocked_reason='withdrawn' WHERE id=?",(self.documents[0],))
            state.db.execute("UPDATE enterprise_documents SET active=0,blocked_reason='building' WHERE id=?",(self.documents[1],))
        self.store.refresh_documents()
        with self.store.open() as state:
            old=state.db.execute('SELECT active,blocked_reason FROM enterprise_documents WHERE id=?',(self.documents[0],)).fetchone()
            building=state.db.execute('SELECT active,blocked_reason FROM enterprise_documents WHERE id=?',(self.documents[1],)).fetchone()
        self.assertEqual((old['active'],old['blocked_reason']),(0,'withdrawn'))
        self.assertEqual((building['active'],building['blocked_reason']),(1,''))
