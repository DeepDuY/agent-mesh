-- Optional AI review gate.
--
-- When enabled, dispatched tasks are scored (0-100) by an LLM against the target
-- node's description/permission before they reach the queue. Low-score tasks are
-- either denied outright or held as `pending_review` for a human decision.
--
--  * review_score / review_reason: the latest verdict, denormalised onto the task
--    so the task list/detail can show it without a join.
--  * task_reviews: full audit trail, one row per AI decision and per human
--    approve/reject/expire action (drives the "审核" module history).

ALTER TABLE tasks ADD COLUMN review_score INTEGER;
ALTER TABLE tasks ADD COLUMN review_reason TEXT;

CREATE TABLE IF NOT EXISTS task_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    mode TEXT,
    score INTEGER,
    threshold INTEGER,
    verdict TEXT,          -- allow | block
    action TEXT,           -- allowed | denied | pending_review
    reason TEXT,
    violations TEXT,
    model TEXT,
    source TEXT,           -- dispatch | approve | reject | expire
    actor TEXT,            -- 'ai' or a username
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_reviews_task ON task_reviews(task_id, id);
CREATE INDEX IF NOT EXISTS idx_task_reviews_created ON task_reviews(created_at);

INSERT OR IGNORE INTO settings (key, value) VALUES ('review_policy', '');
