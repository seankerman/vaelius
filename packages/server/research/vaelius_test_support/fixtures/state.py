"""Canonical PostgreSQL processing state for synthetic algorithm fixtures."""
from agenthub.processing.state import State as ProcessingState
from agenthub.postgres import PostgresConnection, connect
from .postgres import database

def State(home):
    return ProcessingState.from_database(home,PostgresConnection(connect(database(home))))


def invalidate_source(state, source_id):
    """Synthetic source loss for algorithm recheck tests; API lifecycle has its own tests."""
    with state.db:
        state.db.execute('UPDATE memories SET active=0 WHERE id=?',(source_id,))
