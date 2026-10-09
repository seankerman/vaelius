"""Shared enterprise ingestion, provenance and lifecycle operations."""
from __future__ import annotations

from agenthub.processing.storage import table_exists

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from agentclient.cleaning import SECRET, clean_private
from agentclient.enterprise_capture import redact
from agentclient.enterprise_contract import (VERSION, validate_source, validate_lifecycle, validate_receipt, validate_boundary)


class Denied(Exception): pass
class Conflict(Exception): pass


def _digest(value):
    return hashlib.sha256(value if isinstance(value,bytes) else value.encode()).hexdigest()


def _scope(tenant, project, visibility, owner):
    return "enterprise:" + _digest(json.dumps([tenant, project if visibility != "organization" else "*",
        visibility, owner if visibility == "private" else "*"], separators=(",", ":")))


from agenthub.general_sources import GeneralSourcesMixin
from agenthub.backend_retrieval import RetrievalMixin


class EnterpriseStore(GeneralSourcesMixin,RetrievalMixin):
    def __init__(self, *args, **kwargs):
        raise RuntimeError('PostgreSQL authority required; use CloudStore')

    def open(self):
        raise NotImplementedError('PostgreSQL connection required')

    def _audit(self, db, ctx, operation, disposition, target=None, policy_version=None):
        db.execute("INSERT INTO enterprise_audit(tenant,actor,operation,disposition,target_id,request_id,policy_version,created) VALUES(?,?,?,?,?,?,?,?)",
            (ctx["tenant"], ctx["actor"], operation, disposition, target, ctx.get("request_id"), policy_version, time.time()))

    @contextmanager
    def delivery_lock(self):
        """Serialize policy commits with the final HTTP authorization and send."""
        path=self.home/'policy.lock'
        fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def create_organization(self, tenant):
        if not isinstance(tenant,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",tenant):
            raise ValueError("invalid_tenant")
        with self.open() as state, state.db:
            state.db.execute("INSERT INTO enterprise_organizations VALUES(?) ON CONFLICT DO NOTHING",(tenant,))

    def create_principal(self, tenant, principal, *, settings_admin=False):
        if not isinstance(principal,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",principal):
            raise ValueError("invalid_principal")
        with self.open() as state, state.db:
            state.db.execute("INSERT INTO enterprise_principals(tenant,id,settings_admin) VALUES(?,?,?)",
                (tenant,principal,int(settings_admin)))

    def create_project(self, tenant, project):
        if not isinstance(project,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}",project):
            raise ValueError("invalid_project")
        with self.open() as state, state.db:
            state.db.execute("INSERT INTO enterprise_projects(tenant,id) VALUES(?,?)",(tenant,project))

    def set_membership(self, tenant, project, principal, active):
        with self.delivery_lock(), self.open() as state, state.db:
            state.db.execute("INSERT INTO enterprise_memberships VALUES(?,?,?,?) ON CONFLICT(tenant,project,principal) DO UPDATE SET active=excluded.active",
                (tenant,project,principal,int(bool(active))))
            state.db.execute("INSERT INTO enterprise_audit(tenant,actor,operation,disposition,target_id,created) VALUES(?,?,?,?,?,?)",
                (tenant,'local_operator','membership','granted' if active else 'revoked',project+':'+principal,time.time()))

    def enroll(self, tenant, principal, enrollment, actions, *, acting_for=None):
        allowed={"ingest","read","source_read","feedback","curate","correct","withdraw","policy","audit","settings"}
        if not isinstance(enrollment,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}",enrollment):
            raise ValueError("invalid_enrollment")
        if not actions or set(actions)-allowed:raise ValueError("invalid_actions")
        token=secrets.token_urlsafe(48)
        with self.open() as state, state.db:
            prior=state.db.execute('''SELECT principal,acting_for FROM enterprise_credentials
                WHERE tenant=? AND enrollment=? LIMIT 1''',(tenant,enrollment)).fetchone()
            if prior and (prior['principal']!=principal or prior['acting_for']!=acting_for):
                raise Conflict()
            if acting_for:
                grant=state.db.execute('''SELECT * FROM enterprise_delegations
                    WHERE tenant=? AND principal=? AND acting_for=? AND active=1''',
                    (tenant,principal,acting_for)).fetchone()
                if not grant or not set(actions)<=set(json.loads(grant['actions'])):raise Denied()
            state.db.execute("INSERT INTO enterprise_credentials VALUES(?,?,?,?,?,?,1,?)",
                (_digest(token),tenant,principal,acting_for,enrollment,json.dumps(sorted(set(actions))),time.time()))
        return token

    def set_delegation(self, tenant, principal, acting_for, actions, projects, active):
        allowed={'ingest','read','source_read','feedback','correct','withdraw','policy'}
        if (principal==acting_for or not actions or set(actions)-allowed or
                not projects or any(not isinstance(p,str) or not p for p in projects)):
            raise ValueError('invalid_delegation')
        with self.delivery_lock(), self.open() as state, state.db:
            for project in projects:
                if not state.db.execute('SELECT 1 FROM enterprise_projects WHERE tenant=? AND id=?',
                        (tenant,project)).fetchone():raise ValueError('unknown_project')
            state.db.execute('''INSERT INTO enterprise_delegations
                (tenant,principal,acting_for,actions,projects,active) VALUES(?,?,?,?,?,?)
                ON CONFLICT(tenant,principal,acting_for) DO UPDATE SET
                actions=excluded.actions,projects=excluded.projects,active=excluded.active,
                policy_version=enterprise_delegations.policy_version+1''',
                (tenant,principal,acting_for,json.dumps(sorted(set(actions))),
                 json.dumps(sorted(set(projects))),int(bool(active))))
            state.db.execute('''INSERT INTO enterprise_audit
                (tenant,actor,operation,disposition,target_id,created) VALUES(?,?,?,?,?,?)''',
                (tenant,'local_operator','delegation','granted' if active else 'revoked',
                 principal+':'+acting_for,time.time()))

    def revoke_credential(self, token_digest):
        with self.delivery_lock(), self.open() as state, state.db:
            return bool(state.db.execute("UPDATE enterprise_credentials SET active=0 WHERE digest=?",(token_digest,)).rowcount)

    def require_ready(self):
        marker=self.home/'restore-readiness.json'
        if marker.exists() and not json.loads(marker.read_text()).get('ready_for_use',False):
            raise ValueError('restore_profile_unreconciled')

    def authenticate(self, token, request_id=None):
        try:self.require_ready()
        except ValueError:raise Denied() from None
        if not isinstance(token,str) or not token or len(token)>256:raise Denied()
        with self.open() as state:
            row=state.db.execute("""SELECT c.*,p.active principal_active,
                a.active actor_active FROM enterprise_credentials c
                JOIN enterprise_principals p ON p.tenant=c.tenant AND p.id=c.principal
                LEFT JOIN enterprise_principals a ON a.tenant=c.tenant AND a.id=c.acting_for
                WHERE c.digest=?""",(_digest(token),)).fetchone()
            if not row or not row["active"] or not row["principal_active"] or (row["acting_for"] and not row["actor_active"]):
                raise Denied()
            delegated_projects=None
            actions=set(json.loads(row['actions']))
            if row['acting_for']:
                grant=state.db.execute('''SELECT * FROM enterprise_delegations
                    WHERE tenant=? AND principal=? AND acting_for=? AND active=1''',
                    (row['tenant'],row['principal'],row['acting_for'])).fetchone()
                if not grant:raise Denied()
                actions &= set(json.loads(grant['actions']))
                delegated_projects=set(json.loads(grant['projects']))
            return {"tenant":row["tenant"],"principal":row["principal"],
                "actor":row["acting_for"] or row["principal"],"acting_for":row["acting_for"],
                "enrollment":row["enrollment"],"actions":actions,
                "delegated_projects":delegated_projects,
                "request_id":request_id}

    @staticmethod
    def _need(ctx, action):
        if action not in ctx["actions"]:raise Denied()

    @staticmethod
    def _project_member(db, ctx, project):
        if ctx.get('delegated_projects') is not None and project not in ctx['delegated_projects']:
            return False
        row=db.execute("""SELECT 1 FROM enterprise_memberships m
          JOIN enterprise_projects p ON p.tenant=m.tenant AND p.id=m.project
          WHERE m.tenant=? AND m.project=? AND m.principal=? AND m.active=1 AND p.active=1""",
          (ctx["tenant"],project,ctx["actor"])).fetchone()
        return bool(row)

    def _visible_source(self, db, ctx, source, *, raw=False):
        if not source or source["tenant"]!=ctx["tenant"] or not source["active"]:
            return False
        if (ctx.get('delegated_projects') is not None and
                source['external_project'] not in ctx['delegated_projects']):return False
        if not self._general_source_allowed(db,ctx,source):return False
        if raw:
            return source["owner"]==ctx["actor"] and "source_read" in ctx["actions"]
        if "read" not in ctx["actions"]:return False
        if source["visibility"]=="private":return source["owner"]==ctx["actor"]
        if source["visibility"]=="team":return self._project_member(db,ctx,source["external_project"])
        return True

    def ingest(self, ctx, value):
        self._need(ctx,"ingest");validate_source(value)
        if redact(value)[0] != value:raise ValueError("secret_source_rejected")
        # Explicit enrollment and project grant are the only intake authority.
        with self.delivery_lock(), self.open() as state:
            db=state.db
            if not self._project_member(db,ctx,value["project"]):raise Denied()
            body=value["body"]
            cleaned,reasons=clean_private(body)
            if "possible_secret" in reasons:raise ValueError("secret_source_rejected")
            # Stable tenant/enrollment namespace prevents equal external IDs from
            # colliding in State.enqueue and in the enterprise receipt.
            ident=_digest(json.dumps([ctx["tenant"],ctx["enrollment"],value["external_id"]]))
            payload_hash=_digest(json.dumps(value,sort_keys=True,ensure_ascii=False))
            existing=db.execute("SELECT id,payload_hash FROM enterprise_sources WHERE tenant=? AND enrollment=? AND external_id=?",
                (ctx["tenant"],ctx["enrollment"],value["external_id"])).fetchone()
            if existing:
                if not hmac.compare_digest(existing["payload_hash"],payload_hash):raise Conflict()
                return {"source_id":existing["id"],"disposition":"duplicate"}
            visibility=value.get("visibility","private")
            project=_scope(ctx["tenant"],value["project"],visibility,ctx["actor"])
            session=_digest(json.dumps([ctx["tenant"],ctx["enrollment"],value["session"]]))
            turn=_digest(json.dumps([session,value["turn"]]))
            event={"project":project,"session":session,"turn":turn,"kind":value["kind"],
                "body":cleaned,"capture_id":ident,"tool_name":value.get("tool_name",""),
                "exit_code":value.get("exit_code"),
                "enterprise_source":True,
                "source_role":value.get("source_role","episode_evidence"),
                "event_fields":value.get("event_fields",[]),"response_shape":value.get("response_shape"),
                "occurred_at":value["occurred_at"],"speaker":value.get("speaker",""),
                "source_visibility":visibility,"quarantined":bool(value.get("excluded_reason"))}
            source_id=state.enqueue(event)
            if value["kind"]!="gap":state.process()
            with db:
                db.execute("""INSERT INTO enterprise_sources(id,tenant,owner,external_project,internal_project,
                    external_id,enrollment,payload_hash,visibility,raw_visibility,occurred_at,created,
                    occurred_precision,occurred_timezone)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_id,ctx["tenant"],ctx["actor"],value["project"],project,
                     value["external_id"],ctx["enrollment"],payload_hash,visibility,"private",
                     str(value["occurred_at"]),time.time(),value.get('precision','unknown'),
                     value.get('timezone')))
                self._audit(db,ctx,"ingest","accepted",source_id,1)
            return {"source_id":source_id,"disposition":"accepted"}

    def _readable_scopes(self, db, ctx, project=None):
        self._need(ctx,"read")
        clauses=["private"]
        if project is not None:
            projects=[project]
        else:
            projects=[r[0] for r in db.execute("SELECT id FROM enterprise_projects WHERE tenant=? AND active=1",(ctx["tenant"],))]
        result=[]
        for name in projects:
            if ctx.get('delegated_projects') is not None and name not in ctx['delegated_projects']:
                continue
            if self._project_member(db,ctx,name):
                result.extend((_scope(ctx["tenant"],name,"private",ctx["actor"]),
                               _scope(ctx["tenant"],name,"team",ctx["actor"])))
            else:
                result.append(_scope(ctx["tenant"],name,"private",ctx["actor"]))
        if ctx.get('delegated_projects') is None:
            result.append(_scope(ctx["tenant"],"*","organization",ctx["actor"]))
        return sorted(set(result))

    @staticmethod
    def _receiver(ctx, session):
        return _digest(json.dumps([ctx['tenant'],ctx['principal'],ctx['enrollment'],session],
            separators=(',',':')))

    def boundary(self, ctx, value):
        self._need(ctx,'read');validate_boundary(value)
        from agenthub.processing.context_delivery import initialize, record_postcompact_boundary, current_epoch
        event=dict(value['event'])
        event['session_id']=self._receiver(ctx,event['session_id'])
        with self.open() as state, state.db:
            initialize(state.db)
            applied=record_postcompact_boundary(state.db,event)
            epoch=current_epoch(state.db,event['session_id'])['epoch']
            self._audit(state.db,ctx,'context_boundary','applied' if applied else 'duplicate',None)
            return {'applied':applied,'epoch':epoch}

    def receipt(self, ctx, value):
        self._need(ctx,'read');validate_receipt(value)
        from agenthub.processing.context_delivery import current_epoch
        receiver=self._receiver(ctx,value['session'])
        with self.open() as state, state.db:
            db=state.db;epoch=str(current_epoch(db,receiver)['epoch'])
            for card in value['cards']:
                if card['id'].startswith('ta_'):
                    doc=self._temporal_allowed(db,ctx,card['id'])
                    revision=doc['revision_id'] if doc else None
                else:
                    doc=self._document_allowed(db,ctx,card['id'])
                    revision=doc['active_revision_id'] if doc else None
                if not doc or revision!=card['revision']:raise Denied()
            for card in value['cards']:
                ident=_digest(json.dumps([receiver,epoch,value.get('request_id',ctx['request_id']),
                    card['id'],card['revision']]))
                db.execute("INSERT INTO enterprise_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (ident,ctx['tenant'],ctx['principal'],ctx['enrollment'],receiver,epoch,
                     card['id'],card['revision'],'offered',value['serialized_chars'],time.time()))
            return {'accepted':len(value['cards']),'status':'offered','host_confirmed':False,
                    'observed_used':False,'epoch':epoch}

    def _document_allowed(self, db, ctx, document_id):
        doc=db.execute("""SELECT ed.*,kd.lifecycle,kd.active_revision_id FROM enterprise_documents ed
            JOIN knowledge_documents kd ON kd.document_id=ed.id WHERE ed.id=?""",(document_id,)).fetchone()
        if not doc or not doc["active"] or doc["tenant"]!=ctx["tenant"] or doc["lifecycle"]!="active":return None
        generation=db.execute('''SELECT 1 FROM knowledge_generation_documents gd
            JOIN knowledge_generations g ON g.generation_id=gd.generation_id
            WHERE gd.document_id=? AND g.status='active' LIMIT 1''',(document_id,)).fetchone()
        mapped=db.execute('SELECT 1 FROM knowledge_generation_documents WHERE document_id=? LIMIT 1',(document_id,)).fetchone()
        if mapped and not generation:
            from agenthub.draft_review import allowed_revision
            if not allowed_revision(self,ctx,document_id,doc['active_revision_id']):return None
        dependencies=db.execute("""SELECT s.* FROM enterprise_dependencies x
            JOIN enterprise_sources s ON s.id=x.source_id WHERE x.document_id=?""",(document_id,)).fetchall()
        if not dependencies:return None
        if not all(self._visible_source(db,ctx,s) for s in dependencies) and not self._release_allowed(db,ctx,document_id,dependencies):return None
        if doc["internal_project"] not in self._readable_scopes(db,ctx):return None
        return doc

    def refresh_documents(self, *, db=None, document_id=None):
        """Register pipeline outputs only when every source has an enterprise link."""
        if db is None:
            with self.open() as state, state.db:
                return self.refresh_documents(db=state.db, document_id=document_id)
        if document_id is not None and (not isinstance(document_id,str) or not 1<=len(document_id)<=256):
            raise ValueError('document_id_required')
        if getattr(db,'dialect',None)!='postgres':raise ValueError('PostgreSQL authority required')
        # Expand all canonical source paths in
        # one transaction. Missing/inactive/cross-scope provenance keeps
        # the entire document unregistered, rather than dropping links.
        target_clause='AND d.document_id=%s' if document_id is not None else ''
        from agenthub.draft_review import selected_documents
        review=selected_documents(self)
        review_clause=(' AND gd.document_id NOT IN ('+','.join('%s' for _ in review)+')') if review else ''
        row=db.execute(f"""WITH eligible AS MATERIALIZED (
            SELECT d.document_id,d.project,d.active_revision_id
            FROM knowledge_documents d WHERE d.lifecycle='active'
            AND d.active_revision_id IS NOT NULL
            {target_clause}
            AND EXISTS(SELECT 1 FROM knowledge_document_members m WHERE m.document_id=d.document_id)
            AND NOT EXISTS(SELECT 1 FROM knowledge_generation_documents gd
                JOIN knowledge_generations g ON g.generation_id=gd.generation_id
                WHERE gd.document_id=d.document_id AND g.status!='active'{review_clause})
        ), jobs AS MATERIALIZED (
            SELECT e.document_id,c.job_id FROM eligible e
            JOIN episode_candidates c ON c.document_id=e.document_id
        ), sources AS MATERIALIZED (
            SELECT e.document_id,s.id AS source_id FROM eligible e
            JOIN knowledge_document_members m ON m.document_id=e.document_id
            JOIN enterprise_sources s ON s.id=m.memory_id
            UNION
            SELECT e.document_id,o.source_id FROM eligible e
            JOIN knowledge_document_members m ON m.document_id=e.document_id
            JOIN observation_sources o ON o.memory_id=m.memory_id
            UNION
            SELECT e.document_id,s.source_memory_id FROM eligible e
            JOIN knowledge_support s ON s.revision_id=e.active_revision_id
            UNION
            SELECT j.document_id,raw.source_id FROM jobs j
            JOIN curation_episode_jobs c ON c.id=j.job_id
            CROSS JOIN LATERAL jsonb_array_elements_text(c.source_ids::jsonb) raw(source_id)
            UNION
            SELECT j.document_id,p.source_id FROM jobs j
            JOIN backend_processing_dependencies p ON p.episode_job=j.job_id
            UNION
            SELECT j.document_id,s.source_memory_id FROM jobs j
            JOIN enterprise_model_inputs i ON i.job_id=j.job_id
            JOIN knowledge_documents d ON d.document_id=i.related_document_id
            JOIN knowledge_support s ON s.revision_id=d.active_revision_id
            WHERE NOT EXISTS(SELECT 1 FROM backend_jobs b WHERE b.episode_job=j.job_id AND b.dependency_snapshot_complete=1)
            UNION
            SELECT j.document_id,x.source_id FROM jobs j
            JOIN enterprise_model_inputs i ON i.job_id=j.job_id
            JOIN enterprise_dependencies x ON x.document_id=i.related_document_id
            WHERE NOT EXISTS(SELECT 1 FROM backend_jobs b WHERE b.episode_job=j.job_id AND b.dependency_snapshot_complete=1)
        ), valid AS MATERIALIZED (
            SELECT e.document_id,e.project,e.active_revision_id,min(s.tenant) AS tenant
            FROM eligible e JOIN sources x ON x.document_id=e.document_id
            LEFT JOIN enterprise_sources s ON s.id=x.source_id
            GROUP BY e.document_id,e.project,e.active_revision_id
            HAVING count(*)=count(s.id) AND count(DISTINCT s.tenant)=1
            AND bool_and(s.active!=0 AND s.internal_project=e.project)
        ), registered AS (
            INSERT INTO enterprise_documents(id,tenant,internal_project,revision,created)
            SELECT document_id,tenant,project,active_revision_id,%s FROM valid
            ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,
            active=CASE WHEN enterprise_documents.blocked_reason='building' THEN 1 ELSE enterprise_documents.active END,
            blocked_reason=CASE WHEN enterprise_documents.blocked_reason='building' THEN '' ELSE enterprise_documents.blocked_reason END
            RETURNING id
        ), linked AS (
            INSERT INTO enterprise_dependencies(document_id,source_id)
            SELECT r.id,x.source_id FROM registered r JOIN sources x ON x.document_id=r.id
            ON CONFLICT DO NOTHING RETURNING document_id
        ) SELECT (SELECT count(*) FROM registered) AS registered,
            (SELECT count(*) FROM linked) AS linked""",
            (*([document_id] if document_id is not None else []),*(review or []),time.time())).fetchone()
        return row['registered']

    def process_once(self, internal_project, config, runner):
        """Advance the canonical AgentClient episode pipeline in one policy cell.

        The caller supplies the authorized/accounted runner. No provider call is
        initiated by profile creation or the HTTP listener.
        """
        from agenthub.processing.episode_pipeline import run_once
        if not isinstance(internal_project,str) or not internal_project.startswith('enterprise:'):
            raise ValueError('enterprise_curation_scope_required')
        if config.get('knowledge_backend',{}).get('mode')!='enterprise_local':
            raise ValueError('enterprise_config_required')
        path=self.home/'config.json'
        path.write_text(json.dumps(config,sort_keys=True));path.chmod(0o600)
        with self.open() as state:
            advanced=run_once(state,config,runner,project_scope=internal_project)
        self.refresh_documents()
        return advanced

    def accept_reviewed_note(self, ctx, source_id, title, lesson):
        """Explicit owner review for a source-backed note; never raw auto-indexing."""
        self._need(ctx,'correct')
        if (not isinstance(title,str) or not 1<=len(title)<=180 or
            not isinstance(lesson,str) or not 20<=len(lesson)<=1200 or
            SECRET.search(title+' '+lesson)):
            raise ValueError('invalid_reviewed_note')
        from agenthub.processing.knowledge import accept_local_candidate
        # A withdrawal or policy change must finish before a reviewed note can
        # inspect its source and commit new dependencies. This is the same
        # cross-process lock used by lifecycle and the final delivery check.
        with self.delivery_lock(), self.open() as state, state.db:
            db=state.db
            source=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(source_id,)).fetchone()
            if not source or source['tenant']!=ctx['tenant'] or source['owner']!=ctx['actor'] or not source['active']:
                raise Denied()
            memory=db.execute('SELECT session FROM memories WHERE id=? AND active=1',(source_id,)).fetchone()
            if not memory:raise Denied()
            result=accept_local_candidate(db,source_id,source['internal_project'],memory['session'],
                title,lesson,[source_id])
            self._audit(db,ctx,'reviewed_note','accepted',result['document_id'],source['policy_version'])
            # Registration shares the claim transaction. CloudStore also queues
            # its derivative index job here, so a crash after commit cannot
            # strand an accepted note without a durable pending job.
            self.refresh_documents(db=db, document_id=result['document_id'])
        self.refresh_documents()
        return result


    def detail(self, ctx, document_id, *, as_of=None, time_mode=None):
        self._need(ctx,"read")
        with self.open() as state:
            if document_id.startswith('ta_'):
                return self.temporal_detail(state,ctx,document_id,as_of=as_of,time_mode=time_mode)
            if as_of is not None or time_mode is not None:
                raise ValueError('temporal_detail_requires_assertion')
            db=state.db;doc=self._document_allowed(db,ctx,document_id)
            if not doc:raise Denied()
            row=db.execute("SELECT claim_json FROM knowledge_revisions WHERE revision_id=?",
                (doc["active_revision_id"],)).fetchone()
            if not row:raise Denied()
            value={"id":document_id,"revision":doc["active_revision_id"],"claim":json.loads(row[0])}
            if len(json.dumps(value,ensure_ascii=True))>4000:raise ValueError("detail_too_large")
            return value

    def document_evidence(self, ctx, document_id, *, revision, offset=0):
        from agenthub.source_evidence import document_evidence
        return document_evidence(self, ctx, document_id, revision=revision, offset=offset)

    def timeline(self, ctx, document_id):
        self._need(ctx,"read")
        from agenthub.processing.knowledge import active_generation_id
        from agenthub.processing.episode_pipeline import episode_links_for_document
        with self.open() as state:
            db=state.db;doc=self._document_allowed(db,ctx,document_id)
            if not doc:raise Denied()
            generation=active_generation_id(db)
            if not generation:return {"id":document_id,"episodes":[],"coverage_gaps":["generation_unavailable"]}
            linked=episode_links_for_document(db,generation,doc["internal_project"],document_id,limit=3)
            # The summary is derived context from the same policy cell. Raw
            # source inspection is a separate action and is never in this view.
            episodes=[]
            for entry in linked.get("episodes",[]):
                item={"summary":entry.get("summary",""),"summary_revision":entry.get("summary_revision"),
                    "source_range":entry.get("source_range"),"coverage_gaps":entry.get("coverage_gaps",[])}
                if len(json.dumps(item,ensure_ascii=True))<1200:episodes.append(item)
            result={"id":document_id,"revision":doc["active_revision_id"],
                    "episodes":episodes,"coverage_gaps":linked.get("coverage_gaps",[]),
                    "has_more":linked.get("has_more",False)}
            while episodes and len(json.dumps(result,ensure_ascii=True))>4000:episodes.pop()
            if not self._document_allowed(db,ctx,document_id):raise Denied()
            return result

    def source(self, ctx, source_id):
        self._need(ctx,"source_read")
        with self.open() as state:
            db=state.db;row=db.execute("SELECT * FROM enterprise_sources WHERE id=?",(source_id,)).fetchone()
            if not self._visible_source(db,ctx,row,raw=True):raise Denied()
            memory=db.execute("SELECT body,kind,created FROM memories WHERE id=? AND active=1",(source_id,)).fetchone()
            if not memory:raise Denied()
            return {"id":source_id,"body":memory["body"],"kind":memory["kind"],
                    "occurred_at":row["occurred_at"],"precision":row['occurred_precision'],
                    "timezone":row['occurred_timezone'],"visibility":row["visibility"],
                    "source_version":row["source_version"]}

    def _invalidate_processing_source(self, db, source_id, reason):
        """Discard private recovery material when any influencing source changes.

        Attempts and usage receipts remain for accounting. Provider outputs,
        checkpoints and validation progress cannot be reused under a new policy.
        The caller holds the tenant policy/delivery lock in this transaction.
        """
        if table_exists(db,'observer_jobs'):
            legacy=[r['id'] for r in db.execute('SELECT id,source_ids FROM observer_jobs')
                if source_id in json.loads(r['source_ids'] or '[]')]
            for job in legacy:
                db.execute('DELETE FROM observer_progress WHERE job_id=?',(job,))
                db.execute("UPDATE observer_jobs SET status='held',error=? WHERE id=?",(reason,job))
        jobs={r['id'] for r in db.execute('SELECT id,source_ids FROM curation_episode_jobs')
            if source_id in json.loads(r['source_ids'] or '[]')}
        if table_exists(db,'backend_processing_dependencies'):
            jobs.update(r[0] for r in db.execute(
                'SELECT episode_job FROM backend_processing_dependencies WHERE source_id=?',(source_id,)))
        observers=[]
        if table_exists(db,'backend_observer_dependencies'):
            observers=[r[0] for r in db.execute(
                'SELECT observer_id FROM backend_observer_dependencies WHERE source_id=?',(source_id,))]
            if table_exists(db,'backend_jobs'):
                for observer in observers:
                    jobs.update(r[0] for r in db.execute(
                        'SELECT episode_job FROM backend_jobs WHERE observer_id=? AND dependency_snapshot_complete=0',(observer,)))
        for job in jobs:
            db.execute("UPDATE curation_episode_jobs SET progress='{}',error=? WHERE id=?",(reason,job))
            db.execute("UPDATE episode_candidates SET candidate_json='{}',resolution_json='{}',status='withdrawn' WHERE job_id=?",(job,))
            db.execute("UPDATE episode_candidate_history SET candidate_json='{}',resolution_json='{}',status='withdrawn' WHERE job_id=?",(job,))
            if table_exists(db,'backend_provider_returns'):
                db.execute('DELETE FROM backend_provider_returns WHERE job_id IN (SELECT id FROM backend_jobs WHERE episode_job=?)',(job,))
            if table_exists(db,'backend_jobs'):
                db.execute("UPDATE backend_jobs SET status='held',pending_result=NULL,last_error=? WHERE episode_job=?",(reason,job))
        for observer in observers:
            db.execute("""UPDATE backend_observers SET status='invalidated',provider_session=NULL,
                pending_session=NULL,checkpoint='{}',context_chars=0,observer_epoch=observer_epoch+1 WHERE id=?""",(observer,))

    def _purge_deleted_source(self, db, source_id):
        """Same deletion semantics on the live authority and a restored copy."""
        self._invalidate_processing_source(db,source_id,'source_deleted')
        docs=[r[0] for r in db.execute('SELECT document_id FROM enterprise_dependencies WHERE source_id=?',(source_id,))]
        for document in docs:
            db.execute('DELETE FROM knowledge_temporal_relations WHERE new_assertion_id IN (SELECT assertion_id FROM knowledge_temporal_assertions WHERE document_id=?) OR old_assertion_id IN (SELECT assertion_id FROM knowledge_temporal_assertions WHERE document_id=?)',(document,document))
            db.execute('DELETE FROM knowledge_temporal_evidence WHERE assertion_id IN (SELECT assertion_id FROM knowledge_temporal_assertions WHERE document_id=?)',(document,))
            db.execute('DELETE FROM knowledge_temporal_assertions WHERE document_id=?',(document,))
            db.execute('DELETE FROM knowledge_support WHERE revision_id IN (SELECT revision_id FROM knowledge_revisions WHERE document_id=?)',(document,))
            db.execute('DELETE FROM knowledge_relations WHERE document_id=? OR related_document_id=?',(document,document))
            db.execute("UPDATE knowledge_revisions SET claim_json='{}',identity_json='{}' WHERE document_id=?",(document,))
            db.execute("UPDATE knowledge_documents SET lifecycle='deleted',active_revision_id=NULL WHERE document_id=?",(document,))
            db.execute("UPDATE memories SET body='' WHERE id IN (SELECT origin_memory_id FROM knowledge_documents WHERE document_id=?)",(document,))
            db.execute("UPDATE enterprise_documents SET active=0,blocked_reason='delete' WHERE id=?",(document,))
            db.execute('DELETE FROM knowledge_fts WHERE document_id=?',(document,))
            db.execute('DELETE FROM knowledge_embeddings WHERE document_id=?',(document,))
            db.execute("UPDATE backend_native_artifacts SET source_url='' WHERE document_id=?",(document,))
            db.execute("UPDATE backend_native_spans SET heading='' WHERE document_id=?",(document,))
        db.execute("UPDATE memories SET active=0,body='' WHERE id=?",(source_id,))
        db.execute('DELETE FROM memory_fts WHERE id=?',(source_id,))
        db.execute("UPDATE events SET payload='{}' WHERE id=?",(source_id,))
        db.execute('DELETE FROM source_event_metadata WHERE source_id=?',(source_id,))
        db.execute("UPDATE backend_source_revisions SET payload='{}',disposition='excluded' WHERE source_id=?",(source_id,))
        jobs={r['id'] for r in db.execute('SELECT id,source_ids FROM curation_episode_jobs') if source_id in json.loads(r['source_ids'])}
        if table_exists(db,'backend_processing_dependencies'):
            jobs.update(r[0] for r in db.execute('SELECT episode_job FROM backend_processing_dependencies WHERE source_id=?',(source_id,)))
        for job in jobs:
            db.execute("UPDATE curation_episode_jobs SET status='withdrawn',progress='{}',error='source_deleted' WHERE id=?",(job,))
            db.execute("UPDATE episode_candidates SET candidate_json='{}',resolution_json='{}',status='withdrawn' WHERE job_id=?",(job,))
            db.execute("UPDATE episode_candidate_history SET candidate_json='{}',resolution_json='{}',status='withdrawn' WHERE job_id=?",(job,))
            if table_exists(db,'backend_jobs'):
                db.execute("UPDATE backend_jobs SET status='held',pending_result=NULL,last_error='source_deleted' WHERE episode_job=?",(job,))

    def lifecycle(self, ctx, value):
        validate_lifecycle(value)
        if redact(value)[0] != value:raise ValueError("secret_source_rejected")
        operation=value["operation"]
        self._need(ctx,{"correct":"correct","withdraw":"withdraw","delete":"withdraw","policy":"policy"}[operation])
        with self.delivery_lock(), self.open() as state:
            db=state.db;key=_digest(json.dumps([ctx["tenant"],ctx["enrollment"],value["idempotency_key"]]))
            old=db.execute("SELECT result FROM enterprise_lifecycle WHERE key=?",(key,)).fetchone()
            if old:return json.loads(old[0])
            source=db.execute("SELECT * FROM enterprise_sources WHERE id=?",(value["target_id"],)).fetchone()
            if not source or source["tenant"]!=ctx["tenant"] or source["owner"]!=ctx["actor"]:raise Denied()
            if str(source["source_version"])!=value["expected_revision"]:raise Conflict()
            with db:
                if operation=="correct":
                    replacement=value["replacement"]
                    event=replacement["source"]
                    if (event["project"]!=source["external_project"] or
                        event.get("visibility","private")!=source["visibility"]):
                        raise Denied()
                    corrected_id=_digest(json.dumps([ctx["tenant"],ctx["enrollment"],event["external_id"],
                        'correction']))
                    if corrected_id==source["id"] or db.execute("SELECT 1 FROM enterprise_sources WHERE tenant=? AND enrollment=? AND external_id=?",
                        (ctx["tenant"],ctx["enrollment"],event["external_id"])).fetchone():
                        raise Conflict()
                    original=db.execute("SELECT session,turn FROM memories WHERE id=?",(source["id"],)).fetchone()
                    if not original:raise Conflict()
                    now=time.time()
                    cleaned,_=clean_private(event["body"])
                    db.execute("""INSERT INTO events(id,session,turn,kind,project,payload,created,processed)
                        VALUES(?,?,?,?,?,?,?,1)""",
                        (corrected_id,original["session"],original["turn"],event["kind"],
                         source["internal_project"],json.dumps(event,sort_keys=True),now))
                    db.execute("""INSERT INTO memories(id,session,project,body,kind,created,active,turn)
                        VALUES(?,?,?,?,?,?,1,?)""",
                        (corrected_id,original["session"],source["internal_project"],cleaned,
                         event["kind"],now,original["turn"]))
                    db.execute("INSERT INTO source_event_metadata VALUES(?,?,?,?,?,?,?)",
                        (corrected_id,event.get('tool_name',''),'episode_evidence',
                         event.get('capture_id'),json.dumps(event.get('event_fields',[])),
                         json.dumps(event.get('response_shape')),now))
                    db.execute("""INSERT INTO enterprise_sources(id,tenant,owner,external_project,internal_project,
                        external_id,enrollment,payload_hash,visibility,raw_visibility,occurred_at,created,
                        occurred_precision,occurred_timezone)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (corrected_id,ctx['tenant'],ctx['actor'],source['external_project'],
                         source['internal_project'],event['external_id'],ctx['enrollment'],
                         _digest(json.dumps(event,sort_keys=True)),source['visibility'],'private',
                         str(event['occurred_at']),now,event.get('precision','unknown'),
                         event.get('timezone')))
                    db.execute("INSERT INTO historical_sources VALUES(?,?) ON CONFLICT DO NOTHING",
                        (source['id'],corrected_id))
                    dependent=db.execute("SELECT document_id FROM enterprise_dependencies WHERE source_id=?",
                        (source['id'],)).fetchall()
                    if len(dependent)>1:raise Conflict()
                    if dependent:
                        from agenthub.processing.knowledge import apply_resolved_observation
                        target=dependent[0][0]
                        observation={'title':replacement['title'],'problem':'',
                            'lesson':replacement['lesson'],'applicability':'',
                            'applicability_constraints':{'versions':[],'platforms':[],
                                'date_ranges':[],'units':[],'project_scope':[]},
                            'knowledge_type':'observation','domain':'unknown',
                            'subjects':[],'tags':[],'outcome':'unknown',
                            'evidence_status':'user_reported',
                            'evidence':[{'source_id':corrected_id}]}
                        revised=apply_resolved_observation(db,corrected_id,
                            source['internal_project'],original['session'],observation,
                            'CORRECT',target,now)
                        db.execute("DELETE FROM enterprise_dependencies WHERE document_id=?",(target,))
                        db.execute("INSERT INTO enterprise_dependencies VALUES(?,?)",(target,corrected_id))
                        db.execute("UPDATE enterprise_documents SET revision=?,active=1,policy_version=policy_version+1 WHERE id=?",
                            (revised['revision_id'],target))
                    db.execute("UPDATE enterprise_sources SET active=0,source_version=source_version+1 WHERE id=?",
                        (source['id'],))
                    db.execute("UPDATE memories SET active=0 WHERE id=?",(source['id'],))
                    db.execute("DELETE FROM memory_fts WHERE id=?",(source['id'],))
                    db.execute("INSERT INTO enterprise_deletion_journal(tenant,source_id,operation,created) VALUES(?,?,?,?)",
                        (ctx['tenant'],source['id'],operation,now))
                    docs=dependent
                elif operation=="policy":
                    visibility=value.get("visibility")
                    # Policy widening requires a reviewed release flow; local
                    # ordinary mutation is deliberately narrowing-only.
                    order={"private":0,"team":1,"organization":2}
                    if visibility is None or order[visibility]>order[source["visibility"]]:raise Denied()
                    new_scope=_scope(ctx['tenant'],source['external_project'],visibility,ctx['actor'])
                    db.execute("UPDATE enterprise_sources SET visibility=?,internal_project=?,policy_version=policy_version+1,source_version=source_version+1 WHERE id=?",
                        (visibility,new_scope,source["id"]))
                    db.execute("UPDATE memories SET project=? WHERE id=?",(new_scope,source['id']))
                    db.execute("UPDATE events SET project=? WHERE id=?",(new_scope,source['id']))
                else:
                    db.execute("UPDATE enterprise_sources SET active=0,source_version=source_version+1 WHERE id=?",(source["id"],))
                    db.execute("UPDATE memories SET active=0 WHERE id=?",(source["id"],))
                    db.execute("DELETE FROM memory_fts WHERE id=?",(source["id"],))
                    db.execute("INSERT INTO enterprise_deletion_journal(tenant,source_id,operation,created) VALUES(?,?,?,?)",
                        (ctx["tenant"],source["id"],operation,time.time()))
                docs=db.execute("SELECT document_id FROM enterprise_dependencies WHERE source_id=?",(source["id"],)).fetchall() if operation!='correct' else docs
                for d in docs:
                    if operation!='correct':
                        db.execute("UPDATE enterprise_documents SET active=0,blocked_reason=?,policy_version=policy_version+1 WHERE id=?",(operation,d[0]))
                        db.execute("DELETE FROM knowledge_fts WHERE document_id=?",(d[0],))
                        db.execute("DELETE FROM knowledge_embeddings WHERE document_id=?",(d[0],))
                        if operation=='delete':
                            revisions=[r[0] for r in db.execute(
                                'SELECT revision_id FROM knowledge_revisions WHERE document_id=?',(d[0],))]
                            for revision in revisions:
                                db.execute('DELETE FROM knowledge_temporal_evidence WHERE assertion_id IN (SELECT assertion_id FROM knowledge_temporal_assertions WHERE revision_id=?)',(revision,))
                                db.execute('DELETE FROM knowledge_temporal_assertions WHERE revision_id=?',(revision,))
                                db.execute('DELETE FROM knowledge_support WHERE revision_id=?',(revision,))
                            db.execute("UPDATE knowledge_revisions SET claim_json='{}',identity_json='{}' WHERE document_id=?",(d[0],))
                            db.execute("UPDATE knowledge_documents SET lifecycle='deleted',active_revision_id=NULL WHERE document_id=?",(d[0],))
                            db.execute("UPDATE memories SET body='' WHERE id IN (SELECT origin_memory_id FROM knowledge_documents WHERE document_id=?)",(d[0],))
                if operation=='delete':
                    self._purge_deleted_source(db,source['id'])
                if operation!='delete':self._invalidate_processing_source(db,source['id'],'source_'+operation)
                result={"target_id":source["id"],"operation":operation,"source_version":source["source_version"]+1,
                        "dependent_documents_blocked":len(docs)}
                if operation=='correct':result['replacement_source_id']=corrected_id
                db.execute("INSERT INTO enterprise_lifecycle VALUES(?,?,?,?,?,?,?)",
                    (key,ctx["tenant"],ctx["actor"],source["id"],operation,json.dumps(result),time.time()))
                self._audit(db,ctx,operation,"applied",source["id"],source["policy_version"]+1)
            return result

    def status(self, ctx):
        with self.open() as state:
            db=state.db
            return {"service":"AgentHub enterprise-local","version":VERSION,
                "tenant":ctx["tenant"],"principal":ctx["principal"],"acting_for":ctx["acting_for"],
                "enrollment":ctx["enrollment"],"actions":sorted(ctx["actions"]),
                "schema":db.execute("SELECT max(version) FROM enterprise_schema").fetchone()[0]}

    def explain(self, ctx, target_id):
        """Report eligible readers and policy dependencies without claim/source text."""
        if 'settings' not in ctx['actions'] and 'read' not in ctx['actions']:
            raise Denied()
        with self.open() as state:
            db=state.db
            source=db.execute('SELECT * FROM enterprise_sources WHERE id=? AND tenant=?',
                (target_id,ctx['tenant'])).fetchone()
            doc=db.execute('SELECT * FROM enterprise_documents WHERE id=? AND tenant=?',
                (target_id,ctx['tenant'])).fetchone()
            if not source and not doc:raise Denied()
            if 'settings' not in ctx['actions']:
                if source and source['owner']!=ctx['actor']:raise Denied()
                if doc and not self._document_allowed(db,ctx,target_id):raise Denied()
            eligible=[]
            for row in db.execute('SELECT id FROM enterprise_principals WHERE tenant=? AND active=1',
                                  (ctx['tenant'],)):
                probe={'tenant':ctx['tenant'],'principal':row['id'],'actor':row['id'],
                       'actions':{'read'},'delegated_projects':None}
                if (source and self._visible_source(db,probe,source) or
                        doc and self._document_allowed(db,probe,target_id)):
                    eligible.append(row['id'])
            if source:
                return {'kind':'source','id':target_id,'active':bool(source['active']),
                    'visibility':source['visibility'],'policy_version':source['policy_version'],
                    'eligible_principals':eligible,'raw_readers':[source['owner']] if source['active'] else []}
            dependencies=db.execute('SELECT count(*) FROM enterprise_dependencies WHERE document_id=?',
                (target_id,)).fetchone()[0]
            return {'kind':'document','id':target_id,'active':bool(doc['active']),
                'policy_version':doc['policy_version'],'dependency_count':dependencies,
                'eligible_principals':eligible}

    def retention(self, ctx, project, before, *, apply=False, limit=100):
        """Bounded owner-operated expiry; dry-run by default."""
        self._need(ctx,'withdraw')
        if (not isinstance(project,str) or not project or type(before) not in (int,float)
                or before<=0 or before>time.time() or type(limit) is not int or not 1<=limit<=100):
            raise ValueError('invalid_retention_request')
        with self.open() as state:
            rows=state.db.execute('''SELECT id,source_version FROM enterprise_sources
                WHERE tenant=? AND owner=? AND external_project=? AND active=1 AND created<?
                ORDER BY created,id LIMIT ?''',(ctx['tenant'],ctx['actor'],project,before,limit)).fetchall()
            candidates=[(row['id'],row['source_version']) for row in rows]
        if not apply:return {'project':project,'before':before,'candidate_count':len(candidates),
                             'applied':0,'dry_run':True,'truncated':len(candidates)==limit}
        completed=0
        for ident,revision in candidates:
            try:
                self.lifecycle(ctx,{'version':VERSION,'target_id':ident,
                    'expected_revision':str(revision),'operation':'delete',
                    'reason':'explicit retention expiry',
                    'idempotency_key':'retention-'+_digest(ident+':'+str(revision))})
                completed+=1
            except Conflict:
                continue
        return {'project':project,'before':before,'candidate_count':len(candidates),
                'applied':completed,'dry_run':False,'truncated':len(candidates)==limit}

    def processing_status(self, ctx):
        self._need(ctx,'audit')
        with self.open() as state:
            db=state.db;scopes=self._readable_scopes(db,ctx)
            marks=','.join('?' for _ in scopes)
            sources=db.execute('''SELECT count(*) FROM enterprise_sources
                WHERE tenant=? AND active=1 AND internal_project IN ('''+marks+')',
                (ctx['tenant'],*scopes)).fetchone()[0]
            jobs=[dict(row) for row in db.execute('''SELECT status,count(*) count FROM curation_episode_jobs
                WHERE project IN ('''+marks+') GROUP BY status ORDER BY status',scopes)]
            return {'tenant':ctx['tenant'],'principal':ctx['principal'],
                'visible_active_sources':sources,'jobs':jobs,
                'scope_count':len(scopes)}

    def audit(self, ctx, limit=30):
        self._need(ctx,"audit")
        with self.open() as state:
            return [dict(r) for r in state.db.execute("SELECT * FROM enterprise_audit WHERE tenant=? ORDER BY id DESC LIMIT ?",
                (ctx["tenant"],min(100,max(1,int(limit)))))]
