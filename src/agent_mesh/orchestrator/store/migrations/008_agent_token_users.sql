-- Per-agent independent tokens + agent<->user access association.
--
-- Each edge agent authenticates with its OWN token (stored hashed here),
-- independent of user login sessions. `agent_users` records which users are
-- allowed to operate the machine (many-to-many); full multi-user management
-- is built on top of this later.

ALTER TABLE agents ADD COLUMN token_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_token_hash ON agents(token_hash);

CREATE TABLE IF NOT EXISTS agent_users (
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (agent_id, user_id)
);
