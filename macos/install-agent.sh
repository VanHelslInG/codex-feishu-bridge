#!/usr/bin/env bash
#
# Register the LaunchAgent that keeps the bridge running — but only while the
# Codex desktop app is open.
#
# The agent polls on a StartInterval rather than RunAtLoad/KeepAlive, so a
# closed Codex app leaves the bridge stopped. That is the same shape as the
# Windows scheduled task, and it is what makes "nothing runs until Codex is
# opened" true on both platforms.
#
# This script changes launchd state for the current user. Run it deliberately.
#
# NOT YET VERIFIED ON A REAL MAC.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/macos/lib/appdir.sh"

LABEL="com.chen.codex-feishu-bridge"
APP_DIR="$(bridge_app_dir)"
VENV_PYTHON="$(bridge_venv_python "$ROOT")"
TEMPLATE="$ROOT/macos/$LABEL.plist.template"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Virtualenv missing. Run $ROOT/macos/install.sh first." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR"

sed -e "s|__ROOT__|$ROOT|g" \
    -e "s|__VENV__|$ROOT/.venv|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$TEMPLATE" >"$PLIST"
chmod 600 "$PLIST"

# bootout first so re-running the installer replaces a live agent instead of
# failing with "service already bootstrapped".
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL" 2>/dev/null || true

echo "Registered LaunchAgent $LABEL."
echo "  plist       : $PLIST"
echo "  interval    : every 120s, and it starts the bridge only while Codex is open"
echo "  check now   : launchctl kickstart -k gui/$UID/$LABEL"
echo "  inspect     : launchctl print gui/$UID/$LABEL"
echo "  remove      : $ROOT/macos/uninstall-agent.sh"
