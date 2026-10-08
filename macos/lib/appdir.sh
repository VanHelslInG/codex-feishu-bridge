#!/usr/bin/env bash
#
# Shared helpers for the macOS launcher scripts.
#
# Sourced by macos/*.sh; not meant to be executed on its own.
#
# NOT YET VERIFIED ON A REAL MAC.

# Resolve the bridge application directory. Mirrors app_dir() in
# src/feishu_bridge/core/config.py: an explicit CODEX_FEISHU_BRIDGE_HOME wins,
# otherwise state, logs and downloaded images live under
# ~/Library/Application Support/CodexFeishuBridge.
bridge_app_dir() {
    if [ -n "${CODEX_FEISHU_BRIDGE_HOME:-}" ]; then
        printf '%s\n' "$CODEX_FEISHU_BRIDGE_HOME"
        return 0
    fi
    printf '%s\n' "$HOME/Library/Application Support/CodexFeishuBridge"
}

# Path of the interpreter inside the project virtualenv.
bridge_venv_python() {
    printf '%s\n' "$1/.venv/bin/python"
}

# Read the health port out of config.json.
#
# Deliberately sed-based rather than python-based: the port must be readable
# even when the virtualenv is missing or broken, which is exactly the state a
# status script gets reached for.
bridge_health_port() {
    config_file="$1/config.json"
    port="49660"
    if [ -f "$config_file" ]; then
        found="$(sed -n 's/.*"health_port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
            "$config_file" | head -n 1)"
        if [ -n "$found" ]; then
            port="$found"
        fi
    fi
    printf '%s\n' "$port"
}
