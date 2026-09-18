-- Probe package distribution settings.
--
-- bootstrap_download_base: when set, the generated install scripts download the
--   probe package from this base URL (e.g. a GitHub Release
--   "https://github.com/<owner>/<repo>/releases/latest/download") instead of
--   from this orchestrator. Empty = serve the local data/bootstrap/ copy.
-- probe_release_repo: "<owner>/<repo>" used by POST /api/bootstrap/sync to pull
--   the latest prebuilt packages from its GitHub Release into data/bootstrap/.
-- probe_release_token: optional GitHub token for a private release repo.

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('bootstrap_download_base', ''),
    ('probe_release_repo', 'DeepDuY/agent-mesh-edge'),
    ('probe_release_token', '');
