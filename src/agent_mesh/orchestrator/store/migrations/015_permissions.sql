-- Unified permission templates.
--
-- Permission is now a template-level capability (`templates.permission`), using
-- the OpenCode `permission` object format, shared by both llm and command
-- execution modes. The former per-task `allowed_tools` (and the reserved
-- template `allowed_tools`) are removed; the global fallback is the
-- `default_permission` setting.
--
-- The three built-in templates (plan/build/readonly) and the default
-- `default_permission` value are seeded idempotently in code
-- (`_ensure_default_templates`) so the profile definitions live in one place
-- (`agent_mesh.shared.permissions`).

ALTER TABLE templates ADD COLUMN permission TEXT;

ALTER TABLE templates DROP COLUMN allowed_tools;

ALTER TABLE tasks DROP COLUMN allowed_tools;
