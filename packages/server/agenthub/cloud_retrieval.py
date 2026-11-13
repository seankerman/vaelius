"""Authorized PostgreSQL lexical/vector candidates over source passages, and delivery.

HNSW supports bounded nearest-neighbor retrieval; PostgreSQL applies current
permissions before returning candidates. Embedding scores never establish truth.
"""
import hashlib
import json
import re
import time
import uuid
from agentclient.cleaning import terms
from agenthub.processing.semantic import MODEL_KEY,DIMENSION,normalize_vector
from agenthub.enterprise import Denied
from agenthub.request_timing import timed, span

FILTERS={'domain','knowledge_type','subject','event_day'}

def _vector(values):
    normalized=normalize_vector(values,DIMENSION)
    return '['+','.join(str(v) for v in normalized)+']'


def lexical_terms(query):
    """Remove framing from text rank, preserving the full query elsewhere.

    Counting reported-speech framing as topical evidence can outrank the
    requested fact. Reuse canonical function words, retaining content-bearing
    status/version/recovery concepts and quoted identifiers. This is candidate
    generation, never an answerability decision.
    """
    from agenthub.processing.retrieval import GENERIC,subject_query
    ignored=GENERIC-{'fixture','version','public','configuration','config','setting','settings',
        'successful','recover','recovery','repair','error','failed','failure','check','steps','task'}
    if re.search(r'\b(?:agent|assistant)\s+(?:report\w*|say\w*|said|stat\w*|mention\w*)\b',query,re.I):
        ignored=ignored|{'report','reports','reported','say','says','said','state','stated','mention','mentioned'}
    quoted={term for text in re.findall(r'"([^"\n]+)"',query) for term in terms(text)}
    return [term for term in terms(subject_query(query)) if term not in ignored or term in quoted]

def rank_scoped_candidates(db,query,*,prefix='',scope_join='',ranked_where='TRUE',
                           prefix_args=(),ranked_args=(),filter_join='',limit=20,
                           lexical=True,vector=True,embedder=None,model_key=MODEL_KEY,
                           lexical_weight=1,vector_weight=1,indexed_lexical=False):
    """Shared PostgreSQL scoring/fusion after caller-established authorization.

    Production supplies its compiled policy scope. Historical evaluation may
    supply connection-local temporary tables only after canonical batch source
    validation. This function grants no permissions and does not cache them.
    Candidates are ordered by score alone, across everything the caller's scope
    admits: no workspace is ranked ahead of another.
    """
    match=' OR '.join('"'+w.replace('"','""')+'"' for w in lexical_terms(query))
    # Only the service's restricted transaction uses the fixed database entry
    # point. Disposable evaluation relations retain their caller-established scope.
    text_relation='search_lexical_matches(?)' if indexed_lexical else 'knowledge_fts'
    text_score='f.score' if indexed_lexical else "ts_rank_cd(to_tsvector('simple',f.body),websearch_to_tsquery('simple',?))"
    text_match='TRUE' if indexed_lexical else "to_tsvector('simple',f.body) @@ websearch_to_tsquery('simple',?)"
    text_args=(*prefix_args,match,*ranked_args,*(() if indexed_lexical else (match,)),limit)
    with span('lexical_sql'):
        lexical=db.execute(prefix+'''SELECT ranked.document_id,ranked.revision_id,r.claim_json,ranked.score,
            ranked.source_project
            FROM (SELECT d.document_id,d.active_revision_id revision_id,ed.source_project,'''+text_score+''' score
            FROM knowledge_documents d
            JOIN '''+text_relation+''' f ON f.document_id=d.document_id AND f.revision_id=d.active_revision_id
            JOIN enterprise_documents ed ON ed.id=d.document_id
            '''+filter_join+scope_join+' WHERE '+ranked_where+' AND '+text_match+'''
            ORDER BY score DESC,d.document_id LIMIT ?) ranked
            JOIN knowledge_revisions r ON r.revision_id=ranked.revision_id
            ORDER BY ranked.score DESC,ranked.document_id''',text_args).fetchall() if match and lexical else []
    semantic=[]
    if vector and embedder is not None:
        active=db.execute('SELECT generation_id,model_key FROM cloud_vector_state WHERE singleton=1').fetchone()
        if active and active['model_key']==model_key:
            with span('embedding'):
                values=embedder.embed_queries([query]);query_vector=_vector(values[0])
            with span('vector_sql'):
                # Iterative scans compensate for generation, revision and RLS
                # filtering inside PostgreSQL. Never fetch denied rows to Python.
                db.execute("SELECT set_config('hnsw.iterative_scan','strict_order',true), "
                    "set_config('hnsw.ef_search','100',true), "
                    "set_config('hnsw.max_scan_tuples','20000',true)")
                # Keep the vector scan as the ordered relation. The correlated
                # scope check must not flatten into a document-first join that
                # forces a corpus sort and prevents the HNSW access path.
                # Fence the single document-key lookup before applying candidate
            # arrays. Otherwise PostgreSQL can combine ANY(thousands of IDs)
            # into the correlated index condition and probe the array per vector.
            semantic=db.execute(prefix+'''SELECT ranked.document_id,ranked.revision_id,r.claim_json,ranked.score,
                    (SELECT e.source_project FROM enterprise_documents e WHERE e.id=ranked.document_id) source_project
                    FROM (SELECT v.document_id,v.revision_id,
                    1-(v.embedding <=> ?::vector) score FROM cloud_document_vectors v
                    WHERE EXISTS(SELECT 1 FROM (
                    SELECT * FROM knowledge_documents WHERE document_id=v.document_id OFFSET 0) d
                    JOIN enterprise_documents ed ON ed.id=d.document_id '''+filter_join+scope_join+' WHERE d.document_id=v.document_id AND d.active_revision_id=v.revision_id AND '+ranked_where+'''
                    OFFSET 0) AND v.generation_id=? ORDER BY v.embedding <=> ?::vector LIMIT ?) ranked
                    JOIN knowledge_revisions r ON r.revision_id=ranked.revision_id
                    ORDER BY ranked.score DESC,ranked.document_id''',
                    (*prefix_args,query_vector,*ranked_args,active['generation_id'],query_vector,limit)).fetchall()
    fused={}
    for name,rows in [('lexical',lexical),('vector',semantic)]:
        for position,row in enumerate(rows):
            ident=row['document_id'];item=fused.setdefault(ident,dict(row,rrf=0,channels=[]))
            weight=lexical_weight if name=='lexical' else vector_weight
            item['rrf']+=weight/(60+position+1);item['channels'].append(name)
            if name=='vector':item['cosine']=row['score']
    return sorted(fused.values(),key=lambda x:(-x['rrf'],x['document_id']))[:limit]

