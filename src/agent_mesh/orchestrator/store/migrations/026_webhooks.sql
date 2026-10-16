-- Outbound webhook notifications (task / review events).
--
--   webhook_enabled:       "1" / "0" master switch (default off).
--   webhook_kind:          dingtalk | feishu | wecom | generic.
--   webhook_url:           target URL (http/https).
--   webhook_secret:        optional signing secret (DingTalk/Feishu sign, or
--                          HMAC-SHA256 header for the generic kind).
--   webhook_task_events:   CSV subset of completed,failed,timed_out,cancelled,denied.
--   webhook_review_events: CSV subset of allowed,denied,pending,approved,rejected,expired.

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('webhook_enabled', '0'),
    ('webhook_kind', 'generic'),
    ('webhook_url', ''),
    ('webhook_secret', ''),
    ('webhook_task_events', 'completed,failed,timed_out,cancelled,denied'),
    ('webhook_review_events', 'allowed,denied,pending,approved,rejected,expired');
