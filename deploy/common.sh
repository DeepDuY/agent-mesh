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
    echo "  Install one of: python3.12-venv / virtualenv / uv, or ensure internet" >&2
    echo "  access to https://bootstrap.pypa.io/get-pip.py" >&2
    return 1
}
