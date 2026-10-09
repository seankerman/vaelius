-- Deferred provenance constraints preserve canonical insertion order while
-- making the whole source/claim/checkpoint operation atomic at commit.
ALTER TABLE knowledge_revisions ADD CONSTRAINT knowledge_revision_document_fk FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_active_revision_fk FOREIGN KEY(active_revision_id) REFERENCES knowledge_revisions(revision_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_document_members ADD CONSTRAINT knowledge_member_document_fk FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_document_members ADD CONSTRAINT knowledge_member_memory_fk FOREIGN KEY(memory_id) REFERENCES memories(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_support ADD CONSTRAINT knowledge_support_revision_fk FOREIGN KEY(revision_id) REFERENCES knowledge_revisions(revision_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_support ADD CONSTRAINT knowledge_support_memory_fk FOREIGN KEY(source_memory_id) REFERENCES memories(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_generation_documents ADD CONSTRAINT knowledge_generation_fk FOREIGN KEY(generation_id) REFERENCES knowledge_generations(generation_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_generation_documents ADD CONSTRAINT knowledge_generation_document_fk FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_temporal_assertions ADD CONSTRAINT knowledge_assertion_document_fk FOREIGN KEY(document_id) REFERENCES knowledge_documents(document_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_temporal_assertions ADD CONSTRAINT knowledge_assertion_revision_fk FOREIGN KEY(revision_id) REFERENCES knowledge_revisions(revision_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_temporal_evidence ADD CONSTRAINT knowledge_assertion_evidence_fk FOREIGN KEY(assertion_id) REFERENCES knowledge_temporal_assertions(assertion_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE knowledge_temporal_evidence ADD CONSTRAINT knowledge_assertion_memory_fk FOREIGN KEY(source_memory_id) REFERENCES memories(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_source_revisions ADD CONSTRAINT backend_revision_source_fk FOREIGN KEY(source_id) REFERENCES enterprise_sources(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_source_heads ADD CONSTRAINT backend_head_source_fk FOREIGN KEY(source_id) REFERENCES enterprise_sources(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_observer_dependencies ADD CONSTRAINT backend_observer_fk FOREIGN KEY(observer_id) REFERENCES backend_observers(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_observer_dependencies ADD CONSTRAINT backend_observer_source_fk FOREIGN KEY(source_id) REFERENCES enterprise_sources(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_processing_dependencies ADD CONSTRAINT backend_processing_job_fk FOREIGN KEY(episode_job) REFERENCES curation_episode_jobs(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_processing_dependencies ADD CONSTRAINT backend_processing_source_fk FOREIGN KEY(source_id) REFERENCES enterprise_sources(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_jobs ADD CONSTRAINT backend_job_episode_fk FOREIGN KEY(episode_job) REFERENCES curation_episode_jobs(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE backend_jobs ADD CONSTRAINT backend_job_observer_fk FOREIGN KEY(observer_id) REFERENCES backend_observers(id) DEFERRABLE INITIALLY DEFERRED;