class HybridRetrievalMixin:
    # Source passages are the only searched corpus (the curated corpus was removed);
    # the attribute remains because `vaelius status` reports it.
    retrieval_corpus='sources'
    semantic_model_key=MODEL_KEY
    hybrid_enabled=False
    semantic_embedder=None
    # Internal, bounded experiment controls. Defaults preserve installed B17.
    retrieval_candidate_limit=20
    retrieval_numeric_mode='legacy'
    retrieval_lexical_weight=1
    retrieval_vector_weight=1
    retrieval_lexical_enabled=True
    retrieval_authorization_shape='compiled'

    def reindex_vectors(self,*,embedder=None,max_documents=10000):
        model=embedder or self.semantic_embedder
        if model is None:raise ValueError('local_embedding_model_required')
        if not 1<=max_documents<=100000:raise ValueError('embedding_bound')
        generation='vectors-'+uuid.uuid4().hex
        with self.open() as state,state.db:
            rows=state.db.execute('''SELECT d.document_id,d.active_revision_id,r.claim_json
                FROM knowledge_documents d JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
                JOIN enterprise_documents e ON e.id=d.document_id WHERE d.lifecycle='active' AND e.active=1
                AND e.tenant=? ORDER BY d.document_id LIMIT ?''',
                (self.tenant_id,max_documents+1)).fetchall()
            if len(rows)>max_documents:raise ValueError('embedding_document_bound')
            state.db.execute('INSERT INTO cloud_vector_generations(id,model_key,dimension,status,created) VALUES(?,?,?,\'building\',?)',
                (generation,self.semantic_model_key,DIMENSION,time.time()))
        try:
            for first in range(0,len(rows),16):
                batch=rows[first:first+16];texts=[self._vector_text(json.loads(r['claim_json'])) for r in batch]
                vectors=model.embed_documents(texts)
                if len(vectors)!=len(batch):raise ValueError('embedding_batch_shape')
                with self.open() as state,state.db:
                    for row,vector,text in zip(batch,vectors,texts):
                        current=state.db.execute('SELECT active_revision_id,lifecycle FROM knowledge_documents WHERE document_id=?',(row['document_id'],)).fetchone()
                        if not current or current['lifecycle']!='active' or current['active_revision_id']!=row['active_revision_id']:
                            raise ValueError('embedding_source_changed')
                        state.db.execute('INSERT INTO cloud_document_vectors VALUES(?,?,?,?,?::vector)',
                            (generation,row['document_id'],row['active_revision_id'],hashlib.sha256(text.encode()).hexdigest(),_vector(vector)))
            with self.delivery_lock(),self.open() as state,state.db:
                stale=state.db.execute('''SELECT 1 FROM cloud_document_vectors v JOIN knowledge_documents d ON d.document_id=v.document_id
                    WHERE v.generation_id=? AND (d.lifecycle!='active' OR d.active_revision_id!=v.revision_id) LIMIT 1''',(generation,)).fetchone()
                if stale:raise ValueError('embedding_activation_source_changed')
                state.db.execute("UPDATE cloud_vector_generations SET status='retired' WHERE status='active'")
                state.db.execute("UPDATE cloud_vector_generations SET status='active',completed=?,document_count=? WHERE id=?",(time.time(),len(rows),generation))
                state.db.execute('''INSERT INTO cloud_vector_state VALUES(1,?,?,?) ON CONFLICT(singleton)
                    DO UPDATE SET generation_id=excluded.generation_id,model_key=excluded.model_key,updated=excluded.updated''',(generation,self.semantic_model_key,time.time()))
            from agenthub.cloud_ops import Meter
            Meter(self).metric('embedding:'+generation,'embedding_documents',len(rows),details={'generation':generation})
            return {'generation':generation,'model_key':self.semantic_model_key,'dimension':DIMENSION,'documents':len(rows),'search':'pgvector_hnsw_eligible'}
        except BaseException:
            with self.open() as state,state.db:state.db.execute("UPDATE cloud_vector_generations SET status='failed' WHERE id=?",(generation,))
            raise
    @staticmethod
    def _vector_text(claim):return '\n'.join(str(claim.get(k,'')) for k in ('title','lesson','applicability'))

    def _document_allowed(self,db,ctx,document_id):
        # Resolve current identity once per document, rather than opening the
        # control database for every dependency. Reuse the same scoped SQL
        # predicate used before ranking; never cache authorization across requests.
        if db.execute('SELECT 1 FROM cloud_preferences WHERE document_id=? LIMIT 1',(document_id,)).fetchone():
            return super()._document_allowed(db,ctx,document_id)
        from types import SimpleNamespace
        try:where,args=self._authorization_predicate(SimpleNamespace(db=db),ctx,candidate_ids=[document_id])
        except Denied:return None
        row=db.execute('''SELECT ed.*,d.lifecycle,d.active_revision_id FROM enterprise_documents ed
            JOIN knowledge_documents d ON d.document_id=ed.id WHERE d.document_id=? AND '''+where,
            (document_id,*args)).fetchone()
        if row:return row
        # Explicit releases have stricter source/version semantics, preserved
        # by their existing oracle. User preferences keep their separate scope.
        if db.execute('SELECT 1 FROM backend_releases WHERE document_id=? LIMIT 1',(document_id,)).fetchone():
            return super()._document_allowed(db,ctx,document_id)
        return None

    def _authorization_predicate(self, state, ctx, project=None, *, candidate_ids=None):
        self.current_identity(state.db, ctx)
        self._label(state.db, ctx)
        self._need(ctx, 'read')
        scopes = self._readable_scopes(state.db, ctx, project)
        sql = "ed.tenant=? AND ed.active=1 AND d.lifecycle='active'\n            AND (ed.internal_project=ANY(?::text[]) OR EXISTS(SELECT 1 FROM cloud_preferences pf\n                WHERE pf.document_id=d.document_id AND pf.tenant=? AND pf.owner=? AND pf.valid_until IS NULL\n                AND pf.revision_id=d.active_revision_id AND (pf.scope='user' OR (pf.scope='project' AND pf.project=?))))\n            AND (NOT EXISTS(SELECT 1 FROM knowledge_generation_documents gd WHERE gd.document_id=d.document_id)\n                OR EXISTS(SELECT 1 FROM knowledge_generation_documents gd JOIN knowledge_generations g\n                    ON g.generation_id=gd.generation_id WHERE gd.document_id=d.document_id AND g.status='active'))\n            AND NOT EXISTS(SELECT 1 FROM cloud_preference_candidates pc WHERE pc.document_id=d.document_id AND pc.status IN ('pending','held'))\n            AND NOT EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id\n                AND (pf.owner!=? OR pf.tenant!=?))\n            AND (NOT EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id)\n                OR EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id\n                    AND pf.valid_until IS NULL AND pf.revision_id=d.active_revision_id))\n            "
        from agenthub.search_permissions import predicate
        compact, policy_args = predicate(ctx, self._reader_workspaces(state.db, ctx))
        sql += compact
        args = (ctx['tenant'], scopes, ctx['tenant'], ctx['actor'], project, ctx['actor'], ctx['tenant'], *policy_args)
        return (sql, args)

    def _authorized_documents(self,state,ctx,project=None):
        where,args=self._authorization_predicate(state,ctx,project)
        rows=state.db.execute('SELECT ed.id FROM enterprise_documents ed JOIN knowledge_documents d ON d.document_id=ed.id WHERE '+where,args)
        allowed={r[0] for r in rows}
        # Explicit reviewed releases are scarce and bind exact source versions;
        # use their existing canonical rule rather than approximate its semantics.
        releases=state.db.execute('SELECT document_id FROM backend_releases WHERE tenant=?',(ctx['tenant'],))
        for row in releases:
            if self._document_allowed(state.db,ctx,row[0]):allowed.add(row[0])
        return sorted(allowed)

    @timed('candidates')
    def candidates(self, ctx, query, *, project=None, filters=None, limit=10, vector=True, lexical=True,
                   scope='project'):
        """Authorized candidates ranked by score across everything the request may read.

        No workspace is ranked ahead of another: the current workspace's items
        compete on score with the rest. `scope` narrows retrieval, never
        authorization. With a project, 'project' (the default) reaches that
        project's documents and documents from other projects shared with the whole
        organization; 'all' adds every other project the reader may read. Without a
        project nothing is narrowed. `filters['workspaces']` (the `read` filter of a
        session label, or a one-off list) keeps only items with a source in one of
        those workspaces, after `_recall_workspaces` capped it by what the request
        may read.
        """
        self._need(ctx, 'read')
        if scope not in ('project', 'all'):
            raise ValueError('invalid_search_scope')
        filters = filters or {}
        if not 1 <= limit <= 100:
            raise ValueError('candidate_limit')
        with self.open() as state:
            exact_ids = [r[0] for r in state.db.execute("SELECT d.document_id FROM knowledge_revisions r\n                JOIN knowledge_documents d ON d.active_revision_id=r.revision_id\n                WHERE lower(r.claim_json::jsonb->>'title')=lower(?)\n                UNION SELECT document_id FROM knowledge_documents WHERE document_id=?", (query, query))]
            numeric_ids = re.findall('(?<![\\w.])\\d{6,}(?![\\w.])', query)
            if self.retrieval_numeric_mode == 'typed':
                numeric_ids = re.findall('\\b(?:record|document|doc|source|issue|ticket|case|dataset\\s+id)\\s+(?:id\\s*)?[:#-]?\\s*(\\d{6,})\\b', query, re.I)
            entity_ids = None
            if numeric_ids and (not exact_ids):
                entity_query = ' & '.join(numeric_ids)
                entity_ids = [r[0] for r in state.db.execute("SELECT f.document_id FROM knowledge_fts f\n                    JOIN knowledge_documents d ON d.document_id=f.document_id AND d.active_revision_id=f.revision_id\n                    WHERE to_tsvector('simple',f.body) @@ to_tsquery('simple',?)", (entity_query,))]
                if not entity_ids:
                    self.current_identity(state.db, ctx)
                    return []
            from agenthub.search_reader import restrict
            # The `read` filter (a convenience, capped by what the request may read):
            # resolved before the restricted reader role is set and applied on top of
            # the already authorized candidate query, by row-level security
            # (`recall_filter`) and by the indexed prefilter below. Without a filter
            # this runs no statement; `restrict` then checks current identity before
            # anything is read.
            keep = self._recall_workspaces(state.db, ctx, filters.get('workspaces'), project)
            # A session label decides which workspaces recall reaches (its superset
            # list), so it replaces the cross-project `scope` option of #28.
            labelled = ctx.get('session_label') or ctx.get('session_label_request')
            reach = None if project is None else ('all' if labelled else scope)
            reader = restrict(self, state, ctx, project, reach=reach, recall_filter=keep)
            with reader:
                predicates = ['TRUE', "d.lifecycle='active'"]
                args = []
                predicates.append("ed.representation IN ('raw','source')")
                cutoff=filters.get('source_as_of')
                if cutoff:
                    from agenthub.conversation_context import stamp
                    point=stamp(cutoff)+(86400 if re.fullmatch(r'\d{4}-\d{2}-\d{2}',cutoff) else 0)
                    predicates.append('ed.source_time IS NOT NULL AND ed.source_time<?')
                    args.append(point)
                    if filters.get('event_day'):
                        predicates.append('ed.source_time>=?');args.append(point-86400)
                    if filters.get('time_mode')=='known_at':
                        predicates.append('ed.source_captured<?');args.append(point)
                if entity_ids is not None:
                    predicates.append('d.document_id=ANY(?::text[])')
                    args.append(entity_ids)
                if keep is not None:
                    # source_project is NULL for a document whose sources span several
                    # workspaces; row-level security keeps it when one of them is listed.
                    predicates.append('(ed.source_project=ANY(?::text[]) OR ed.source_project IS NULL)')
                    args.append(keep)
                for key in ('domain', 'knowledge_type'):
                    if filters.get(key):
                        predicates.append("r.claim_json::jsonb->>'" + key + "'=?")
                        args.append(filters[key])
                if filters.get('subject'):
                    predicates.append("EXISTS(SELECT 1 FROM jsonb_array_elements_text(COALESCE(r.claim_json::jsonb->'subjects','[]'::jsonb)) s WHERE lower(s.value)=lower(?))")
                    args.append(filters['subject'])
                where = ' AND '.join(predicates)

                prefix, scope_join, ranked_where, prefix_args, ranked_args = '', '', where, (), args
                exact = state.db.execute(prefix + 'SELECT d.document_id,r.revision_id,r.claim_json,ed.source_project FROM knowledge_documents d\n                    JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id\n                    JOIN enterprise_documents ed ON ed.id=d.document_id ' + scope_join + ' WHERE ' + ranked_where + '\n                    AND d.document_id=ANY(?::text[])\n                    ORDER BY d.document_id LIMIT ?', (*prefix_args, *ranked_args, exact_ids, limit)).fetchall() if exact_ids else []
                if exact:
                    return [dict(row, rrf=1, channels=['exact']) for row in exact]
                filter_join = ' JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id ' if any((filters.get(k) for k in ('domain', 'knowledge_type', 'subject'))) else ''
                return rank_scoped_candidates(state.db, query, prefix=prefix, scope_join=scope_join, ranked_where=ranked_where, prefix_args=prefix_args, ranked_args=ranked_args, filter_join=filter_join, limit=limit, lexical=lexical, vector=vector, embedder=self.semantic_embedder, model_key=self.semantic_model_key, lexical_weight=self.retrieval_lexical_weight, vector_weight=self.retrieval_vector_weight,indexed_lexical=True)

    @timed('search')
    def search(self,ctx,value):
        # Validate advertised additions explicitly while retaining the existing API envelope.
        value=dict(value);filters={key:value.pop(key) for key in FILTERS if key in value}
        for key,item in filters.items():
            if not isinstance(item,str) or not item or len(item)>120:raise ValueError('invalid_search_filter')
        if filters.get('event_day'):
            if value.get('as_of'):raise ValueError('ambiguous_temporal_search')
            value['as_of']=filters['event_day']
        from agentclient.enterprise_contract import validate_search
        validate_search(value)
        if value.get('workspaces') is not None:
            # A one-off recall filter for this search (see `_recall_workspaces`).
            filters['workspaces']=list(value['workspaces'])
        from agenthub.source_index import search
        return search(self,ctx,value,filters)

    def validate_search_delivery(self,ctx,value,result):
        """Short delivery checkpoint after unlocked ranking; preserve history rules."""
        final=[]
        with self.open() as state:
            self.current_identity(state.db,ctx);self._need(ctx,'read')
            from agenthub.search_permissions import current_document_revisions
            current_revisions=current_document_revisions(self,state.db,ctx,result['results'])
            for card in result['results']:
                if current_revisions.get(card['id'])==card['revision']:final.append(card)
        if len(final)==len(result['results']):return result
        output=dict(result,results=final,answerable=False)
        if 'support' in output:output['support']='partial' if final else 'none'
        output['coverage_gaps']=list(dict.fromkeys([*output.get('coverage_gaps',[]),'authorization_changed_before_delivery']))
        bound=1500 if value.get('mode')=='automatic' else 4000
        while output['results'] and len(json.dumps(output,ensure_ascii=True))>bound:output['results'].pop()
        return output
