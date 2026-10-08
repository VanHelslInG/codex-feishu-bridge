#!/usr/bin/env bash
#
# Stop the bridge and its Codex app-server child tree.
#
# The bridge installs a SIGTERM handler that shuts its app-server down, so a
# graceful signal is the whole teardown; SIGKILL is only a last resort and can
# leave an orphaned app-server behind.
#
# NOT YET VERIFIED ON A REAL MAC.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/macos/lib/appdir.sh"

APP_DIR="$(bridge_app_dir)"
PID_FILE="$APP_DIR/bridge.pid"

if [ ! -f "$PID_FILE" ]; then
    echo 'No pid file; the bridge does not appear to be running.'
    exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$pid" ]; then
    rm -f "$PID_FILE"
    echo 'Empty pid file removed.'
    exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Process $pid is gone; stale pid file removed."
    exit 0
fi

echo "Stopping bridge pid $pid (including the app-server tree)"
kill -TERM "$pid" 2>/dev/null || true

waited=0
while [ "$waited" -lt 50 ]; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
    waited=$((waited + 1))
done

if kill -0 "$pid" 2>/dev/null; then
    echo "Bridge did not exit on SIGTERM; sending SIGKILL."
    kill -KILL "$pid" 2>/dev/null || true
    sleep 0.5
    if pgrep -f 'codex app-server' >/dev/null 2>&1; then
        echo 'A codex app-server may have outlived the forced stop; check with:' >&2
        echo "  pgrep -fl 'codex app-server'" >&2
    fi
fi

rm -f "$PID_FILE"
echo 'Stopped. SQLite state and artifacts were preserved.'
