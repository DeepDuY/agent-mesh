-- Distinguish a template's own note ("说明") from the node description it
-- provides to bound nodes.
--
-- `templates.description` is the template's own note (what the template is for,
-- for humans). The new `templates.node_description` is the node-purpose text
-- shown to the main agent for node selection. Effective node description is:
--     node's own `agents.description`  >  template's `node_description`
-- (both are independent of the template's `description`).

ALTER TABLE templates ADD COLUMN node_description TEXT;
