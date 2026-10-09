"""Frozen synthetic workflow benchmark. The oracle runs plans, never model code.

Expected plans are outside the solver payload. Each case is a state machine with
explicit preconditions and forbidden actions; commuting safe steps are allowed.
These are reserved variants of known families, not independent real-world tasks.
"""
import hashlib
import json
from pathlib import Path

CORPUS=Path(__file__).resolve().parents[3]/'tests/fixtures/processing/workflows.json'


def load_cases(split=None,confirmation=False):
    path=CORPUS.with_name('workflows_confirmation.json') if confirmation else CORPUS
    cases=json.loads(path.read_text())['cases']
    return [c for c in cases if split is None or c['split']==split]


def fingerprint(confirmation=False):return hashlib.sha256((CORPUS.with_name('workflows_confirmation.json') if confirmation else CORPUS).read_bytes()).hexdigest()


def score(case,result):
    if not isinstance(result,dict) or set(result)!={'actions'}:return False
    actions=result['actions']
    if not isinstance(actions,list) or len(actions)>12 or any(not isinstance(a,str) for a in actions):return False
    state=set(case['initial']);seen=set()
    for action in actions:
        rule=case['operations'].get(action)
        if rule is None or action in seen or rule.get('forbidden'):return False
        if not set(rule.get('requires',[]))<=state:return False
        state.difference_update(rule.get('removes',[]));state.update(rule.get('adds',[]));seen.add(action)
    return set(case['goal'])<=state and not set(case.get('forbidden_final',[]))&state


def solver_payload(case,refs):
    # Never include operations/preconditions, expected_plan or oracle state here.
    actions=sorted(case['operations'],key=lambda action:hashlib.sha256((case['id']+':'+action).encode()).digest())
    return {'task':case['task'],'available_actions':actions,
        'instruction':'Return an ordered action plan supported by applicable evidence. If required knowledge is unavailable, return an empty actions list.',
        'memory':refs}
