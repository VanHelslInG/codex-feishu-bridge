#!/usr/bin/env bash
# macOS installer for the Codex Feishu Bridge.
#
# Stage 2 of the plan: the shared core already runs on POSIX, but this script
# and the LaunchAgent template have NOT been validated on a real Mac yet.
# Validate on the target Mac before relying on them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/CodexFeishuBridge"
VENV="$ROOT/.venv"

PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"

mkdir -p "$APP_DIR"
if [ ! -f "$APP_DIR/config.json" ]; then
    cp "$ROOT/config.example.json" "$APP_DIR/config.json"
    echo "Wrote $APP_DIR/config.json (edit projects and health_port for this Mac)"
fi

if [ -n "${FEISHU_APP_ID:-}" ] && [ -n "${FEISHU_APP_SECRET:-}" ]; then
    security add-generic-password -s codex-feishu-bridge -a feishu-app-id -w "$FEISHU_APP_ID" -U
    security add-generic-password -s codex-feishu-bridge -a feishu-app-secret -w "$FEISHU_APP_SECRET" -U
    echo "Credentials stored in the login Keychain"
fi

chmod +x "$ROOT/macos/bridge-agent.sh"

echo "Installed."
echo "Start by hand with : $VENV/bin/python -m feishu_bridge.bridge run"
echo "Or install the agent: $ROOT/macos/bridge-agent.sh \"$VENV/bin/python\" \"$ROOT\""
echo "The agent starts the bridge only while the Codex desktop app is open."
