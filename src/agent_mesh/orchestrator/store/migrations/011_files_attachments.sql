-- File library + task attachments.
--
-- 1. `files` table is a standalone file library. Files are uploaded
--    independently of tasks (REST multipart, user token), deduplicated by
--    (md5 + filename), and referenced by tasks as attachments.
-- 2. `tasks.attachments` stores the FileRef snapshots (file_id/filename/
--    size/content_type/md5/download_url) attached at dispatch time, so the
--    edge can download and md5-verify them before executing the task.

CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_type TEXT,
    md5 TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE tasks ADD COLUMN attachments TEXT;
