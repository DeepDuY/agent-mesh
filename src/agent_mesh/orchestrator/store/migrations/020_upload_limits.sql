-- Runtime-configurable upload / transfer limits (Web console "上传大小限制").
--
-- These supersede the AGENT_MESH_ARTIFACT_MAX_SIZE_MB / _MAX_TOTAL_MB env vars,
-- which are now only used as the ArtifactStore / OrchestratorConfig fallback.
-- Effective values are read per request from `settings`, so a change takes
-- effect immediately and across all workers.

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('file_max_size_mb', '100'),        -- file-library single-file cap
    ('artifact_max_size_mb', '100'),    -- probe artifact single-file cap
    ('artifact_task_total_mb', '100'),  -- per-task artifact total cap
    ('artifact_total_mb', '200'),       -- global artifact-store quota
    ('artifact_evict_oldest', '1'),     -- recycle oldest artifacts when full
    ('artifact_timeout_s', '300');      -- probe upload / page download timeout
