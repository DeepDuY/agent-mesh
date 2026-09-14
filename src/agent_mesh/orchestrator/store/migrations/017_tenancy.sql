-- Multi-tenant groundwork: teams/groups + per-resource ownership.
--
-- * teams / team_members: a "team" (a.k.a. group). A user belongs to at most
--   one team (unique index on user_id).
-- * agents.access: JSON {"teams": [...], "users": [...]} — who may operate the
--   node (admin always bypasses).
-- * tasks.user_id / tasks.team_id: owner, derived automatically from the
--   caller's token at dispatch time.
-- * templates.owner_user_id / owner_team_id: which user/team owns the template
--   (owners manage it from the Web UI; admin owns NULL-owner/global templates).

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (team_id, user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);

ALTER TABLE agents ADD COLUMN access TEXT;

ALTER TABLE tasks ADD COLUMN user_id TEXT;
ALTER TABLE tasks ADD COLUMN team_id TEXT;

ALTER TABLE templates ADD COLUMN owner_user_id TEXT;
ALTER TABLE templates ADD COLUMN owner_team_id TEXT;
