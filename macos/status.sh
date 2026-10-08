#!/usr/bin/env bash
#
# Report bridge health, pairing state and the Codex app-server pid.
#
# NOT YET VERIFIED ON A REAL MAC.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/macos/lib/appdir.sh"

APP_DIR="$(bridge_app_dir)"
PID_FILE="$APP_DIR/bridge.pid"
PORT="$(bridge_health_port "$APP_DIR")"

if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "pid file: $pid (running: true)"
    else
        echo "pid file: $pid (running: false)"
    fi
else
    echo 'pid file: none'
fi

if ! curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/"; then
    echo "health endpoint http://127.0.0.1:${PORT}/ is not answering" >&2
    exit 1
fi
echo
