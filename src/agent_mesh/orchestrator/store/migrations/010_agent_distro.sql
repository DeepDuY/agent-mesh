-- Edge probes report the Linux distribution name/version (e.g. "ubuntu 22.04",
-- "centos 7", "kylin V10") as a static SYSTEM telemetry field, registered in
-- `shared.schemas.SYSTEM_FIELDS`. `os` keeps the platform family
-- (linux/darwin/win32) used for upgrade-package selection.
ALTER TABLE agents ADD COLUMN distro TEXT;
