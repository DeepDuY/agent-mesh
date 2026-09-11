-- Rename agents.mac to device_id (node identity now uses /etc/machine-id
-- or a persisted random id, instead of the loopback MAC).

ALTER TABLE agents RENAME COLUMN mac TO device_id;

-- Recreate the unique index on the renamed column if it was tied to `mac`.
DROP INDEX IF EXISTS idx_agents_mac;
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_device_id ON agents(device_id);
