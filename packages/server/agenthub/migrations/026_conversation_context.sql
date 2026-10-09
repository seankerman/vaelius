-- Bounded parent/conversation expansion; originals remain outside search results.
CREATE INDEX IF NOT EXISTS memories_conversation_context
    ON memories(session,project,created,id) WHERE active=1;
CREATE INDEX IF NOT EXISTS memories_turn_context
    ON memories(session,project,turn,created,id) WHERE active=1;
