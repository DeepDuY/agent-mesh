#!/usr/bin/env bash
set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh-agent}"
AGENT_ID="${EDGE_ALIAS:-$(hostname -s 2>/dev/null || echo agent)}"
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-}"
TOKEN="${TOKEN:-}"
WORK_DIR="${WORK_DIR:-${INSTALL_DIR}/work}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$ORCHESTRATOR_URL" ]; then
    echo "ERROR: ORCHESTRATOR_URL is required" >&2
    exit 1
fi
if [ -z "$TOKEN" ]; then
    echo "ERROR: TOKEN is required" >&2
    exit 1
fi

# ========== Pre-flight checks ==========

if [ "${SKIP_PREFLIGHT:-0}" != "1" ]; then
    echo "==> Running pre-flight checks..."

    if [ "$(id -u)" -ne 0 ]; then
        if command -v systemctl >/dev/null 2>&1; then
            echo "WARNING: not running as root, systemd service registration will be skipped" >&2
            SKIP_SERVICE=1
        fi
    fi

    AVAILABLE=$(df -m "$INSTALL_DIR" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
    if [ "$AVAILABLE" -lt 500 ] 2>/dev/null; then
        echo "ERROR: insufficient disk space at ${INSTALL_DIR} (need ~500MB, have ${AVAILABLE}MB)." >&2
        echo "       The package includes the edge binary + opencode (~250MB unpacked)." >&2
        exit 1
    fi

    # PyInstaller freezes the build host's libpython, so a package built on a new
    # distro fails to load on older ones (e.g. "libpython3.12.so.1.0: GLIBC_2.38
    # not found"). MANIFEST.json records the required floor; fail early and
    # actionably instead of leaving a boot-looping service.
    if [ "$(uname -s)" = "Linux" ] && [ -f "$SCRIPT_DIR/MANIFEST.json" ]; then
        REQUIRED_GLIBC="$(sed -n 's/.*"glibc_min"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$SCRIPT_DIR/MANIFEST.json" | head -n1)"
        HOST_GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
        if [ -n "$REQUIRED_GLIBC" ] && [ -n "$HOST_GLIBC" ]; then
            if [ "$(printf '%s\n%s\n' "$REQUIRED_GLIBC" "$HOST_GLIBC" | sort -V | head -n1)" != "$REQUIRED_GLIBC" ]; then
                echo "ERROR: this probe package requires glibc >= ${REQUIRED_GLIBC}, but this host has ${HOST_GLIBC}." >&2
                echo "       Rebuild/download a probe built on an older-glibc base" >&2
                echo "       (CentOS 7 / manylinux2014, glibc 2.17)." >&2
                exit 1
            fi
        fi
    fi

    if [ -f "${INSTALL_DIR}/bin/agent-mesh-edge" ] || [ -f "${INSTALL_DIR}/bin/agent-mesh-edge.bin" ]; then
        echo "WARNING: agent-mesh-edge already exists at ${INSTALL_DIR}" >&2
        if [ -z "${FORCE_REINSTALL:-}" ]; then
            echo "Set FORCE_REINSTALL=1 to overwrite, or change INSTALL_DIR" >&2
            exit 1
        fi
        echo "FORCE_REINSTALL=1: overwriting existing installation"
        systemctl stop agent-mesh-edge 2>/dev/null || true
    fi

    echo "==> Pre-flight checks passed"
fi

# ========== Install ==========

echo "==> Installing agent-mesh agent to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}/bin"
mkdir -p "${WORK_DIR}"
mkdir -p "${INSTALL_DIR}/etc"

# Fresh install: drop stale sync/upgrade state so the new binary re-syncs
# LLM config and does not skip config_version comparison after reinstall.
rm -f "${INSTALL_DIR}/etc/config_version"
rm -f "${INSTALL_DIR}/etc/upgrading"
rm -f "${INSTALL_DIR}/etc/upgrade-started"
rm -f "${INSTALL_DIR}/bin/agent-mesh-edge.bin.old"
rm -f "${INSTALL_DIR}/etc/agent_version.bak"

