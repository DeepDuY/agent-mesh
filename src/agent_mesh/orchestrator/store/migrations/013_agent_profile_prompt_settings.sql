-- Agent profile + node-level LLM prompt, and configurable model list.
--
-- 1. `agents.description`: operator-set free-form note about what the node is
--    for; surfaced to the main agent (REST/MCP) so it can pick the right node.
-- 2. `agents.system_prompt`: node-level role/context injected into every llm
--    task's prompt by the edge (top of `_wrap_llm_instruction`). Operator-set.
-- 3. `settings.llm_models`: newline/comma separated gateway model ids. This is
--    the single source of truth for the opencode models map (the edge no longer
--    hardcodes any model) and the dispatch-time allow-list. Empty = no limit.
-- 4. `settings.system_prompt`: optional global default, overridden per node.

ALTER TABLE agents ADD COLUMN description TEXT;
ALTER TABLE agents ADD COLUMN system_prompt TEXT;

INSERT OR IGNORE INTO settings (key, value) VALUES ('system_prompt', '');
INSERT OR IGNORE INTO settings (key, value) VALUES (
    'llm_models',
    'anthropic/deepseek-v4-flash
anthropic/deepseek-v4-pro
anthropic/vip/kimi-k2.7-code'
);
