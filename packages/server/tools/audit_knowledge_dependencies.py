"""Read-only operator audit; aggregate counts only, never source or claim text."""
import argparse
import json
from pathlib import Path
from agenthub.cloud_local import runtime


def audit(store):
    with store.open() as state:
        db=state.db
        # One consistent snapshot; this is an explicit operator audit, not search.
        db.commit() # End schema verification before selecting the audit snapshot.
        db.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        total=db.execute("SELECT count(*) n FROM enterprise_documents WHERE active=1").fetchone()['n']
        impact=db.execute('''WITH impacts AS (
            SELECT x.source_id,count(DISTINCT x.document_id) n
            FROM enterprise_dependencies x JOIN enterprise_documents d ON d.id=x.document_id
            WHERE d.active=1 GROUP BY x.source_id)
            SELECT count(*) sources,coalesce(max(n),0) maximum_documents,
            coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY n),0) median_documents,
            coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY n),0) p95_documents
            FROM impacts''').fetchone()
        support=db.execute('''WITH cited AS MATERIALIZED (
            SELECT DISTINCT k.document_id,s.source_memory_id source_id
            FROM knowledge_documents k JOIN knowledge_support s ON k.active_revision_id=s.revision_id
        ), links AS (
            SELECT x.document_id,x.source_id,c.source_id IS NOT NULL cited
            FROM enterprise_dependencies x JOIN enterprise_documents d ON d.id=x.document_id
            LEFT JOIN cited c ON c.document_id=x.document_id AND c.source_id=x.source_id
            WHERE d.active=1)
            SELECT count(*) total_dependencies,count(*) FILTER(WHERE cited) current_citation_dependencies,
            count(*) FILTER(WHERE NOT cited) other_processing_or_history_dependencies FROM links''').fetchone()
        duplicates=db.execute('''WITH groups AS (
            SELECT encode(sha256(convert_to(lower(regexp_replace(r.claim_json::jsonb->>'lesson','\\s+',' ','g')),'UTF8')),'hex') identity,
            count(*) n,count(DISTINCT d.project) scopes
            FROM knowledge_documents d JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
            WHERE d.lifecycle='active' AND coalesce(r.claim_json::jsonb->>'lesson','')!=''
            GROUP BY 1 HAVING count(DISTINCT d.project)>1)
            SELECT count(*) exact_text_groups,coalesce(sum(n),0) documents FROM groups''').fetchone()
        decisions=db.execute('''SELECT resolution_json::jsonb->>'operation' operation,count(*) n
            FROM episode_candidates GROUP BY 1 ORDER BY 1''').fetchall()
        return {'active_registered_documents':total,'withdrawal_dependency_impact':dict(impact),
            'max_fraction':float(impact['maximum_documents'])/total if total else 0,
            'dependency_types':dict(support),'cross_scope_exact_text':dict(duplicates),
            'resolution_operations':[dict(r) for r in decisions],
            'limits':['Potential invalidation counts, not simulated deletion or measured recovery.',
                'Exact text duplicates only; separate permissions may legitimately require copies.',
                'Processing/history dependencies are not necessarily unnecessary; privacy remains enforced.',
                'Operation counts do not measure semantic correctness or false-supersede rate.']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',required=True);p.add_argument('--tenant',default='acme');p.add_argument('--output',required=True)
    a=p.parse_args();out=Path(a.output)
    if out.exists():p.error('receipt already exists')
    _,registry=runtime(a.profile)
    result=audit(registry.resolve(a.tenant));out.write_text(json.dumps(result,indent=2,default=float)+'\n')
    print(json.dumps(result,default=float))

if __name__=='__main__':main()