if [ "$SCRIPT_DIR" = "${INSTALL_DIR}" ]; then
    mv -f "${INSTALL_DIR}/bin/agent-mesh-edge" "${INSTALL_DIR}/bin/agent-mesh-edge.bin" 2>/dev/null || true
else
    cp -a "$SCRIPT_DIR/bin/"* "${INSTALL_DIR}/bin/"
    mv -f "${INSTALL_DIR}/bin/agent-mesh-edge" "${INSTALL_DIR}/bin/agent-mesh-edge.bin" 2>/dev/null || true
fi

cat > "${INSTALL_DIR}/etc/edge.env" <<EOF
EDGE_AGENT_ID=$AGENT_ID
EDGE_ORCHESTRATOR_URL=$ORCHESTRATOR_URL
EDGE_TOKEN=$TOKEN
EDGE_HEARTBEAT_S=3
EDGE_RUNTIME=opencode
EDGE_WORKDIR=${WORK_DIR}
EDGE_INSTALL_DIR=${INSTALL_DIR}
EDGE_LLM_API_KEY=${EDGE_LLM_API_KEY:-}
EDGE_LLM_BASE_URL=${EDGE_LLM_BASE_URL:-}
EDGE_LLM_MODEL=${EDGE_LLM_MODEL:-}
EDGE_LLM_MODELS=${EDGE_LLM_MODELS:-}
EDGE_SYSTEM_PROMPT=${EDGE_SYSTEM_PROMPT:-}
LOG_LEVEL=INFO
EOF

cat > "${INSTALL_DIR}/bin/agent-mesh-edge" <<'WRAPPER'
#!/usr/bin/env bash
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
WRAPPER
chmod +x "${INSTALL_DIR}/bin/agent-mesh-edge"

# Record the installed package version so upgrades can be detected.
if [ -f "$SCRIPT_DIR/VERSION" ]; then
    mkdir -p "${INSTALL_DIR}/etc"
    cp -f "$SCRIPT_DIR/VERSION" "${INSTALL_DIR}/etc/agent_version"
fi

if [ -f "${INSTALL_DIR}/bin/opencode" ]; then
    chmod +x "${INSTALL_DIR}/bin/opencode" 2>/dev/null || true
    echo "==> opencode bundled: ${INSTALL_DIR}/bin/opencode"
else
    echo "WARNING: this package does not contain bin/opencode; llm tasks will" >&2
    echo "         fail on this node until opencode is installed in ${INSTALL_DIR}/bin/." >&2
fi

if [ "${SKIP_SERVICE:-0}" = "1" ]; then
    echo "==> Skipped service registration (SKIP_SERVICE=1 or not running as root)"
elif command -v systemctl >/dev/null 2>&1; then
    cat > /etc/systemd/system/agent-mesh-edge.service <<EOF
[Unit]
Description=agent-mesh edge agent
After=network.target

[Service]
Type=simple
EnvironmentFile=${INSTALL_DIR}/etc/edge.env
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/bin/agent-mesh-edge
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable agent-mesh-edge
    systemctl start agent-mesh-edge
    echo "==> Service installed and started: systemctl status agent-mesh-edge"
elif [ "$(uname -s)" = "Darwin" ]; then
    PLIST="/Library/LaunchDaemons/com.agentmesh.edge.plist"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.agentmesh.edge</string>
  <key>ProgramArguments</key>
  <array>
    <string>${INSTALL_DIR}/bin/agent-mesh-edge</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>${INSTALL_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${INSTALL_DIR}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
    launchctl load -w "$PLIST" 2>/dev/null || true
    launchctl start com.agentmesh.edge 2>/dev/null || true
    echo "==> LaunchAgent installed and started: launchctl list com.agentmesh.edge"
else
    echo "==> No supported init system detected. Start manually: ${INSTALL_DIR}/bin/agent-mesh-edge"
fi

echo "==> Agent installed to ${INSTALL_DIR}"
echo "    Control: systemctl {start,stop,status} agent-mesh-edge"
