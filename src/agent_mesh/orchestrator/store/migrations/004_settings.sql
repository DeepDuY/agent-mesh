CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO settings (key, value) VALUES
('public_url', ''),
('llm_api_key', ''),
('llm_base_url', ''),
('llm_model', '');
