"""Fixed-label, source-free timings local to one search request.

Spans are inclusive and may nest; do not sum them to obtain request latency.
No SQL, caller identifiers, query text, claims, tokens or exceptions are recorded.
The mutable trace follows AnyIO's copied context into the request's worker thread.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import time
import uuid

_CURRENT=ContextVar('agenthub_search_timing',default=None)
_STAGES=frozenset({'authenticate','ready','search','candidates','lexical_sql',
    'embedding','vector_sql','rerank','selection','delivery_lock_wait','delivery_check','meter'})


class Trace:
    def __init__(self):
        self.request_id=uuid.uuid4().hex
        self.started=time.perf_counter()
        self.stages={}

    def record(self,status):
        return {'event':'search_request_timing','request_id':self.request_id,
            'status':int(status),'elapsed_ms':round((time.perf_counter()-self.started)*1000,3),
            'stages_ms':{key:round(value*1000,3) for key,value in sorted(self.stages.items())}}


@contextmanager
def capture():
    trace=Trace();token=_CURRENT.set(trace)
    try:yield trace
    finally:_CURRENT.reset(token)


@contextmanager
def span(name):
    if name not in _STAGES:raise ValueError('unknown_timing_stage')
    trace=_CURRENT.get()
    if trace is None:
        yield
        return
    started=time.perf_counter()
    try:yield
    finally:trace.stages[name]=trace.stages.get(name,0)+(time.perf_counter()-started)


def timed(name):
    if name not in _STAGES:raise ValueError('unknown_timing_stage')
    def decorate(fn):
        @wraps(fn)
        def run(*args,**kwargs):
            with span(name):return fn(*args,**kwargs)
        return run
    return decorate


def emit(record):
    # Uvicorn already owns the service's configured operator log handler.
    logging.getLogger('uvicorn.error').info(json.dumps(record,sort_keys=True))
