-- 'feedback' and 'curate' are now enforced as their own actions. Before this,
-- 'read' allowed outcome feedback and 'correct' allowed curation. Every grant that
-- exists when this migration runs keeps those abilities (legacy_actions=1); grants
-- issued afterwards state them explicitly. Stored action lists are not rewritten,
-- so the actions reported on renewal (and clients' scope checks) are unchanged.
ALTER TABLE enterprise_credentials ADD COLUMN IF NOT EXISTS legacy_actions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE enterprise_credential_families ADD COLUMN IF NOT EXISTS legacy_actions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE enterprise_delegations ADD COLUMN IF NOT EXISTS legacy_actions INTEGER NOT NULL DEFAULT 0;
UPDATE enterprise_credentials SET legacy_actions=1;
UPDATE enterprise_credential_families SET legacy_actions=1;
UPDATE enterprise_delegations SET legacy_actions=1;
