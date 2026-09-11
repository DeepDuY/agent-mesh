-- Task live log stream.
--
-- Edge agents stream the LLM/command execution output to the orchestrator in
-- near-real-time (`POST /api/edge/task_log`), so operators can watch what an
-- LLM task is producing while it runs instead of only the final result tail.
--
-- `kind` values: text (LLM-generated content), error, complete, raw (verbatim
-- stdout/stderr line). `id` is a monotonic per-task sequence consumed via
-- `?after_id=` incremental polling.

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
