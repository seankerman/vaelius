CREATE TABLE IF NOT EXISTS cloud_vector_generations(
 id TEXT PRIMARY KEY,model_key TEXT NOT NULL,dimension INTEGER NOT NULL CHECK(dimension=512),
 status TEXT NOT NULL CHECK(status IN ('building','active','retired','failed')),
 created DOUBLE PRECISION NOT NULL,completed DOUBLE PRECISION,document_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cloud_document_vectors(
 generation_id TEXT NOT NULL REFERENCES cloud_vector_generations(id),
 document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id),revision_id TEXT NOT NULL,
 body_sha256 TEXT NOT NULL,embedding vector(512) NOT NULL,
 PRIMARY KEY(generation_id,document_id)
);
CREATE TABLE IF NOT EXISTS cloud_vector_state(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation_id TEXT REFERENCES cloud_vector_generations(id),
 model_key TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL
);
