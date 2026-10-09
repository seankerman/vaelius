"""Shared backend model-call accounting; no standalone observer worker."""
import json
import time
from agenthub.processing.harness import HarnessError
def call_model(state, cfg, purpose, instruction, payload, schema, runner, context=None):
    from agenthub.processing.accounting import record_start, record_finish
    current=json.loads((state.home/'config.json').read_text())
    if current.get('paused') or not current.get('observer',{}).get('enabled'):
        raise HarnessError('observer_paused')
    model = cfg["observer"].get("model", "gpt-6-luna")
    with state.db:
        sql="INSERT INTO model_calls(created,purpose,model,status) VALUES(?,?,?,'started')"
        call_id=state.db.execute(sql+" RETURNING id",(time.time(),purpose,model)).fetchone()[0]
        attribution=record_start(state.db,'model_calls',call_id,context)
    started=time.monotonic()
    try:
        result, usage = runner(state.home, dict(cfg,_purpose=purpose,_call_context=attribution), instruction, payload, schema)
    except BaseException:
        with state.db:
            state.db.execute("UPDATE model_calls SET status='failed' WHERE id=?", (call_id,))
            record_finish(state.db,'model_calls',call_id,time.monotonic()-started)
        raise
    with state.db:
        state.db.execute("UPDATE model_calls SET status='done',usage=? WHERE id=?", (json.dumps(usage),call_id))
        record_finish(state.db,'model_calls',call_id,time.monotonic()-started)
    return result


def initialize(db):
    db.require_schema()
