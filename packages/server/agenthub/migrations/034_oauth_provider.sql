-- Provider-issued access tokens retain only digest/application metadata.
-- The authorization server exclusively owns refresh, reuse detection and logout.
ALTER TABLE enterprise_credentials ADD COLUMN IF NOT EXISTS oauth_provider TEXT;
