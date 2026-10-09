import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agenthub.processing.accounting import report,job_context,record_start
from agenthub.processing.harness import run_structured
from agenthub.processing.observer import call_model,HarnessError
from vaelius_test_support.fixtures.state import State
from agenthub.processing.usage import Ledger


class AccountingTests(unittest.TestCase):
    def test_attempts_link_local_and_installation_and_keep_failures_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'client';home.mkdir()
            cfg={'accounting_home':tmp,'observer':{'enabled':True,'max_calls_per_day':20}}
            (home/'config.json').write_text(json.dumps(cfg));state=State(home)
            attribution={'job_kind':'observation','job_id':'job-one','project':'p','session':'s','source_sessions':['s']}
            with patch('agenthub.processing.harness._invoke',return_value=({}, {'input_tokens':10,'output_tokens':3})):
                call_model(state,cfg,'observe','PRIVATE_PROMPT',{'private':'SECRET_PAYLOAD'}, {}, run_structured,attribution)
            with patch('agenthub.processing.harness._invoke',side_effect=HarnessError('harness_timeout')):
                with self.assertRaises(HarnessError):
                    call_model(state,cfg,'memory_review','private',{}, {}, run_structured,attribution)
            # A retry is a new attempt associated with the same durable job.
            with patch('agenthub.processing.harness._invoke',return_value=({}, {'input_tokens':7,'output_tokens':2})):
                call_model(state,cfg,'memory_review','private',{}, {}, run_structured,attribution)
            ledger=Ledger(cfg)
            local_ids=[r[0] for r in state.db.execute('SELECT attempt_id FROM model_call_details ORDER BY call_id')]
            global_ids=[r[0] for r in ledger.db.execute('SELECT attempt_id FROM call_details ORDER BY call_id')]
            self.assertEqual(local_ids,global_ids);self.assertEqual(len(set(local_ids)),3)
            result=report(state.db)
            self.assertEqual(result['sessions'][0]['calls'],3)
            self.assertEqual(result['jobs'][0]['statuses'],{'done':2,'failed':1})
            self.assertEqual(result['jobs'][0]['reported_tokens']['input_tokens'],17)
            self.assertEqual(result['jobs'][0]['calls_without_usage'],1)
            self.assertEqual(result['jobs'][0]['timed_calls'],3)
            self.assertEqual(report(ledger.db,'calls')['total']['calls'],3)
            self.assertNotIn('SECRET_PAYLOAD',json.dumps(result));self.assertNotIn('PRIVATE_PROMPT',json.dumps(result))
            self.assertEqual(ledger.summary()['rolling_calls'],3)
            ledger.close();state.close()

    def test_legacy_and_multi_session_work_is_not_guessed_or_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=State(tmp)
            with state.db:
                state.db.execute("INSERT INTO model_calls(created,purpose,model,status,usage) VALUES(0,'observe','fake','done',?)",(json.dumps({'input_tokens':12}),))
            result=report(state.db)
            self.assertEqual(result['legacy_unattributed_calls'],1)
            self.assertEqual(result['sessions'],[])
            ctx=job_context('candidate',{'id':'candidate','project':'p'},[
                {'session':'one','project':'p'},{'session':'two','project':'p'}])
            self.assertIsNone(ctx['session']);self.assertEqual(ctx['source_sessions'],['one','two'])
            with state.db:
                call=state.db.execute("INSERT INTO model_calls(created,purpose,model,status) VALUES(1,'candidate_review','fake','started') RETURNING id").fetchone()[0]
                record_start(state.db,'model_calls',call,ctx)
            result=report(state.db)
            self.assertEqual(result['total']['calls'],2)
            self.assertEqual(result['without_single_session']['calls'],2)
            self.assertEqual(result['sessions'],[])
            self.assertEqual(result['jobs'][0]['calls'],1)
            self.assertEqual(job_context('candidate',{'id':'c','project':'p'},[])['session'],None)
            self.assertEqual(job_context('candidate',{'id':'c','project':'p'},[{'session':'one','project':'p'}])['session'],'one')
            state.close()

    def test_read_only_legacy_schema_and_partial_token_coverage(self):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE calls(id INTEGER,created REAL,purpose TEXT,status TEXT,usage TEXT)')
        db.execute("INSERT INTO calls VALUES(1,0,'observe','done',?)",(json.dumps({'input_tokens':False,'output_tokens':-1}),))
        before=list(db.iterdump());result=report(db,'calls')
        self.assertEqual(before,list(db.iterdump()));self.assertEqual(result['total']['calls_without_usage'],1)
        self.assertEqual(result['total']['reported_tokens'],{})
        db.close()

    def test_project_and_exact_session_filters_limit_accounting_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=State(tmp)
            with state.db:
                for project,session,tokens in (
                    ('allowed-project','selected-session',5),
                    ('allowed-project','unselected-session',11),
                    ('other-project','other-session',17),
                ):
                    ident=state.db.execute(
                        "INSERT INTO model_calls(created,purpose,model,status,usage) VALUES(?,?,?,?,?) RETURNING id",
                        (1,'observe','fake','done',json.dumps({'input_tokens':tokens})),
                    ).fetchone()[0]
                    record_start(state.db,'model_calls',ident,{
                        'job_kind':'observation','job_id':session,'project':project,
                        'session':session,'source_sessions':[session],
                    })
            exact=report(state.db,since=0,project='allowed-project',sessions=['selected-session'])
            self.assertEqual(exact['total']['calls'],1)
            self.assertEqual(exact['total']['reported_tokens']['input_tokens'],5)
            self.assertEqual([row['session'] for row in exact['sessions']],['selected-session'])
            project_only=report(state.db,since=0,project='allowed-project')
            self.assertEqual(project_only['total']['calls'],2)
            self.assertEqual({row['session'] for row in project_only['sessions']},
                             {'selected-session','unselected-session'})
            self.assertEqual(report(state.db,project='allowed-project',sessions=[])['total']['calls'],0)
            state.close()


if __name__=='__main__':unittest.main()
