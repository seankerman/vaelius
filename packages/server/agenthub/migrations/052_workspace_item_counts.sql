-- Shared items per workspace (docs/ACCESS_MODEL.md, "Why can't this session read X?").
--
-- The session descriptor tells a person how many memories of a narrower workspace
-- their session's recall leaves out. The count reads only this partial index
-- (active, non-private sources of one workspace), never the whole table.
CREATE INDEX IF NOT EXISTS enterprise_sources_workspace_shared
    ON enterprise_sources(tenant,external_project) WHERE active=1 AND private=0;
