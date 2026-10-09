-- Task runs (batch dispatch grouping) + idempotency keys for batch dispatch.
ALTER TABLE tasks ADD COLUMN run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    run_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, key)
);
