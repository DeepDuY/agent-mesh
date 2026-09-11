-- LLM session reuse, per-task skills, resource metrics, and the skills library.
--
-- 1. LLM session reuse: a task may carry an optional opencode `session_id` to
--    continue a previous session; the completed task records the session it used.
-- 2. Skills: tasks may carry a JSON list of skills (unused for now; skills are
--    fetched on-demand by the edge/agent via the prompt). New `skills` table is
--    the server-side skill library (uploaded as a zip, only summaries exposed).
-- 3. Resource metrics: edges report CPU / memory usage with each heartbeat.

ALTER TABLE tasks ADD COLUMN session_id TEXT;
ALTER TABLE task_results ADD COLUMN session_id TEXT;
ALTER TABLE tasks ADD COLUMN skills TEXT;

ALTER TABLE agents ADD COLUMN cpu_percent REAL;
ALTER TABLE agents ADD COLUMN mem_percent REAL;
ALTER TABLE agents ADD COLUMN mem_used_mb REAL;
ALTER TABLE agents ADD COLUMN mem_total_mb REAL;

CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    filename TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
