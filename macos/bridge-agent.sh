#!/usr/bin/env bash
#
# LaunchAgent entry point: keep the Codex Feishu Bridge running, but only while
# the Codex desktop app is open. Mirrors windows/ensure-running.ps1 so both
# platforms behave the same way — nothing runs until Codex is opened.
#
# Driven by a StartInterval trigger, not RunAtLoad/KeepAlive, so a closed Codex
# app leaves the bridge stopped.
#
# Usage (from the plist): bridge-agent.sh <venv-python> <repo-root>
#
# NOT YET VERIFIED ON A REAL MAC.
set -euo pipefail

PYTHON="${1:-}"
ROOT="${2:-}"
if [ -z "$PYTHON" ] || [ -z "$ROOT" ]; then
    echo "usage: bridge-agent.sh <venv-python> <repo-root>" >&2
    exit 64
fi

APP_DIR="${CODEX_FEISHU_BRIDGE_HOME:-$HOME/Library/Application Support/CodexFeishuBridge}"
PID_FILE="$APP_DIR/bridge.pid"

PORT="49660"
if [ -f "$APP_DIR/config.json" ]; then
    FOUND="$(sed -n 's/.*"health_port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
        "$APP_DIR/config.json" | head -n 1)"
    [ -n "$FOUND" ] && PORT="$FOUND"
fi

codex_app_running() {
    # Match on the app bundle path so that neither an unrelated ChatGPT window
    # nor our own `codex app-server` child can satisfy this check.
    pgrep -f '/Applications/(ChatGPT|Codex)\.app/Contents/MacOS/' >/dev/null 2>&1
}

healthy() {
    curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" 2>/dev/null | grep -q '"ok"[[:space:]]*:[[:space:]]*true'
}

if ! codex_app_running; then
    echo "The Codex desktop app is not running; leaving the bridge alone."
    exit 0
fi

if healthy; then
    exit 0
fi

echo "Starting the bridge."
mkdir -p "$APP_DIR"
PYTHONPATH="${ROOT}/src" CODEX_FEISHU_BRIDGE_HOME="$APP_DIR" \
    nohup "$PYTHON" -m feishu_bridge.bridge run >>"$APP_DIR/agent.log" 2>&1 &
echo $! >"$PID_FILE"
