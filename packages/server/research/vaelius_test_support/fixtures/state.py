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


def visible_episode_claims(state):
    from agenthub.processing.knowledge import active_generation_id
    from agenthub.processing.episode_pipeline import get_episode_view
    generation=active_generation_id(state.db)
    if not generation:return []
    result=[]
    for row in state.db.execute('SELECT project,session,turn FROM curation_episode_jobs WHERE generation_id=? ORDER BY created,id',(generation,)).fetchall():
        view=get_episode_view(state.db,generation,row['project'],row['session'],row['turn'])
        if view:
            result.extend({'text':view['summary'],'revision':a['revision_id']} for a in view['assertions'])
    return result
