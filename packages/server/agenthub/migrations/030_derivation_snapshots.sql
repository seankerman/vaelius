-- Legacy dependencies remain conservative. New jobs freeze all model inputs.
ALTER TABLE backend_jobs ADD COLUMN dependency_snapshot_complete INTEGER NOT NULL DEFAULT 0;
CREATE TABLE backend_model_input_snapshots (
    episode_job TEXT NOT NULL,
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    PRIMARY KEY(episode_job,document_id,revision_id)
);
