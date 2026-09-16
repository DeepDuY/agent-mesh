-- Optional expiry for user API tokens. NULL means the token never expires
-- (backward compatible with tokens created before this migration).
ALTER TABLE users ADD COLUMN token_expires_at TIMESTAMP;
