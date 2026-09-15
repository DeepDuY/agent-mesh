-- Record the source IP the orchestrator observed on the last heartbeat/poll, so
-- the node detail page can show where a node connects from (useful to identify
-- misconfigured/legacy probes that never register).
ALTER TABLE agents ADD COLUMN ip_address TEXT;
