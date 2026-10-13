#!/usr/bin/env bash
# Shared helpers for the deploy scripts.

# Create a Python virtualenv that has a working `pip`, tolerating distros whose
# Python lacks ensurepip (where `python -m venv` fails). Tries, in order:
#   uv -> standard venv -> venv --without-pip + get-pip bootstrap -> virtualenv
#
# Usage: ensure_venv <venv_dir> <python_bin>
ensure_venv() {
    local venv="$1" pybin="$2"

    if [ -x "${venv}/bin/pip" ]; then
        return 0
    fi
    rm -rf "${venv}"

    if command -v uv >/dev/null 2>&1; then
        echo "==> Creating virtual environment with uv (${pybin})"
        if uv venv --python "${pybin}" "${venv}" >/dev/null 2>&1 \
            && uv pip install -q --python "${venv}/bin/python" pip >/dev/null 2>&1 \
            && [ -x "${venv}/bin/pip" ]; then
            return 0
        fi
        rm -rf "${venv}"
    fi

    echo "==> Creating virtual environment (${pybin})"
    if "${pybin}" -m venv "${venv}" 2>/dev/null && [ -x "${venv}/bin/pip" ]; then
        return 0
    fi
    rm -rf "${venv}"

    # ensurepip unavailable: create the venv without pip, then bootstrap pip.
    if "${pybin}" -m venv --without-pip "${venv}"; then
        echo "==> ensurepip unavailable; bootstrapping pip via get-pip.py"
        local gp="/tmp/get-pip.$$.py"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "${gp}"
        elif command -v wget >/dev/null 2>&1; then
            wget -qO "${gp}" https://bootstrap.pypa.io/get-pip.py
        else
            echo "ERROR: need curl or wget to bootstrap pip" >&2
            return 1
        fi
        if "${venv}/bin/python" "${gp}" >/dev/null 2>&1 && [ -x "${venv}/bin/pip" ]; then
            rm -f "${gp}"
            return 0
        fi
        rm -f "${gp}"
    fi
    rm -rf "${venv}"

    if command -v virtualenv >/dev/null 2>&1; then
        echo "==> Creating virtual environment with virtualenv (${pybin})"
        virtualenv -p "${pybin}" "${venv}" >/dev/null 2>&1 && [ -x "${venv}/bin/pip" ] && return 0
    fi

    echo "ERROR: could not create a Python virtualenv with pip." >&2
    echo "  Fix one of the following:" >&2
    echo "    - install the venv/ensurepip support for your Python:" >&2
    echo "        Debian/Ubuntu : apt-get install -y python3.12-venv" >&2
    echo "        RHEL/CentOS   : yum install -y python3-pip   # or ensure python3.12 has venv+ensurepip" >&2
    echo "    - install 'uv'           (https://docs.astral.sh/uv/)" >&2
    echo "    - install 'virtualenv'   (pip install --user virtualenv)" >&2
    echo "    - ensure curl/wget can reach https://bootstrap.pypa.io/get-pip.py" >&2
    return 1
}

# ---------------------------------------------------------------------------
# Orchestrator listen-port helpers.
#
# Used by redeploy.sh / install.sh to confirm the service actually stopped
# (port freed) and actually started (port bound) before declaring success. A
# stale non-systemd process holding the port is the classic "upgrade says ok but
# the old process keeps serving" failure. All helpers degrade gracefully when
# ss/lsof/curl are unavailable.
# ---------------------------------------------------------------------------

# Resolve the orchestrator port: $AGENT_MESH_PORT > install env file > 8000.
orch_port() {
    local install_dir="$1"
    local port="${AGENT_MESH_PORT:-}"
    if [ -z "${port}" ] && [ -f "${install_dir}/etc/orchestrator.env" ]; then
        port="$(sed -n 's/^AGENT_MESH_PORT=//p' "${install_dir}/etc/orchestrator.env" | tail -1)"
    fi
    echo "${port:-8000}"
}

can_check_port() { command -v ss >/dev/null 2>&1 || command -v lsof >/dev/null 2>&1; }
can_http() { command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; }

# 0 if something is listening on the TCP port.
port_listening() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"
    elif command -v lsof >/dev/null 2>&1; then
        [ -n "$(lsof -nP -iTCP:"${p}" -sTCP:LISTEN 2>/dev/null)" ]
    else
        return 1
    fi
}

# Print the listener(s) on the port (for diagnostics).
port_holders() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | awk -v pat="[:.]${p}\$" '$4 ~ pat'
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"${p}" -sTCP:LISTEN 2>/dev/null
    fi
}

# 0 if the URL returns 2xx.
http_ok() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 5 "$url" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O /dev/null --timeout=5 "$url" 2>/dev/null
    else
        return 1
    fi
}
