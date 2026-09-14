"""Unified permission model shared by the orchestrator and the edge.

The wire format is OpenCode's ``permission`` object: a mapping of tool name (or
``"*"``) to an action (``"allow" | "ask" | "deny"``), plus an object form for
fine-grained, pattern based rules (the last matching rule wins)::

    {"edit": "deny",
     "bash": {"*": "deny", "ls *": "allow", "git status": "allow"}}

The same object drives both execution modes:

* ``llm`` mode   -> embedded in the generated ``opencode.json``; OpenCode
  enforces it (explicit ``deny`` is still enforced under ``--auto``).
* ``command`` mode -> evaluated by :func:`evaluate_bash` on the edge before the
  shell is spawned.

``command`` evaluation treats ``"ask"`` as ``"deny"``: there is no human at the
edge to approve a prompt. Keep templates to explicit ``allow``/``deny``.

This matcher is a guard against accidental/destructive commands, **not** a
security boundary: shell indirection (``bash -c "$(base64 ...)"``), variable
expansion and the like can evade a pattern matcher. Strong containment requires
an OS sandbox (out of scope for v1).
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from typing import Any

Action = str  # "allow" | "ask" | "deny"

# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

#: Full access. Backwards compatible with the pre-permission behaviour.
BUILD: dict[str, Any] = {"*": "allow"}

#: Read-only shell commands that are safe to run for analysis/planning.
_READONLY_BASH: dict[str, str] = {
    "*": "deny",
    "ls": "allow",
    "ls *": "allow",
    "cat *": "allow",
    "head *": "allow",
    "tail *": "allow",
    "grep *": "allow",
    "rg *": "allow",
    "find *": "allow",
    "wc *": "allow",
    "stat *": "allow",
    "df *": "allow",
    "du *": "allow",
    "pwd": "allow",
    "whoami": "allow",
    "date": "allow",
    "echo *": "allow",
    "uname *": "allow",
    "git status": "allow",
    "git status *": "allow",
    "git log": "allow",
    "git log *": "allow",
    "git diff": "allow",
    "git diff *": "allow",
    "git show *": "allow",
    "git branch": "allow",
    "git branch *": "allow",
    # The edge prompt tells the LLM it may fetch the skill library over HTTP.
    "curl *": "allow",
}

#: Planning profile: analysis only, no file modifications. Shell is limited to
#: the read-only allow-list, and web fetch is permitted for research.
PLAN: dict[str, Any] = {
    "edit": "deny",
    "bash": _READONLY_BASH,
    "webfetch": "allow",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
}

#: Strictest profile: pure read tools, no shell and no network.
READONLY: dict[str, Any] = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
}

PROFILES: dict[str, dict[str, Any]] = {
    "build": BUILD,
    "plan": PLAN,
    "readonly": READONLY,
}

#: Profile used when nothing is configured (both the seeded global default and
#: the last-resort fallback).
DEFAULT_PROFILE = "readonly"

PROFILE_DESCRIPTIONS: dict[str, str] = {
    "build": "完全放开：允许编辑、任意 shell 命令与联网。",
    "plan": "规划模式：禁止改文件，shell 仅限只读命令，允许联网检索。",
    "readonly": "只读模式：仅允许 read/glob/grep，无 shell、无联网。",
}


def expand_profile(name: str) -> dict[str, Any]:
    """Return a deep copy of a built-in profile (raises ``KeyError`` if unknown)."""
    return copy.deepcopy(PROFILES[name])


def default_permission() -> dict[str, Any]:
    """The last-resort permission when neither a template nor a global default is set."""
    return expand_profile(DEFAULT_PROFILE)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("^" + "".join(parts) + "$", re.DOTALL)


def glob_match(pattern: str, text: str) -> bool:
    """OpenCode-compatible wildcard match (``*`` = any chars, ``?`` = one char)."""
    return bool(_compile(pattern).match(text))


def _rules_object(rules: Any) -> dict[str, str] | None:
    """Normalize a permission value to an ordered ``{pattern: action}`` mapping.

    A plain string (``"allow"``) is the catch-all shorthand for ``{"*": action}``.
    """
    if rules is None:
        return None
    if isinstance(rules, str):
        return {"*": rules}
    if isinstance(rules, dict):
        return {str(k): str(v) for k, v in rules.items()}
    return None


def evaluate_rules(rules: Any, value: str, default: Action = "ask") -> Action:
    """Evaluate an object-form rule set against ``value``; last match wins."""
    obj = _rules_object(rules)
    if not obj:
        return default
    action = default
    for pattern, act in obj.items():
        if glob_match(pattern, value):
            action = act
    return action


_SHELL_SEPARATORS = re.compile(r"&&|\|\||;|\n|\|")


def split_commands(command: str) -> list[str]:
    """Best-effort split of a shell string into individual commands.

    Splits on ``&&``, ``||``, ``;``, ``|`` and newlines. This is intentionally
    simple: quoted separators are not special-cased, so it can both over- and
    under-split. It is a mis-operation guard, not a parser.
    """
    return [part.strip() for part in _SHELL_SEPARATORS.split(command or "") if part.strip()]


def _tool_action(permission: dict[str, Any] | None, tool: str, value: str) -> Action:
    """Resolve the action for a tool, honouring the top-level ``"*"`` fallback."""
    if not permission:
        return "ask"
    # A specific tool key overrides the global "*" default.
    if tool in permission:
        specific = permission[tool]
        if isinstance(specific, dict):
            return evaluate_rules(specific, value)
        return str(specific)
    if "*" in permission:
        return str(permission["*"])
    return "ask"


def evaluate_command(permission: dict[str, Any] | None, command: str) -> Action:
    """Evaluate a command-mode shell string.

    Every sub-command must resolve to ``"allow"``; anything else (``deny`` or
    ``ask``) denies the whole task. Returns ``"allow"`` or ``"deny"``.
    """
    subs = split_commands(command)
    if not subs:
        return "allow"
    for sub in subs:
        if _tool_action(permission, "bash", sub) != "allow":
            return "deny"
    return "allow"


# ---------------------------------------------------------------------------
# Serialization helpers (settings / template columns store JSON text)
# ---------------------------------------------------------------------------


def dumps(permission: dict[str, Any] | None) -> str:
    return json.dumps(permission if permission is not None else {}, ensure_ascii=False)


def loads(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None
