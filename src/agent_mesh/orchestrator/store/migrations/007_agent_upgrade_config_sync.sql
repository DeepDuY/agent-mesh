-- Agent self-upgrade + LLM config sync support.
-- Adds per-agent runtime version / arch, an upgrade request flag, and a
-- global `config_version` used to push LLM config changes to edges.

ALTER TABLE agents ADD COLUMN arch TEXT;
ALTER TABLE agents ADD COLUMN version TEXT;
ALTER TABLE agents ADD COLUMN upgrade_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN upgrade_version TEXT;
ALTER TABLE agents ADD COLUMN upgrade_requested_at TIMESTAMP;

INSERT OR IGNORE INTO settings (key, value) VALUES ('config_version', '0');
