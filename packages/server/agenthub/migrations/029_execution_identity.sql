-- Provider/model identity fences observer handles and confirmed-return reuse.
ALTER TABLE backend_observers ADD COLUMN execution_key TEXT NOT NULL DEFAULT '';
