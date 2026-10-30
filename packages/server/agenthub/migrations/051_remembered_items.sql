-- Explicit "remember" items (docs/MEMORY_GOVERNANCE.md, "Remembered items").
--
-- A remembered item is an ordinary owner-reviewed note: an owner-private source
-- holding the person's own words and one curated document derived from it
-- (agenthub.processing.knowledge.accept_local_candidate). This table holds only
-- metadata about that pair, never content: who wrote it, the workspace it is stored
-- in, whether it is personal (delivered in every workspace) and whether it is
-- "always load" (delivered at session start and with recall, to sessions whose
-- audience admits it). agenthub.remember owns every write to this table.
--
-- Versions are lifecycle corrections of the source (the document keeps its id and
-- gains a revision); withdrawal is a lifecycle withdrawal. The source and document
-- rows stay authoritative for who may read the item.
CREATE TABLE IF NOT EXISTS remembered_items(
    document_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    owner TEXT NOT NULL,
    workspace TEXT NOT NULL,
    personal SMALLINT NOT NULL DEFAULT 0 CONSTRAINT remembered_items_personal CHECK (personal IN (0,1)),
    always_load SMALLINT NOT NULL DEFAULT 0 CONSTRAINT remembered_items_always_load CHECK (always_load IN (0,1)),
    source_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    created DOUBLE PRECISION NOT NULL,
    updated DOUBLE PRECISION NOT NULL,
    withdrawn DOUBLE PRECISION);
CREATE UNIQUE INDEX IF NOT EXISTS remembered_items_request ON remembered_items(tenant,owner,request_digest);
CREATE INDEX IF NOT EXISTS remembered_items_owner ON remembered_items(tenant,owner) WHERE withdrawn IS NULL;
