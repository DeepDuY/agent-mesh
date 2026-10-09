-- Support a user belonging to multiple teams (many-to-many) and multi-team
-- ownership for tasks / schedules / templates.
--
-- * team_members already has PRIMARY KEY (team_id, user_id); drop the unique
--   index on user_id that limited a user to a single team.
-- * tasks / schedules gain team_ids; templates gain owner_team_ids. The legacy
--   single-team columns are backfilled into the new arrays and then left unused.
DROP INDEX IF EXISTS idx_team_members_user;

ALTER TABLE tasks ADD COLUMN team_ids TEXT;
UPDATE tasks SET team_ids = json_array(team_id) WHERE team_id IS NOT NULL;

ALTER TABLE schedules ADD COLUMN team_ids TEXT;
UPDATE schedules SET team_ids = json_array(team_id) WHERE team_id IS NOT NULL;

ALTER TABLE templates ADD COLUMN owner_team_ids TEXT;
UPDATE templates SET owner_team_ids = json_array(owner_team_id) WHERE owner_team_id IS NOT NULL;
