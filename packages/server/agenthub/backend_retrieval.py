"""Policy-constrained temporal selection and explicitly reviewed derived releases."""

from agenthub.processing.storage import table_exists
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone


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
            if not doc or doc['active_revision_id']!=expected_revision or not self._project_member(db,ctx,project):raise Denied()
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

    def _temporal_policy(self,db,ctx,scopes,*,known_at=False,candidate_ids=None):
        bounded=' AND a.document_id=ANY(?::text[])' if candidate_ids is not None else ''
        args=(ctx['tenant'],candidate_ids) if candidate_ids is not None else (ctx['tenant'],)
        permitted=[r[0] for r in db.execute('''SELECT DISTINCT a.document_id FROM knowledge_temporal_assertions a
            JOIN enterprise_documents e ON e.id=a.document_id WHERE e.tenant=?'''+bounded,args)
            if self._temporal_document_allowed(db,ctx,r[0],known_at=known_at)]
        def source_allowed(ident):
            source=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(ident,)).fetchone()
            if self._visible_source(db,ctx,source):return True
            return self._retained_native_source_allowed(db,ctx,ident)
        return permitted,source_allowed

    def _retained_native_source_allowed(self,db,ctx,source_id):
        """Same current head, policy and explicit denial proof as original bytes."""
        if not table_exists(db,'backend_document_versions'):return False
        from agenthub.document_ingest import DocumentStore
        from agenthub.enterprise import Denied
        from agenthub.source_objects import ObjectMissing
        try:DocumentStore(self,None)._authorize(db,ctx,source_id)
        except (Denied,ObjectMissing):return False
        return True

    def _temporal_document_allowed(self,db,ctx,document_id,*,known_at=False):
        current=self._document_allowed(db,ctx,document_id)
        if current:return current
        if known_at:
            # Supersession preserves a past accepted claim. Current source policy
            # still has to authorize every dependency; withdrawn/deleted sources
            # and private preferences never gain a historical bypass.
            prior=db.execute('''SELECT e.*,d.lifecycle,d.active_revision_id FROM enterprise_documents e
                JOIN knowledge_documents d ON d.document_id=e.id WHERE e.id=?''',
                (document_id,)).fetchone()
            if (prior and prior['tenant']==ctx['tenant'] and prior['active'] and
                    not prior['blocked_reason'] and prior['lifecycle']=='superseded' and
                    prior['internal_project'] in self._readable_scopes(db,ctx) and
                    not (table_exists(db,'cloud_preferences') and db.execute(
                        'SELECT 1 FROM cloud_preferences WHERE document_id=? LIMIT 1',
                        (document_id,)).fetchone())):
                sources=db.execute('''SELECT s.* FROM enterprise_dependencies x
                    JOIN enterprise_sources s ON s.id=x.source_id
                    WHERE x.document_id=?''',(document_id,)).fetchall()
                if sources and (all(self._visible_source(db,ctx,source) for source in sources)
                                or self._release_allowed(db,ctx,document_id,sources)):
                    return prior
        if not table_exists(db,'backend_native_artifacts'):return None
        doc=db.execute('''SELECT e.*,d.lifecycle,d.active_revision_id,n.source_id FROM enterprise_documents e
            JOIN knowledge_documents d ON d.document_id=e.id
            JOIN backend_native_artifacts n ON n.document_id=e.id
            WHERE e.id=? AND n.artifact_kind='native_document' ''',(document_id,)).fetchone()
        if (not doc or doc['tenant']!=ctx['tenant'] or doc['lifecycle']!='active' or
            doc['active'] or doc['blocked_reason']!='source_revision_replaced' or
            doc['internal_project'] not in self._readable_scopes(db,ctx)):return None
        if table_exists(db,'cloud_preferences') and db.execute('SELECT 1 FROM cloud_preferences WHERE document_id=? LIMIT 1',(document_id,)).fetchone():return None
        if table_exists(db,'cloud_preference_candidates') and db.execute("SELECT 1 FROM cloud_preference_candidates WHERE document_id=? AND status IN ('pending','held') LIMIT 1",(document_id,)).fetchone():return None
        mapped=db.execute('SELECT 1 FROM knowledge_generation_documents WHERE document_id=? LIMIT 1',(document_id,)).fetchone()
        active=db.execute('''SELECT 1 FROM knowledge_generation_documents x JOIN knowledge_generations g
            ON g.generation_id=x.generation_id WHERE x.document_id=? AND g.status='active' LIMIT 1''',(document_id,)).fetchone()
        if mapped and not active:return None
        sources=db.execute('SELECT source_id FROM enterprise_dependencies WHERE document_id=?',(document_id,)).fetchall()
        if not sources or any(not self._retained_native_source_allowed(db,ctx,row[0]) for row in sources):return None
        return doc

    def temporal_search(self,state,ctx,value,scopes,*,candidate_ids=None):
        from agenthub.processing.temporal_retrieval import historical_cards
        if not scopes:return {'results':[],'answerable':False,'coverage_gaps':['no_authorized_temporal_evidence']}
        query=value['query']
        if value.get('as_of'):
            explicit_day=value['as_of'][:10]
            dates=re.findall(r'\b\d{4}-\d{2}-\d{2}\b',query)
            if dates and (len(set(dates))!=1 or dates[0]!=explicit_day):
                return {'results':[],'answerable':False,
                        'coverage_gaps':['ambiguous_temporal_cutoff']}
            if not dates:query+=' as of '+value['as_of']
        elif value.get('time_mode')=='current' and not re.search(r'\b(now|current|currently|latest)\b',query,re.I):
            query+=' currently'
        known_mode=value.get('time_mode')=='known_at'
        permitted,source_allowed=self._temporal_policy(state.db,ctx,scopes,known_at=known_mode,candidate_ids=candidate_ids)
        entries=historical_cards(state,query,scopes[0],read_projects=scopes,
            timezone_name='UTC',authorized_document_ids=permitted,
            source_authorizer=source_allowed,time_mode=value.get('time_mode'))
        if entries is None:return None
        results=[]
        for entry in entries:
            item=entry['item'];card=entry['card']
            if not self._temporal_allowed(state.db,ctx,item['assertion_id'],known_at=known_mode):continue
            results.append({'id':item['assertion_id'],'revision':item['revision_id'],
                'title':card.get('title','Temporal knowledge'),
                'lesson':item['answer_text'][:1000],'evidence_status':'source_linked_temporal'})
        output={'results':results,'answerable':bool(results)}
        if not results:output['coverage_gaps']=['no_authorized_supported_temporal_answer']
        bound=1500 if value.get('mode')=='automatic' else 4000
        while results and len(json.dumps(output,ensure_ascii=True))>bound:results.pop();output['answerable']=bool(results)
        return output

    def _temporal_allowed(self,db,ctx,ident,*,known_at=False):
        assertion=db.execute('SELECT * FROM knowledge_temporal_assertions WHERE assertion_id=?',(ident,)).fetchone()
        if not assertion or not self._temporal_document_allowed(db,ctx,assertion['document_id'],known_at=known_at):return None
        evidence=db.execute('SELECT source_memory_id FROM knowledge_temporal_evidence WHERE assertion_id=?',(ident,)).fetchall()
        if not evidence or any(not (self._visible_source(db,ctx,db.execute('SELECT * FROM enterprise_sources WHERE id=?',(r[0],)).fetchone())
            or self._retained_native_source_allowed(db,ctx,r[0])) for r in evidence):return None
        return assertion

    def temporal_detail(self,state,ctx,ident,*,as_of=None,time_mode=None):
        from agenthub.enterprise import Denied
        from agenthub.processing.temporal_retrieval import detail_for_assertion
        if time_mode not in (None,'effective_at','known_at'):
            raise ValueError('invalid_temporal_mode')
        if time_mode=='known_at' and not as_of:
            raise ValueError('known_at_requires_as_of')
        if as_of is not None and (not isinstance(as_of,str) or len(as_of)>40):
            raise ValueError('invalid_temporal_cutoff')
        known_mode=time_mode=='known_at'
        assertion=self._temporal_allowed(state.db,ctx,ident,known_at=known_mode)
        if not assertion:raise Denied()
        scopes=self._readable_scopes(state.db,ctx)
        permitted,source_allowed=self._temporal_policy(state.db,ctx,scopes,known_at=known_mode)
        # Direct assertion ID inspects its supported historical interval explicitly.
        point=as_of if as_of is not None else assertion['valid_from_utc']
        if isinstance(point,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',point):
            point=(datetime.fromisoformat(point).replace(tzinfo=timezone.utc)
                   +timedelta(days=1)-timedelta(microseconds=1)).timestamp()
        if isinstance(point,str) and not re.search(r'(Z|[+-]\d{2}:\d{2})$',point):
            raise ValueError('as_of_requires_offset')
        detail=detail_for_assertion(state,ident,assertion['project'],read_projects=scopes,
            as_of=point,known_at=(point if known_mode else None),
            authorized_document_ids=permitted,source_authorizer=source_allowed)
        if not detail:raise Denied()
        if not self._temporal_allowed(state.db,ctx,ident,known_at=known_mode):raise Denied()
        result={'id':ident,'revision':assertion['revision_id'],'claim':detail}
        if len(json.dumps(result,ensure_ascii=True))>4000:raise ValueError('temporal_detail_too_large')
        return result
