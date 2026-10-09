CREATE TABLE cloud_migration_receipts(
 id TEXT PRIMARY KEY,source_sha256 TEXT NOT NULL,tenant TEXT NOT NULL,
 status TEXT NOT NULL,counts_json TEXT NOT NULL,objects_json TEXT NOT NULL,
 exclusions_json TEXT NOT NULL,created DOUBLE PRECISION NOT NULL);
CREATE TABLE cloud_migrated_source_objects(
 source_id TEXT PRIMARY KEY REFERENCES enterprise_sources(id),object_key TEXT NOT NULL,
 sha256 TEXT NOT NULL,length BIGINT NOT NULL,representation TEXT NOT NULL,
 original_complete INTEGER NOT NULL);
CREATE TABLE enterprise_legacy_ids(
 tenant TEXT NOT NULL,kind TEXT NOT NULL,old_id TEXT NOT NULL,new_id TEXT NOT NULL,
 source_profile TEXT NOT NULL,PRIMARY KEY(tenant,kind,old_id));
