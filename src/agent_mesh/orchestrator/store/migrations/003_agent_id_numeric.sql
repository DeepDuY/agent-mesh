-- agents 表已改为数字自增主键 id，agent_id 改为非唯一显示名，mac 作为设备唯一标识。
-- 若从旧表迁移，直接重建表结构，按 agent_id 去重后保留最新记录，mac 留空。

CREATE TABLE IF NOT EXISTS agents_new (
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
    llm_api_key TEXT,
    llm_base_url TEXT,
    llm_model TEXT,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- De-duplicate old rows, keeping the most recently updated one per agent_id.
DELETE FROM agents
WHERE rowid NOT IN (
    SELECT rowid
    FROM agents AS a
    WHERE a.updated_at = (
        SELECT MAX(updated_at)
        FROM agents AS b
        WHERE b.agent_id = a.agent_id
    )
    GROUP BY agent_id
);

INSERT INTO agents_new (
    agent_id, alias, runtime, hostname, os, online, last_seen_at,
    current_task_id, metadata, created_at, updated_at
)
SELECT
    agent_id, alias, runtime, hostname, os, online, last_seen_at,
    current_task_id, metadata, created_at, updated_at
FROM agents;

DROP TABLE agents;
ALTER TABLE agents_new RENAME TO agents;

CREATE INDEX IF NOT EXISTS idx_agents_mac ON agents(mac);

-- tasks.agent_id used to reference agents.agent_id; now it is a free-form target key.
-- Remove the foreign key constraint if it exists by recreating the table.
CREATE TABLE IF NOT EXISTS tasks_new (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    mode TEXT DEFAULT 'llm',
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

INSERT INTO tasks_new SELECT * FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
