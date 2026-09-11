-- Node templates: reusable, reference-based node configuration.
--
-- A template is a named preset that a node binds to via `agents.template_id`.
-- Editing a template affects every node bound to it (live binding). Node-level
-- values (agents.system_prompt / llm_model) override / prepend to the template.
--
-- v1 fields: system_prompt (role/context), llm_model (default model), plus
-- allowed_tools / data reserved for future node permission control. The
-- former global `settings.system_prompt` is dropped: prompts come from the
-- built-in base + node + template layers only.

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    system_prompt TEXT,
    llm_model TEXT,
    allowed_tools TEXT,
    data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE agents ADD COLUMN template_id INTEGER REFERENCES templates(id) ON DELETE SET NULL;

DELETE FROM settings WHERE key = 'system_prompt';
