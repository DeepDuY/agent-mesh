-- Scheduled / recurring tasks.
--
-- A schedule dispatches a task (command or llm) on a cron expression. The
-- orchestrator's in-process scheduler owns execution (single process only); see
-- docs/orchestrator.md.
--
-- * cron: standard 5-field expression, evaluated in `timezone` (IANA name; empty
--   = the server's system timezone from `settings.schedule_timezone`).
-- * agent_ref: node reference (numeric id / device_id / display name) resolved
--   at each run; a missing/forbidden node is recorded in `last_status`.
-- * constraints / attachments: JSON, mirroring the task dispatch parameters.
-- * owner (user_id/team_id) drives visibility, like tasks.
-- * next_run_at / last_run_at are UTC ISO timestamps.
-- * last_task_id / last_status reflect the most recent dispatch.

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    cron TEXT NOT NULL,
    timezone TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    agent_ref TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'llm',
    instruction TEXT NOT NULL,
    constraints TEXT,
    attachments TEXT,
    user_id TEXT,
    team_id TEXT,
    created_by TEXT,
    next_run_at TIMESTAMP,
    last_run_at TIMESTAMP,
    last_task_id TEXT,
    last_status TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);

-- Timezone used to evaluate cron when a schedule has no explicit timezone.
-- Empty = detect the server's system timezone at evaluation time.
INSERT OR IGNORE INTO settings (key, value) VALUES ('schedule_timezone', '');
