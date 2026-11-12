"""Explicitly reviewed derived releases."""

import hashlib
import json
import time


def initialize(db):
    db.require_schema()


class RetrievalMixin:
    def _release_allowed(self,db,ctx,document_id,sources):
        release=db.execute('SELECT * FROM backend_releases WHERE document_id=?',(document_id,)).fetchone()
        if not release or release['tenant']!=ctx['tenant']:return False
        if ctx['actor']!=release['actor'] and ctx['actor'] not in json.loads(release['readers']):return False
        frozen=json.loads(release['dependencies'])
        if {s['id'] for s in sources}!=set(frozen):return False
        for source in sources:
            if (not source['active'] or source['tenant']!=ctx['tenant']
                    or frozen[source['id']]!=[source['source_version'],source['policy_version']]):return False
            if source.get('principal_quarantine') and source['owner']!=ctx['actor']:return False
            if not self._general_source_allowed(db,dict(ctx,actor=release['actor']),source):return False
        return True

    def reviewed_release(self,ctx,document_id,expected_revision,project,readers,key):
        from agenthub.enterprise import Denied, Conflict, _scope
        from agenthub.processing.knowledge import accept_local_candidate
        self._need(ctx,'policy');self._need(ctx,'read')
        if not readers or len(readers)>100 or not key or len(key)>128:raise ValueError('invalid_release')
        readers=sorted(set(readers))
        ident=hashlib.sha256(json.dumps([ctx['tenant'],ctx['actor'],project,key]).encode()).hexdigest()
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db
            prior=db.execute('SELECT document_id,original_document,original_revision,readers FROM backend_releases WHERE id=?',(ident,)).fetchone()
            if prior:
                if (prior['original_document']!=document_id or prior['original_revision']!=expected_revision or json.loads(prior['readers'])!=readers):raise Conflict()
                return {'document_id':prior['document_id'],'disposition':'duplicate'}
            doc=self._document_allowed(db,ctx,document_id)
            if not doc or doc['active_revision_id']!=expected_revision or not self._project_member(db,ctx,project,2):raise Denied()
            sources=db.execute('SELECT s.* FROM enterprise_sources s JOIN enterprise_dependencies d ON d.source_id=s.id WHERE d.document_id=?',(document_id,)).fetchall()
            if not sources or any(s['owner']!=ctx['actor'] or s['external_project']!=project for s in sources):raise Denied()
            for reader in readers:
                probe=dict(ctx,actor=reader)
                if not self._project_member(db,probe,project):raise Denied()
            for source in sources:
                link=db.execute('SELECT connection FROM backend_source_revisions WHERE source_id=?',(source['id'],)).fetchone()
                if link:
                    connection=self._connection(db,ctx,link[0],ingest=False)
                    if not json.loads(connection['capabilities']).get('allow_reviewed_release',False):raise Denied()
            claim=json.loads(db.execute('SELECT claim_json FROM knowledge_revisions WHERE revision_id=?',(expected_revision,)).fetchone()[0])
            scope=_scope(ctx['tenant'],project,'team',ctx['actor'])
            db.execute('INSERT INTO memories(id,session,project,body,kind,created,turn) VALUES(?,?,?,?,?,?,?)',
                (ident,'reviewed-release',scope,json.dumps(claim),'ReviewedRelease',time.time(),''))
            result=accept_local_candidate(db,ident,scope,'reviewed-release',claim['title'],claim['lesson'],[s['id'] for s in sources])
            db.execute('INSERT INTO enterprise_documents(id,tenant,internal_project,revision,created) VALUES(?,?,?,?,?)',
                (result['document_id'],ctx['tenant'],scope,result['revision_id'],time.time()))
            db.executemany('INSERT INTO enterprise_dependencies VALUES(?,?)',((result['document_id'],s['id']) for s in sources))
            dependencies={s['id']:[s['source_version'],s['policy_version']] for s in sources}
            db.execute('INSERT INTO backend_releases VALUES(?,?,?,?,?,?,?,?,?)',
                (ident,result['document_id'],document_id,expected_revision,ctx['tenant'],ctx['actor'],json.dumps(readers),json.dumps(dependencies),time.time()))
            self._audit(db,ctx,'reviewed_release','accepted',result['document_id'],1)
            return {'document_id':result['document_id'],'revision':result['revision_id'],'disposition':'accepted'}
