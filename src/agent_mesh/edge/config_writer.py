from __future__ import annotations

import logging
import os
import platform
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def parse_model_list(raw: str) -> list[str]:
    """Parse a comma/newline separated model list into a clean list."""
    return [m.strip() for m in re.split(r"[,\n]", raw or "") if m.strip()]


def get_device_id(install_dir: str | Path | None = None) -> str:
    """Return the stable device identifier for this machine.

    Preference order:
    1. /etc/machine-id (Linux standard).
    2. A persisted random id file inside the agent install dir
       (``<install_dir>/machine-id``), generated on first run.
    3. Fallback derived from the hostname.
    """
    # 1) Linux machine-id.
    machine_id_path = Path("/etc/machine-id")
    try:
        mid = machine_id_path.read_text(encoding="utf-8").strip()
        if mid:
            return mid
    except OSError:
        pass

    # 2) Persisted id inside the install dir.
    if install_dir:
        persist_path = Path(install_dir) / "machine-id"
        try:
            existing = persist_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        try:
            persist_path.parent.mkdir(parents=True, exist_ok=True)
            new_id = _random_device_id()
            persist_path.write_text(new_id, encoding="utf-8")
            return new_id
        except OSError:
            logger.warning("cannot persist machine-id at %s", persist_path)

    # 3) Fallback derived from hostname.
    import hashlib

    return hashlib.sha256(platform.node().encode()).hexdigest()


def _random_device_id() -> str:
    """Generate a random device id based on a millisecond timestamp plus jitter."""
    ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"m{ms}-{os.getpid()}"


def _canonical_model_id(model: str) -> str:
    """Plain model name (last path segment), e.g. ``vip/glm-5.2`` -> ``glm-5.2``."""
    return model.rsplit("/", 1)[-1].rsplit("/", 1)[-1]


def _model_map_key(model_id: str) -> str:
    """opencode models-map key: the id minus the leading provider segment.

    The provider is always ``anthropic`` in the generated config, so
    ``anthropic/vip/kimi-k2.7-code`` becomes ``vip/kimi-k2.7-code``. Ids without
    the provider prefix are used verbatim.
    """
    model_id = model_id.strip()
    prefix = "anthropic/"
    return model_id[len(prefix):] if model_id.startswith(prefix) else model_id


def build_opencode_config(
    api_key: str,
    base_url: str,
    model: str,
    allowed_tools: list[str] | None = None,
    models: list[str] | None = None,
) -> dict[str, Any]:
    permission: dict[str, Any] = {
        "edit": "allow",
        "bash": "allow",
        "webfetch": "ask",
        "mcp__*": "deny",
    }
    if allowed_tools:
        # v1: simple mapping for demo; deeper tool gating can be added later.
        for tool in allowed_tools:
            permission[tool] = "allow"
    # The models map is built entirely from the orchestrator-configured list
    # (settings.llm_models); nothing is hardcoded here. The map keys must be the
    # gateway's EXACT model ids minus the provider prefix, because opencode
    # resolves the model by key and forwards it verbatim (a wrong key -> 5xx/404).
    model_map: dict[str, Any] = {}
    for mid in models or []:
        mid = (mid or "").strip()
        if not mid:
            continue
        model_map[_model_map_key(mid)] = {"name": _canonical_model_id(mid)}
    if model:
        model_map.setdefault(_model_map_key(model), {"name": _canonical_model_id(model)})
    return {
        "model": model,
        "provider": {
            "anthropic": {
                "options": {
                    "apiKey": api_key,
                    "baseURL": base_url,
                },
                "models": model_map,
            }
        },
        "permission": permission,
    }


def get_hostname() -> str:
    return platform.node()


def get_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "win32"
    return "linux"


