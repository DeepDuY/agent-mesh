CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    role TEXT DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    mac TEXT UNIQUE,
    alias TEXT,
    runtime TEXT,
    hostname TEXT,
    os TEXT,
    online BOOLEAN DEFAULT FALSE,
    last_seen_at TIMESTAMP,
    current_task_id TEXT,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agents_mac ON agents(mac);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    instruction TEXT NOT NULL,
    workdir TEXT DEFAULT '.',
    timeout_s INTEGER DEFAULT 300,
    model TEXT,
    allowed_tools TEXT,
    output_limit INTEGER DEFAULT 200000,
    status TEXT NOT NULL,
    max_retries INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assigned_at TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    depends_on TEXT,
    dispatched_by TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT UNIQUE NOT NULL,
    agent_id TEXT NOT NULL,
    enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    exit_code INTEGER,
    stdout_tail TEXT,
    stderr_tail TEXT,
    duration_ms INTEGER,
    summary TEXT,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER,
    content_type TEXT,
    storage_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    event_type TEXT NOT NULL,
    agent_id TEXT,
    user_id TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Default admin user: admin/admin (password must be changed in production)
-- The hash below is intentionally invalid so that the migration sets a real one.
INSERT OR IGNORE INTO users (user_id, username, password_hash, token, role)
VALUES (
    'u-admin',
    'admin',
    '$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderpl',
    'admin-token-change-me',
    'admin'
);
