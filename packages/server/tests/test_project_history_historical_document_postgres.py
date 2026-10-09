"""Historical document events retain current-head and historical-version policy."""
import io
import json
from contextlib import nullcontext
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agenthub.processing.episode_pipeline import _record_episode_revision
from agenthub.cloud_runtime import CloudStore
from agenthub.document_ingest import DocumentStore
from agenthub.project_history import project_overview
from agenthub.source_objects import FileSourceObjects
from test_cloud_postgres import PostgresFixture, SERVICES


@unittest.skipUnless(SERVICES, 'explicit synthetic PostgreSQL services required')
class HistoricalDocumentHistoryTests(PostgresFixture, unittest.TestCase):
    def setUp(self):
        def pin_context():
            return (nullcontext() if os.environ.get('AGENTNETWORK_INSTALLED_TEST')=='1'
                    else patch('agenthub.pipeline_pin.verify',return_value={}))
        with pin_context():
            self.postgres_setup()
        with pin_context():
            self.store=CloudStore(self.store.home,self.dsn,'acme')
        self.store.enroll_connection(self.ctx,'history-originals','synthetic','maple',
            ['document'],visibility='team',reader_ids=['alice','bob'])
        self.documents=DocumentStore(self.store,FileSourceObjects(Path(self.temp.name)/'objects'))
        self.bob=self.store.authenticate(self.tokens['bob'])

    def tearDown(self):self.postgres_teardown()

    def test_retired_version_remains_in_authorized_history_then_policy_narrows(self):
        sources=[]
        for version,text in [(1,'Original synthetic Maple design.'),
                             (2,'Revised synthetic Maple design.')]:
            receipt=self.documents.ingest(self.ctx,'history-originals','design',str(version),
                'design.md',io.BytesIO(text.encode()),title='Maple Design')
            sources.append((version,text,receipt['source_id']))
        with self.store.open() as state,state.db:
            db=state.db
            scope=db.execute('SELECT internal_project FROM enterprise_sources WHERE id=?',
                (sources[-1][2],)).fetchone()[0]
            db.execute('''INSERT INTO knowledge_generations
                (generation_id,curator_version,status,source_policy,config_hash,created)
                VALUES('history-document-fixture','synthetic','active','synthetic','fixed',1)''')
            for version,text,source in sources:
                ident=f'history-original-v{version}'
                source_session=db.execute('SELECT session FROM memories WHERE id=?',
                    (source,)).fetchone()[0]
                db.execute('''INSERT INTO curation_episode_jobs
                    (id,generation_id,episode_id,project,session,turn,source_ids,source_hash,
                     created,updated,version,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'done')''',
                    (ident,'history-document-fixture',ident,scope,
                     source_session,ident,json.dumps([source]),ident,version,version,'fixture'))
                row={'id':ident,'generation_id':'history-document-fixture',
                     'project':scope,'session':source_session,'turn':ident,
                     'source_hash':ident,'source_ids':json.dumps([source])}
                record={'title':f'Maple design v{version}','text':text,'subject':'maple',
                        'facets':['activity'],'actors':['alice'],'state':'document_version',
                        'attribution':'source','occurred_date':''}
                ref={'source_id':source,'version':str(version),'start':0,'end':len(text),
                     'quote':text,'segment_id':ident}
                _record_episode_revision(db,row,{'episode_disposition':'no_learning'},
                    [{'atom_key':ident,'record':record,'evidence':[ref],
                      'operation':'AUTHORED_FIXED'}],[],{},version)
        listed=self.documents.list(self.bob,title='Maple Design')
        self.assertEqual({item['version'] for item in listed},{'1','2'})
        with self.store.open() as state:
            from agenthub.processing.episode_pipeline import get_episode_view
            self.assertEqual(state.db.execute('SELECT count(*) FROM episode_occurrences').fetchone()[0],2)
            sample=get_episode_view(state.db,'history-document-fixture',scope,source_session,
                'history-original-v2',limit=20,mode='history',source_authorizer=lambda _:True)
            self.assertTrue(sample and sample.get('assertions'),sample)
        page=project_overview(self.store,self.bob,'maple',page_limit=1)
        self.assertEqual(len(page['episodes']),1)
        self.assertTrue(page['has_more'])
        next_page=project_overview(self.store,self.bob,'maple',cursor=page['next_cursor'],page_limit=1)
        self.assertEqual(len(next_page['episodes']),1)
        self.assertNotEqual(page['episodes'][0]['episode_id'],next_page['episodes'][0]['episode_id'])
        self.store.connection_policy(self.ctx,'history-originals',reader_ids=['alice'])
        self.assertEqual(self.documents.list(self.bob,title='Maple Design'),[])
        with self.assertRaises((ValueError,PermissionError)):
            project_overview(self.store,self.bob,'maple',cursor=page['next_cursor'],page_limit=1)


if __name__=='__main__':unittest.main()
