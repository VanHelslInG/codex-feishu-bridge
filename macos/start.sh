#!/usr/bin/env bash
#
# Start the bridge in the background and wait for the health endpoint.
#
# Mirrors windows/start.ps1 so both platforms behave the same way: preflight
# the import in the foreground so a missing dependency or a syntax error reads
# as a real traceback, then leave a detached daemon behind.
#
# NOT YET VERIFIED ON A REAL MAC.
#
# Usage: macos/start.sh [timeout-seconds]
set -euo pipefail

TIMEOUT_SECONDS="${1:-60}"
case "$TIMEOUT_SECONDS" in
    ''|*[!0-9]*) echo "timeout must be a whole number of seconds" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/macos/lib/appdir.sh"

VENV_PYTHON="$(bridge_venv_python "$ROOT")"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtualenv missing. Run $ROOT/macos/install.sh first." >&2
    exit 1
fi

APP_DIR="$(bridge_app_dir)"
mkdir -p "$APP_DIR"
PID_FILE="$APP_DIR/bridge.pid"
LOG_FILE="$APP_DIR/bridge.log"
PORT="$(bridge_health_port "$APP_DIR")"

if [ -f "$PID_FILE" ]; then
    existing="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$existing" ] && kill -0 "$existing" 2>/dev/null; then
        echo "Bridge already running (pid $existing)"
        exit 0
    fi
fi

# Preflight in the foreground: import errors surface here as a traceback
# instead of disappearing into a detached process.
echo 'Preflight: importing the bridge'
if ! PYTHONPATH="$ROOT/src" CODEX_FEISHU_BRIDGE_HOME="$APP_DIR" \
    "$VENV_PYTHON" -c 'import feishu_bridge.bridge'; then
    echo 'Preflight failed; fix the error above before starting the daemon.' >&2
    exit 1
fi

# The daemon writes its own bridge.log; stdout/stderr are appended there too so
# an uncaught traceback lands next to the structured log instead of being lost.
PYTHONPATH="$ROOT/src" CODEX_FEISHU_BRIDGE_HOME="$APP_DIR" \
    nohup "$VENV_PYTHON" -m feishu_bridge.bridge run >>"$LOG_FILE" 2>&1 &
BRIDGE_PID=$!
printf '%s\n' "$BRIDGE_PID" >"$PID_FILE"
echo "Bridge started (pid $BRIDGE_PID)"

deadline=$((SECONDS + TIMEOUT_SECONDS))
while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 2
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo 'Bridge exited during startup. Last 30 log lines:'
        tail -n 30 "$LOG_FILE" 2>/dev/null || true
        echo
        echo 'For a full traceback run it in the foreground:'
        echo "  PYTHONPATH=\"$ROOT/src\" CODEX_FEISHU_BRIDGE_HOME=\"$APP_DIR\" \\"
        echo "    \"$VENV_PYTHON\" -m feishu_bridge.bridge run --verbose"
        exit 1
    fi
    if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/" 2>/dev/null \
        | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
        echo 'Health check: ok'
        exit 0
    fi
done

echo "Health check did not become ready in time on port $PORT; check $LOG_FILE" >&2
exit 1