def _parse_os_release(text: str) -> str | None:
    """Parse an /etc/os-release payload into ``<ID> <VERSION_ID>``.

    Returns e.g. ``"ubuntu 22.04"`` / ``"centos 7"`` / ``"kylin V10"``, or the
    bare ``ID`` when no version is present, or ``None`` when unparseable.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"\'')
    os_id = fields.get("ID", "").strip()
    if not os_id:
        return None
    version_id = fields.get("VERSION_ID", "").strip()
    return f"{os_id} {version_id}" if version_id else os_id


def get_distro() -> str | None:
    """Linux distribution name + version, e.g. ``"ubuntu 22.04"``.

    Only populated on Linux (from ``/etc/os-release``); returns ``None`` on
    other platforms or when the file is unreadable/unparseable so callers can
    fall back to the platform family from :func:`get_os`.
    """
    if platform.system().lower() != "linux":
        return None
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_os_release(text)


def get_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def read_agent_version(install_dir: str | None = None) -> str | None:
    """Return the installed package version, or None if unknown (dev mode)."""
    if not install_dir:
        return None
    version_path = Path(install_dir) / "etc" / "agent_version"
    try:
        value = version_path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


_EDGE_ENV_LLM_KEYS = {
    "EDGE_LLM_API_KEY": "llm_api_key",
    "EDGE_LLM_BASE_URL": "llm_base_url",
    "EDGE_LLM_MODEL": "llm_model",
}


def apply_llm_config(install_dir: str, config: dict[str, str], version: str) -> None:
    """Persist the synced LLM config and record the applied version.

    Single-line values (api key / base url / model) go into ``etc/edge.env``;
    multi-line values (``system_prompt`` and the ``llm_models`` list) are written
    to dedicated files so they survive restarts without breaking shell sourcing
    of edge.env. The running process applies the values in-memory separately
    (see EdgeAgent._apply_llm_config).
    """
    install = Path(install_dir)
    etc = install / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    env_path = etc / "edge.env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = {k: False for k in _EDGE_ENV_LLM_KEYS}
        out: list[str] = []
        for line in lines:
            matched = False
            for env_key, cfg_key in _EDGE_ENV_LLM_KEYS.items():
                if line.startswith(env_key + "="):
                    out.append(f"{env_key}={config.get(cfg_key, '')}")
                    updated[env_key] = True
                    matched = True
                    break
            if not matched:
                out.append(line)
        for env_key, done in updated.items():
            if not done:
                out.append(f"{env_key}={config.get(_EDGE_ENV_LLM_KEYS[env_key], '')}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # Multi-line / list values: dedicated files (raw text, newlines preserved).
    (etc / "system_prompt").write_text(config.get("system_prompt", "") or "", encoding="utf-8")
    (etc / "llm_models").write_text(config.get("llm_models", "") or "", encoding="utf-8")
    (etc / "config_version").write_text(str(version), encoding="utf-8")


def read_system_prompt(install_dir: str) -> str:
    """Node-level system prompt persisted by the config-sync channel (or "")."""
    try:
        return (Path(install_dir) / "etc" / "system_prompt").read_text(encoding="utf-8")
    except OSError:
        return ""


def read_llm_models(install_dir: str) -> str:
    """Available model list persisted by config-sync (raw comma/newline text)."""
    try:
        return (Path(install_dir) / "etc" / "llm_models").read_text(encoding="utf-8")
    except OSError:
        return ""


def persist_edge_token(install_dir: str, token: str) -> None:
    """Persist the agent's own token into edge.env (EDGE_TOKEN=...).

    Keeps the token stable across restarts once the orchestrator issues the
    agent an independent token on first registration.
    """
    install = Path(install_dir)
    etc = install / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    env_path = etc / "edge.env"
    if not env_path.exists():
        env_path.write_text(f"EDGE_TOKEN={token}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    out: list[str] = []
    for line in lines:
        if line.startswith("EDGE_TOKEN="):
            out.append(f"EDGE_TOKEN={token}")
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(f"EDGE_TOKEN={token}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_edge_token(install_dir: str) -> str | None:
    """Read the EDGE_TOKEN currently configured in edge.env, if any."""
    env_path = Path(install_dir) / "etc" / "edge.env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("EDGE_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


WRAPPER_SCRIPT = r"""#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
source "$PWD/etc/edge.env"
export PATH="$PWD/bin:$PATH"
BIN="$PWD/bin/agent-mesh-edge.bin"
OLD="$PWD/bin/agent-mesh-edge.bin.old"
MARKER="$PWD/etc/upgrading"
STARTED="$PWD/etc/upgrade-started"

# Post-upgrade rollback: if a previous boot started the new binary but it never
# confirmed healthy (first successful heartbeat clears the markers), restore the
# previous binary so we don't boot-loop on a broken upgrade.
if [ -f "$OLD" ] && [ -f "$MARKER" ]; then
    if [ -f "$STARTED" ]; then
        rm -f "$MARKER"
        rm -f "$STARTED"
        mv -f "$OLD" "$BIN"
        [ -f "$PWD/etc/agent_version.bak" ] && mv -f "$PWD/etc/agent_version.bak" "$PWD/etc/agent_version"
        echo "agent-mesh-edge: rolled back to previous binary" >&2
    else
        # First boot after an upgrade: run the new binary and mark it.
        touch "$STARTED"
    fi
fi

exec "$BIN" "$@"
"""


def find_runtime(runtime: str) -> str:
    exe = shutil.which(runtime)
    if exe:
        return exe
    # Fallback to known opencode install path on this machine.
    home = Path.home()
    candidates = [
        home / ".opencode" / "bin" / runtime,
        home / ".opencode" / "bin" / f"{runtime}.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return runtime
