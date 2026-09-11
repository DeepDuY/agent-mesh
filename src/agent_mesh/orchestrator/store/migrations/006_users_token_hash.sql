-- Rebuild users: drop the plaintext `token` column and store only the
-- SHA-256 digest (token_hash). Adds auth bookkeeping columns.
--
-- The pre-existing admin seed only held a placeholder token, so no real
-- token is lost here; `_ensure_admin_user()` boots a fresh random admin
-- token whenever token_hash is missing.

CREATE TABLE users_v2 (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    token_hash TEXT UNIQUE,
    role TEXT DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    last_login_at TIMESTAMP,
    token_created_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO users_v2 (user_id, username, password_hash, role, created_at)
SELECT user_id, username, password_hash, role, created_at FROM users;

DROP TABLE users;
ALTER TABLE users_v2 RENAME TO users;
