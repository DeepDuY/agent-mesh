import enum

VERSION = "1.6.3"          # edge probe / bootstrap package version
SERVER_VERSION = "1.7.1"   # orchestrator (server) version

# Initial gateway model ids seeded into `settings.llm_models` on a fresh
# database. This is only a default value, not a hardcoded allow-list: the edge
# builds its opencode models map from this setting and operators edit it in the
# Web console. Kept in sync with migration 013.
DEFAULT_LLM_MODELS = "\n".join(
    (
        "anthropic/deepseek-v4-flash",
        "anthropic/deepseek-v4-pro",
        "anthropic/vip/kimi-k2.7-code",
    )
)

DEFAULT_SWEEP_INTERVAL_S = 5
DEFAULT_OFFLINE_AFTER_S = 15


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    DENIED = "denied"
