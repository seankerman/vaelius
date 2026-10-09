"""User-private preference metadata over canonical versioned knowledge.

Preference values/evidence live in knowledge_revisions, never a second corpus.
The metadata table only establishes verified subject, applicability and validity.
"""
from __future__ import annotations
import hashlib
import json
import re
import time


def validate_preference_candidate(text, candidate):
    """Conservative admission for explicit durable first-person preferences.

    This validates observer output; it does not infer identity from prose. The
    calling backend must independently establish the source's verified speaker.
    Unsupported or ambiguous speech remains evidence without becoming a default.
    """
    if not isinstance(candidate,dict) or set(candidate)-{'key','value','scope','project','task','quote'}:
        raise ValueError('invalid_preference_candidate')
    key,value,quote,scope=(candidate.get(k) for k in ('key','value','quote','scope'))
    if (not isinstance(key,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',key)
            or not isinstance(value,str) or not 1<=len(value)<=400
            or not isinstance(quote,str) or not 10<=len(quote)<=1200
            or quote not in text or value.lower() not in quote.lower()
            or scope not in ('user','project','task')):
        raise ValueError('ungrounded_preference')
    # Require the quoted span to be the actual first-person sentence. A coworker
    # quotation or an instruction for only this operation must never be promoted.
    if re.search(r'\b(said|says|wrote|quoted|coworker|colleague)\b|^\s*>|[“”]',quote,re.I):
        raise ValueError('quoted_or_other_speaker_preference')
    if re.search(r'\b(just|only) for (this|the) (task|request|turn)\b|\bthis time\b',quote,re.I):
        raise ValueError('one_time_instruction')
    if re.search(r'\bfor this (task|request|turn)\b',quote,re.I):
        raise ValueError('one_time_instruction')
    if re.search(r'\b(not|no|except|avoid)\s+'+re.escape(value)+r'\b',quote,re.I):
        raise ValueError('negated_preference_value')
    if not re.search(r'\bI (prefer|like|want)\b|\b(please )?remember\b|\balways\b',quote,re.I):
        raise ValueError('durability_not_explicit')
    if scope == 'project':
        project=candidate.get('project')
        if not isinstance(project,str) or not project or project.lower() not in quote.lower():
            raise ValueError('ungrounded_project_scope')
    if scope == 'task':
        task=candidate.get('task')
        if (not isinstance(task,str) or not task or task.lower() not in quote.lower()
                or not re.search(r'\bremember\b',quote,re.I)):
            raise ValueError('ungrounded_task_scope')
    if scope=='user' and (candidate.get('project') or candidate.get('task')):
        raise ValueError('ambiguous_preference_scope')
    if scope=='user' and re.search(r'\bfor (?:the )?[A-Za-z0-9_-]+ project\b',quote,re.I):
        raise ValueError('narrow_preference_scope')
    return {'key':key,'value':value,'scope':scope,'project':candidate.get('project'),
            'task':candidate.get('task'),'quote':quote}


def select_preference_values(rows, *, project=None, task=None, explicit=None, required=None):
    chosen={}
    ranked={}
    for row in rows:
        scope=row['scope']
        if scope=='project' and row.get('project')!=project: continue
        if scope=='task' and (row.get('task')!=task or (row.get('project') and row['project']!=project)): continue
        rank={'user':0,'project':1,'task':2}[scope]
        if rank>=ranked.get(row['key'],-1):
            chosen[row['key']]=row['value'];ranked[row['key']]=rank
    chosen.update(explicit or {})
    chosen.update(required or {})
    return chosen


class CloudPreferencesMixin:
    def _invalidate_source(self,db,source_id,reason):
        super()._invalidate_source(db,source_id,reason)
        db.execute('UPDATE cloud_preferences SET valid_until=%s WHERE source_id=%s AND valid_until IS NULL',
            (time.time(),source_id))
        db.execute("UPDATE cloud_preference_candidates SET status='held' WHERE source_id=%s",(source_id,))

    def _purge_deleted_source(self,db,source_id):
        super()._purge_deleted_source(db,source_id)
        db.execute('UPDATE cloud_preferences SET valid_until=%s WHERE source_id=%s AND valid_until IS NULL',
            (time.time(),source_id))
        db.execute("UPDATE cloud_preference_candidates SET candidate_json='[]',status='held' WHERE source_id=%s",(source_id,))
        if db.table_exists('backend_provider_returns'):
            jobs={r['id'] for r in db.execute('''SELECT b.id,j.source_ids FROM backend_jobs b
                JOIN curation_episode_jobs j ON j.id=b.episode_job''') if source_id in json.loads(r['source_ids'])}
            jobs.update(r['id'] for r in db.execute('''SELECT b.id FROM backend_jobs b
                JOIN backend_processing_dependencies d ON d.episode_job=b.episode_job WHERE d.source_id=%s''',(source_id,)))
            for job in jobs:
                db.execute("UPDATE backend_provider_returns SET result='{}',provider_handle=NULL WHERE job_id=%s",(job,))

    def _preference_source_allowed(self,db,ctx,source):
        if not ctx.get('processing_connection'):return self._visible_source(db,ctx,source)
        # The backend's explicit connection processing consent is separate from
        # an ingest-only plugin's read/correction scopes. This private path is
        # never enabled by customer JSON, and grants no generic read capability.
        if not source or source['owner']!=ctx['actor'] or source['tenant']!=ctx['tenant'] or not source['active']:
            return False
        link=db.execute('SELECT connection FROM backend_source_revisions WHERE source_id=%s',(source['id'],)).fetchone()
        if not link or link['connection']!=ctx['processing_connection']:return False
        connection=db.execute('SELECT * FROM backend_connections WHERE id=%s',(link['connection'],)).fetchone()
        return bool(connection and connection['active'] and connection['owner']==ctx['actor']
            and connection['permission_observed']+connection['freshness_seconds']>time.time()
            and self._connection_executor_authorized(db,connection))

    def _observed_preference_candidates(self,db,ctx,source_id,document_ids):
        self.current_identity(db,ctx)
        source=db.execute('SELECT * FROM enterprise_sources WHERE id=%s',(source_id,)).fetchone()
        memory=db.execute('SELECT * FROM memories WHERE id=%s',(source_id,)).fetchone()
        if (not source or not memory or source['owner']!=ctx['actor']
                or memory['kind']!='UserPromptSubmit' or not self._preference_source_allowed(db,ctx,source)):
            return []
        selected=[]
        for ident in document_ids:
            row=db.execute('''SELECT d.document_id,r.claim_json FROM knowledge_documents d
                JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
                JOIN knowledge_support s ON s.revision_id=r.revision_id
                WHERE d.document_id=%s AND d.lifecycle='active' AND s.source_memory_id=%s''',
                (ident,source_id)).fetchone()
            if row:
                claim=json.loads(row['claim_json'])
                rendered=str(claim.get('lesson',''))+' '+str(claim.get('title',''))
                if re.search(r'prefer|default|always use|use .+for .+projects',rendered,re.I):
                    selected.append((ident,rendered))
        candidates=[]
        for line in re.split(r'(?<=[.!?])\s+|\n',memory['body']):
            if not re.search(r'\bI prefer\b|\balways use\b',line,re.I):continue
            families=[('test_runner',r'\b(pytest|unittest|vitest|jest)\b'),
                ('programming_language',r'\b(Python|TypeScript|Rust|JavaScript)\b'),
                ('formatting',r'\b(tabs|spaces|black|ruff|prettier)\b'),
                ('response_style',r'\b(concise|brief|detailed|verbose)\b')]
            for key,pattern in families:
                match=re.search(pattern,line,re.I)
                if not match:continue
                candidate={'key':key,'value':match.group(1),'scope':'user','quote':line}
                project_match=re.search(r'\bfor (?:the )?([A-Za-z0-9_-]+) project\b',line,re.I)
                if project_match:candidate.update(scope='project',project=project_match.group(1))
                try:validate_preference_candidate(memory['body'],candidate)
                except ValueError:continue
                supported=[ident for ident,rendered in selected if match.group(1).lower() in rendered.lower()]
                if supported:candidates.append((candidate,supported))
        return candidates

    def mark_observed_preference_candidates(self,db,ctx,source_ids,document_ids):
        """Hold model-selected preferences in the canonical install transaction.

        The pending record is an authorization marker, not searchable knowledge.
        Crash/retry between installation and private promotion remains fail-closed.
        """
        marked=0
        for source_id in source_ids:
            candidates=self._observed_preference_candidates(db,ctx,source_id,document_ids)
            by_document={}
            for candidate,selected in candidates:
                for document in selected:by_document.setdefault(document,[]).append(candidate)
            for document,items in by_document.items():
                db.execute('''INSERT INTO cloud_preference_candidates
                    (document_id,source_id,owner,candidate_json,status,created) VALUES(%s,%s,%s,%s,'pending',%s)
                    ON CONFLICT(document_id,source_id) DO UPDATE SET owner=excluded.owner,
                    candidate_json=excluded.candidate_json,status='pending',created=excluded.created''',
                    (document,source_id,ctx['actor'],json.dumps(items),time.time()))
                marked+=1
        return marked

    def curate_observed_preferences(self,ctx,source_id,document_ids):
        """Conservative bridge for the existing durable observer schema.

        Only a model-selected canonical document with this source as evidence
        permits promotion. Current automatic families are test runner, language,
        formatting and response style; unsupported preferences remain evidence.
        A selected shared preference document is retired after private admission.
        """
        from agenthub.processing.knowledge import refresh_index
        with self.open() as state:
            candidates=self._observed_preference_candidates(state.db,ctx,source_id,document_ids)
        results=[]
        for candidate,selected_ids in candidates:
            result=self.curate_preference(ctx,source_id,candidate,_processing=bool(ctx.get('processing_connection')))
            with self.delivery_lock(),self.open() as state,state.db:
                self.current_identity(state.db,ctx)
                for ident in selected_ids:
                    if ident==result['document_id']:continue
                    state.db.execute("UPDATE knowledge_documents SET lifecycle='superseded',active_revision_id=NULL WHERE document_id=%s",(ident,))
                    state.db.execute("UPDATE enterprise_documents SET active=0,blocked_reason='private_preference_promoted' WHERE id=%s",(ident,))
                    refresh_index(state.db,ident)
                    self._invalidate_preference_dependents(state.db,ident)
                    state.db.execute("UPDATE cloud_preference_candidates SET status='promoted' WHERE document_id=%s AND source_id=%s",(ident,source_id))
            results.append({**result,'retired_shared_documents':selected_ids,'key':candidate['key']})
        return results

    def _document_allowed(self,db,ctx,document_id):
        if db.execute("SELECT 1 FROM cloud_preference_candidates WHERE document_id=%s AND status IN ('pending','held')",(document_id,)).fetchone():
            return None
        preference=db.execute('''SELECT * FROM cloud_preferences WHERE document_id=%s
            AND valid_until IS NULL ORDER BY valid_from DESC LIMIT 1''',(document_id,)).fetchone()
        # Any historical preference document is private even after withdrawal.
        historical=preference or db.execute('SELECT * FROM cloud_preferences WHERE document_id=%s LIMIT 1',(document_id,)).fetchone()
        if not historical: return super()._document_allowed(db,ctx,document_id)
        if not preference or preference['tenant']!=ctx['tenant'] or preference['owner']!=ctx['actor']:
            return None
        doc=db.execute('''SELECT ed.*,kd.lifecycle,kd.active_revision_id FROM enterprise_documents ed
            JOIN knowledge_documents kd ON kd.document_id=ed.id WHERE ed.id=%s''',(document_id,)).fetchone()
        if (not doc or not doc['active'] or doc['lifecycle']!='active'
                or doc['active_revision_id']!=preference['revision_id']): return None
        sources=db.execute('''SELECT s.* FROM enterprise_sources s JOIN enterprise_dependencies d
            ON d.source_id=s.id WHERE d.document_id=%s''',(document_id,)).fetchall()
        if not sources or not all(self._visible_source(db,ctx,s) for s in sources): return None
        return doc

    def curate_preference(self,ctx,source_id,candidate,*,replace_document=None,_processing=False):
        """Admit a source-grounded observer candidate for its verified speaker.

        UserPromptSubmit has a server-attributed owner from authenticated capture.
        Generic conversation author strings require a separate verified binding;
        this milestone holds those candidates rather than treating text as identity.
        """
        from agenthub.enterprise import Denied, _digest
        from agenthub.processing.knowledge import apply_resolved_observation
        if not _processing:self._need(ctx,'correct')
        elif not ctx.get('processing_connection'):raise Denied()
        now=time.time()
        with self.delivery_lock(),self.open() as state,state.db:
            db=state.db
            self.current_identity(db,ctx)
            source=db.execute('SELECT * FROM enterprise_sources WHERE id=%s',(source_id,)).fetchone()
            memory=db.execute('SELECT * FROM memories WHERE id=%s',(source_id,)).fetchone()
            if (not source or not memory or source['tenant']!=ctx['tenant']
                    or source['owner']!=ctx['actor'] or not self._preference_source_allowed(db,ctx,source)
                    or memory['kind']!='UserPromptSubmit'):
                raise Denied()
            admitted=validate_preference_candidate(memory['body'],candidate)
            if admitted['project'] and not self._project_member(db,ctx,admitted['project']):raise Denied()
            scope='enterprise:'+_digest(json.dumps([ctx['tenant'],'user_preferences',ctx['actor']]))
            if replace_document is None:
                active=db.execute('''SELECT document_id FROM cloud_preferences WHERE tenant=%s AND owner=%s
                    AND preference_key=%s AND scope=%s AND project IS NOT DISTINCT FROM %s
                    AND task IS NOT DISTINCT FROM %s AND valid_until IS NULL''',
                    (ctx['tenant'],ctx['actor'],admitted['key'],admitted['scope'],admitted['project'],admitted['task'])).fetchone()
                if active:replace_document=active['document_id']
            # Source-backed extraction identity is stable across transport and
            # postcommit bridge retries; discovering the current target later
            # must not turn one observation into an extra correction revision.
            ident=_digest(json.dumps([source_id,admitted],sort_keys=True))
            prior=db.execute('SELECT * FROM cloud_preferences WHERE origin_id=%s',(ident,)).fetchone()
            if prior:
                if replace_document and replace_document!=prior['document_id']:raise Denied()
                return {'document_id':prior['document_id'],'revision_id':prior['revision_id'],'disposition':'duplicate'}
            if replace_document:
                target=db.execute('''SELECT * FROM cloud_preferences WHERE document_id=%s AND valid_until IS NULL''',(replace_document,)).fetchone()
                if (not target or target['owner']!=ctx['actor'] or target['tenant']!=ctx['tenant']
                        or target['preference_key']!=admitted['key'] or target['scope']!=admitted['scope']
                        or target['project']!=admitted['project'] or target['task']!=admitted['task']):raise Denied()
            db.execute('''INSERT INTO memories(id,session,project,body,kind,created,turn)
                VALUES(%s,%s,%s,%s,'Observation',%s,%s)''',
                (ident,memory['session'],scope,json.dumps(admitted),now,memory['turn']))
            claim={'title':'Preference: '+admitted['key'],'problem':'','lesson':admitted['value'],
                'knowledge_type':'preference','domain':'user_preferences','subjects':[ctx['actor'],admitted['key']],
                'tags':['private_preference'],'outcome':'reported','applicability':admitted['scope'],
                'applicability_constraints':{'project_scope':[admitted['project']] if admitted['project'] else []},
                'evidence':[{'source_id':source_id}], 'evidence_status':'verified_user_explicit_preference',
                'memory_context':{'preference_key':admitted['key'],'preference_value':admitted['value'],
                    'verified_tenant':ctx['tenant'],'verified_user':ctx['actor'],'quote':admitted['quote']}}
            result=apply_resolved_observation(db,ident,scope,memory['session'],claim,
                'CORRECT' if replace_document else 'CREATE',target_document_id=replace_document,now=now)
            if replace_document:
                db.execute('UPDATE cloud_preferences SET valid_until=%s WHERE document_id=%s AND valid_until IS NULL',(now,replace_document))
            db.execute('''INSERT INTO cloud_preferences(origin_id,document_id,revision_id,tenant,owner,
                preference_key,scope,project,task,valid_from,valid_until,source_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)''',
                (ident,result['document_id'],result['revision_id'],ctx['tenant'],ctx['actor'],admitted['key'],
                 admitted['scope'],admitted['project'],admitted['task'],now,source_id))
            db.execute('''INSERT INTO enterprise_documents(id,tenant,internal_project,revision,created)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,active=1''',
                (result['document_id'],ctx['tenant'],scope,result['revision_id'],now))
            if replace_document:
                db.execute('DELETE FROM enterprise_dependencies WHERE document_id=%s',(replace_document,))
            db.execute('INSERT INTO enterprise_dependencies VALUES(%s,%s) ON CONFLICT DO NOTHING',(result['document_id'],source_id))
            self._invalidate_preference_dependents(db,result['document_id'])
            self._audit(db,ctx,'preference','corrected' if replace_document else 'accepted',result['document_id'])
            return {**result,'disposition':'accepted','private_owner':ctx['actor']}

    def _invalidate_preference_dependents(self,db,document):
        # Current source dependencies and context epochs are authoritative. Clear
        # offered receipts so corrected values can arrive after the current turn.
        db.execute('DELETE FROM enterprise_receipts WHERE document_id=%s',(document,))
        if db.table_exists('backend_observers'):
            db.execute('''UPDATE backend_observers SET status='reconstruct',provider_session=NULL,
                pending_session=NULL,checkpoint='{}',context_chars=0,observer_epoch=observer_epoch+1
                WHERE id IN (SELECT j.observer_id FROM backend_jobs j JOIN enterprise_model_inputs i
                    ON i.job_id=j.episode_job WHERE i.related_document_id=%s)''',(document,))

    def withdraw_preference(self,ctx,document_id):
        from agenthub.enterprise import Denied
        from agenthub.processing.knowledge import refresh_index
        with self.delivery_lock(),self.open() as state,state.db:
            self.current_identity(state.db,ctx);self._need(ctx,'withdraw')
            row=state.db.execute('SELECT * FROM cloud_preferences WHERE document_id=%s AND valid_until IS NULL FOR UPDATE',(document_id,)).fetchone()
            if not row or row['tenant']!=ctx['tenant'] or row['owner']!=ctx['actor']:raise Denied()
            state.db.execute('UPDATE cloud_preferences SET valid_until=%s WHERE document_id=%s AND valid_until IS NULL',(time.time(),document_id))
            state.db.execute("UPDATE knowledge_documents SET lifecycle='retired',active_revision_id=NULL WHERE document_id=%s",(document_id,))
            state.db.execute('UPDATE enterprise_documents SET active=0 WHERE id=%s',(document_id,))
            refresh_index(state.db,document_id)
            self._invalidate_preference_dependents(state.db,document_id)
            self._audit(state.db,ctx,'preference_withdrawal','applied',document_id)
        return {'disposition':'withdrawn','document_id':document_id}

    def set_required_preference(self,ctx,key,value,*,project=None):
        """Settings-only organization requirements; does not read private memory."""
        from agenthub.enterprise import Denied
        self._need(ctx,'settings')
        if (not isinstance(key,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',key)
                or (value is not None and (not isinstance(value,str) or not 1<=len(value)<=400))):
            raise ValueError('invalid_required_preference')
        with self.delivery_lock(),self.open() as state,state.db:
            self.current_identity(state.db,ctx)
            admin=state.db.execute('SELECT settings_admin FROM enterprise_principals WHERE tenant=%s AND id=%s',
                (ctx['tenant'],ctx['principal'])).fetchone()
            if not admin or not admin['settings_admin']:raise Denied()
            if project and not state.db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=%s AND id=%s AND active=1',
                (ctx['tenant'],project)).fetchone():raise Denied()
            if value is None:
                state.db.execute('DELETE FROM cloud_required_preferences WHERE preference_key=%s AND project=%s',(key,project or ''))
            else:
                state.db.execute('''INSERT INTO cloud_required_preferences VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT(preference_key,project) DO UPDATE SET value=excluded.value,
                    updated=excluded.updated,actor=excluded.actor''',(key,project or '',value,time.time(),ctx['actor']))
            self._audit(state.db,ctx,'preference_requirement','removed' if value is None else 'set',key)
        return {'disposition':'removed' if value is None else 'set','key':key,'project':project}

    def preferences(self,ctx,*,project,task=None,explicit=None,required=None,session=None,mode='explicit'):
        from agenthub.enterprise import Denied
        if mode not in ('explicit','automatic'):raise ValueError('invalid_preference_mode')
        with self.open() as state:
            db=state.db;self.current_identity(db,ctx);self._need(ctx,'read')
            if not self._project_member(db,ctx,project):raise Denied()
            rows=db.execute('''SELECT p.*,r.claim_json FROM cloud_preferences p
                JOIN knowledge_documents d ON d.document_id=p.document_id AND d.lifecycle='active'
                JOIN knowledge_revisions r ON r.revision_id=p.revision_id AND d.active_revision_id=p.revision_id
                WHERE p.tenant=%s AND p.owner=%s AND p.valid_until IS NULL ORDER BY p.valid_from''',
                (ctx['tenant'],ctx['actor'])).fetchall()
            visible=[]
            for row in rows:
                if row['scope']=='project' and row['project']!=project:continue
                if row['scope']=='task' and (row['task']!=task or (row['project'] and row['project']!=project)):continue
                if not self._document_allowed(db,ctx,row['document_id']):continue
                if mode=='automatic' and session and self._preference_in_context(db,ctx,session,row):continue
                value=json.loads(row['claim_json'])['memory_context']['preference_value']
                visible.append({'key':row['preference_key'],'value':value,'scope':row['scope'],
                    'project':row['project'],'task':row['task'],'document_id':row['document_id'],
                    'revision_id':row['revision_id'],'source_id':row['source_id']})
            enforced={r['preference_key']:r['value'] for r in db.execute('''SELECT * FROM cloud_required_preferences
                WHERE project='' OR project=%s ORDER BY CASE WHEN project='' THEN 0 ELSE 1 END''',(project,))}
            enforced.update(required or {})
            return {'values':select_preference_values(visible,project=project,task=task,explicit=explicit,required=enforced),
                    'preferences':visible,'private_owner':ctx['actor']}

    def _preference_in_context(self,db,ctx,session,row):
        from agenthub.processing.context_delivery import current_epoch
        receiver=self._receiver(ctx,session)
        epoch=str(current_epoch(db,receiver)['epoch'])
        # Ingestion namespaces the original host session separately from delivery
        # identity. Suppress a preference just stated in the same un-compacted
        # conversation even before an offered receipt exists.
        original=hashlib.sha256(json.dumps([ctx['tenant'],ctx['enrollment'],session]).encode()).hexdigest()
        if epoch=='0' and db.execute('SELECT 1 FROM memories WHERE id=%s AND session=%s',
            (row['source_id'],original)).fetchone():return True
        if epoch=='0':
            linked=db.execute('SELECT payload FROM backend_source_revisions WHERE source_id=%s',(row['source_id'],)).fetchone()
            if linked and json.loads(linked['payload']).get('conversation')==session:return True
        return bool(db.execute('''SELECT 1 FROM enterprise_receipts WHERE tenant=%s AND principal=%s
            AND session=%s AND epoch=%s AND document_id=%s AND revision=%s''',
            (ctx['tenant'],ctx['principal'],receiver,epoch,row['document_id'],row['revision_id'])).fetchone())
