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
# NOT YET VERIFIED ON A REAL MAC. In particular, re-confirm the app-detection
# patterns below on the target machine — see the note on codex_app_running.
set -euo pipefail

PYTHON="${1:-}"
ROOT="${2:-}"
if [ -z "$PYTHON" ] || [ -z "$ROOT" ]; then
    echo "usage: bridge-agent.sh <venv-python> <repo-root>" >&2
    exit 64
fi

. "$ROOT/macos/lib/appdir.sh"

APP_DIR="$(bridge_app_dir)"
PID_FILE="$APP_DIR/bridge.pid"
PORT="$(bridge_health_port "$APP_DIR")"
STARTUP_GRACE_SECONDS="${BRIDGE_STARTUP_GRACE_SECONDS:-180}"

codex_app_running() {
    # Two deliberate choices here.
    #
    # 1. The patterns are anchored at the executable and match only the app's
    #    own GUI binary under Contents/MacOS. The CLI the bridge spawns lives
    #    deeper in the bundle (…/codex-cli/CodexCLI.app/Contents/MacOS/codex),
    #    so an unanchored search would count the bridge's own `codex app-server`
    #    child as "Codex is open" and the bridge would never stop.
    # 2. Two literal patterns instead of one alternation, so the check does not
    #    depend on which regex dialect this pgrep was built with.
    #
    # BRIDGE_APP_PATTERN overrides both: check what a Mac actually reports with
    # `pgrep -fl 'Contents/MacOS/'` before trusting the defaults there.
    if [ -n "${BRIDGE_APP_PATTERN:-}" ]; then
        pgrep -f "$BRIDGE_APP_PATTERN" >/dev/null 2>&1
        return $?
    fi
    pgrep -f '^/Applications/ChatGPT\.app/Contents/MacOS/' >/dev/null 2>&1 && return 0
    pgrep -f '^/Applications/Codex\.app/Contents/MacOS/' >/dev/null 2>&1 && return 0
    return 1
}

healthy() {
    curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" 2>/dev/null \
        | grep -q '"ok"[[:space:]]*:[[:space:]]*true'
}

if ! codex_app_running; then
    echo "The Codex desktop app is not running; leaving the bridge alone."
    exit 0
fi

if healthy; then
    exit 0
fi

echo "Nothing healthy on port $PORT."

# A bridge still inside its startup window may simply be coming up; killing it
# here would turn every scan into a start/stop/start churn. The pid file is
# written at spawn time, so its mtime is the process age.
if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        started="$(stat -f %m "$PID_FILE" 2>/dev/null || echo 0)"
        age=$(( $(date +%s) - started ))
        if [ "$age" -lt "$STARTUP_GRACE_SECONDS" ]; then
            echo "Bridge pid $pid started ${age}s ago; leaving it alone."
            exit 0
        fi
    fi
fi

echo 'Restarting the bridge.'
"$ROOT/macos/stop.sh" >/dev/null 2>&1 || true
exec "$ROOT/macos/start.sh" 90
